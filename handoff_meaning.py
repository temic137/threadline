"""Search by meaning: finds messages that answer a question even when they use other words.

Optional. It needs `pip install handoff[meaning]` (fastembed, which downloads the 90 MB
all-MiniLM-L6-v2 model once). Without it, search uses words only.

Messages are cut into passages and each passage gets a vector, stored in the journal.
Indexing runs newest messages first, saves as it goes and resumes where it stopped, so
recent work is searchable within minutes of the first run."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

MODEL = "sentence-transformers/all-MiniLM-L6-v2"
PASSAGE, STEP, MAX_PASSAGES = 900, 700, 4  # The first ~3,000 characters of each message; pasted documents add little.
KINDS = ("user_message", "assistant_message")
_LOCK = threading.Lock()
_MODELS: dict = {}


def available() -> bool:
    try:
        import fastembed  # noqa: F401
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False


def passages(content: str) -> list[tuple[int, str]]:
    """Overlapping pieces of a message, as (start, text)."""
    if not content or not content.strip():
        return []
    starts = range(0, max(len(content) - 200, 1), STEP)
    return [(start, content[start:start + PASSAGE]) for start in list(starts)[:MAX_PASSAGES]]


class FastEmbedder:
    def __init__(self, cache_dir: Path):
        from fastembed import TextEmbedding
        # Half the cores, so indexing in the background leaves the machine usable.
        self.model = TextEmbedding(MODEL, cache_dir=str(cache_dir), threads=max(1, (os.cpu_count() or 2) // 2))

    def __call__(self, texts: list[str]):
        import numpy as np
        return np.array(list(self.model.embed(texts, batch_size=32)), dtype=np.float32)


def default_embedder(db_path: Path):
    key = str(db_path.parent)
    with _LOCK:
        if key not in _MODELS:
            _MODELS[key] = FastEmbedder(db_path.parent / "models")
        return _MODELS[key]


def initialize(db: sqlite3.Connection):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS meaning_vectors (
            project TEXT, event_id TEXT, start INTEGER, model TEXT, vector BLOB,
            PRIMARY KEY (event_id, start, model)
        );
        CREATE INDEX IF NOT EXISTS meaning_vectors_project ON meaning_vectors(project, model);
        CREATE TABLE IF NOT EXISTS meaning_done (event_id TEXT, model TEXT, PRIMARY KEY (event_id, model));
    """)


class MeaningIndex:
    """Vectors for one journal. Searches read whatever is indexed so far."""

    def __init__(self, db_path: Path, embedder=None, model=MODEL):
        self.db_path, self.model_name = Path(db_path), model
        self._embedder = embedder
        self._cache: dict = {}  # project -> (last rowid, ids, starts, matrix)
        self._threads: dict = {}
        self._vector_lock = threading.Lock()

    def embedder(self):
        if self._embedder is None:
            self._embedder = default_embedder(self.db_path)
        return self._embedder

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        from handoff_sessions import Codec
        Codec(self.db_path, db).register(db)  # The events view reads compressed text.
        initialize(db)
        return db

    def pending(self, db: sqlite3.Connection, project: str, limit=None) -> list[tuple[str, str]]:
        """Messages without vectors, newest first, leaving out child tasks such as automatic reviews,
        which repeat their main chat's messages."""
        children = _child_chats(db, project)
        rows = db.execute(f"""SELECT e.id, e.source, e.session_id, e.content FROM events e
            WHERE e.project=? AND e.kind IN ({",".join("?" * len(KINDS))})
              AND NOT EXISTS (SELECT 1 FROM meaning_done d WHERE d.event_id=e.id AND d.model=?)
            ORDER BY e.timestamp DESC, e.ordinal DESC""", (project, *KINDS, self.model_name))
        found = []
        for event_id, source, session_id, content in rows:
            if (source, session_id) not in children:
                found.append((event_id, content))
                if limit and len(found) == limit:
                    break
        return found

    def status(self, project: Path) -> dict:
        key = str(Path(project).resolve())
        db = self._connect()
        try:
            children = _child_chats(db, key)
            total = done = 0
            for source, session_id, count, indexed in db.execute(f"""SELECT e.source, e.session_id, count(*),
                    count(d.event_id) FROM events e LEFT JOIN meaning_done d ON d.event_id=e.id AND d.model=?
                    WHERE e.project=? AND e.kind IN ({",".join("?" * len(KINDS))}) GROUP BY 1,2""",
                                                                  (self.model_name, key, *KINDS)):
                if (source, session_id) not in children:
                    total, done = total + count, done + indexed
        finally:
            db.close()
        return {"messages_indexed": done, "messages_total": total, "indexing": self.indexing(project)}

    def index(self, project: Path, seconds=None, progress=None, stop=None) -> int:
        """Add vectors for unindexed messages, newest first. Returns how many messages were added."""
        key = str(Path(project).resolve())
        db = self._connect()
        added, began = 0, time.monotonic()
        try:
            todo = self.pending(db, key)
            for first in range(0, len(todo), 64):
                if (seconds is not None and time.monotonic() - began > seconds) or (stop and stop.is_set()):
                    break
                batch = todo[first:first + 64]
                pieces = [(event_id, start, text) for event_id, content in batch for start, text in passages(content)]
                vectors = self.embedder()([text for _, _, text in pieces]) if pieces else []
                db.executemany("INSERT OR REPLACE INTO meaning_vectors VALUES (?,?,?,?,?)",
                               [(key, event_id, start, self.model_name, _normalized(vector))
                                for (event_id, start, _), vector in zip(pieces, vectors)])
                db.executemany("INSERT OR IGNORE INTO meaning_done VALUES (?,?)", [(event_id, self.model_name) for event_id, _ in batch])
                db.commit()
                added += len(batch)
                if progress:
                    progress(added, len(todo))
        finally:
            db.close()
        return added

    def index_in_background(self, project: Path) -> bool:
        """Start indexing on a thread unless it is already running for this project."""
        key = str(Path(project).resolve())
        running = self._threads.get(key)
        if running and running.is_alive():
            return False
        thread = threading.Thread(target=self._index_quietly, args=(project,), daemon=True, name="handoff-meaning")
        self._threads[key] = thread
        thread.start()
        return True

    def _index_quietly(self, project):
        try:
            self.embedder()  # Loading the model and vectors here spares the next search the wait.
            self._vectors(str(Path(project).resolve()))
            self.index(project)
        except Exception:  # A failed background run must not break the server; search keeps using words.
            pass

    def indexing(self, project: Path) -> bool:
        thread = self._threads.get(str(Path(project).resolve()))
        return bool(thread and thread.is_alive())

    def _vectors(self, key: str):
        with self._vector_lock:  # The background indexer and a search may load at the same time.
            return self._load_vectors(key)

    def _load_vectors(self, key: str):
        import numpy as np
        last, ids, starts, matrix = self._cache.get(key, (0, [], [], np.zeros((0, 0), dtype=np.float32)))
        db = self._connect()
        try:
            rows = db.execute("SELECT rowid, event_id, start, vector FROM meaning_vectors WHERE project=? AND model=? AND rowid>? ORDER BY rowid",
                              (key, self.model_name, last)).fetchall()
        finally:
            db.close()
        if rows:
            new = np.frombuffer(b"".join(row[3] for row in rows), dtype=np.float32).reshape(len(rows), -1)
            matrix = new if not len(ids) else np.vstack([matrix, new])
            ids, starts, last = ids + [row[1] for row in rows], starts + [row[2] for row in rows], rows[-1][0]
            self._cache[key] = (last, ids, starts, matrix)
        return ids, starts, matrix

    def hits(self, project: Path, query: str, pool=300) -> list[tuple[str, int, float]]:
        """Messages closest in meaning to the query, as (event_id, passage start, similarity), best first."""
        import numpy as np
        key = str(Path(project).resolve())
        ids, starts, matrix = self._vectors(key)
        if not ids or not query.strip():
            return []
        q = _unit(self.embedder()([query])[0])
        scores = matrix @ q
        order = np.argsort(-scores)
        best: dict = {}
        for i in order[:pool * MAX_PASSAGES]:
            if ids[i] not in best:
                best[ids[i]] = (int(starts[i]), float(scores[i]))
                if len(best) == pool:
                    break
        return [(event_id, start, score) for event_id, (start, score) in best.items()]



def delete(db: sqlite3.Connection, project: str, session_id=None):
    """Remove vectors of deleted history. Call before the events themselves are deleted."""
    initialize(db)
    ids, params = "SELECT id FROM events WHERE project=?", [project]
    if session_id:
        ids, params = ids + " AND session_id=?", params + [session_id]
    db.execute(f"DELETE FROM meaning_vectors WHERE event_id IN ({ids})", params)
    db.execute(f"DELETE FROM meaning_done WHERE event_id IN ({ids})", params)


def _unit(vector):
    import numpy as np
    vector = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


def _normalized(vector) -> bytes:
    return _unit(vector).tobytes()


def _child_chats(db: sqlite3.Connection, project: str) -> set:
    """Chats started by another chat, read from capture metadata (the same rules as Journal.conversations)."""
    children = set()
    try:
        rows = db.execute("SELECT source, session_id, details FROM capture_state WHERE project=?", (project,)).fetchall()
    except sqlite3.OperationalError:
        return children
    for source, session_id, details in rows:
        try:
            metadata = json.loads(details).get("metadata", {})
        except (TypeError, ValueError):
            continue
        origin = metadata.get("source")
        if (metadata.get("parent_thread_id") or metadata.get("parent_session_id") or metadata.get("isSubagent")
                or metadata.get("isSidechain") or (isinstance(origin, dict) and "subagent" in origin)):
            children.add((source, session_id))
    return children
