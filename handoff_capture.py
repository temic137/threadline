"""Project-wide capture. Source archives are redacted, never silently discarded."""
import datetime
import hashlib
import json
import re
import sqlite3
import zlib
from pathlib import Path
from collections import Counter
from contextlib import closing

from handoff_sessions import (
    READERS, REDACTION_VERSION, candidate_meta, candidate_paths, make_event, read_cursor, read_jsonl, redact,
    sanitize_session, sanitize_value, session, text_of,
)


def workspace_path(value):
    # Windows extended paths and ordinary paths identify the same workspace.
    value = str(value)
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value).resolve()


def event_signature(event):
    content = event["content"]
    try:
        content = json.dumps(json.loads(content), sort_keys=True, ensure_ascii=False)
    except (ValueError, TypeError):
        pass
    return event["kind"], event["actor"], content


def codex_databases(home, issues=None):
    root = home / ".codex"
    def latest(prefix):
        try:
            paths = [(int(p.stem[len(prefix):]), p) for p in root.glob(prefix + "*.sqlite")
                     if p.stem[len(prefix):].isdigit()]
        except OSError as exc:
            if issues is not None:
                issues.append({"source": "codex", "path": str(root), "reason": f"Database discovery failed: {exc}"})
            return None
        return max(paths, default=(0, None))[1]
    return latest("state_"), latest("thread_history_")


def codex_sqlite_events(sid, item, row, turn):
    """Normalize public history items; unknown types remain in the source archive."""
    kind = item.get("type")
    timestamp = row.get("created_at_ms")
    if timestamp is None and turn.get("started_at") is not None:
        timestamp = turn["started_at"] * 1000
    if timestamp is not None:
        timestamp = datetime.datetime.fromtimestamp(int(timestamp) / 1000, datetime.timezone.utc).isoformat()
    metadata = {"native_item_id": row["item_id"], "native_turn_id": row["turn_id"],
                "rollout_ordinal": row["rollout_ordinal"], "source_format": "codex_sqlite",
                "turn_status": turn.get("status")}
    ordinal = int(row["rollout_ordinal"]) * 10
    def event(event_kind, actor, content, extra=None, offset=0):
        return make_event("codex", sid, ordinal+offset, event_kind, actor, content,
                          timestamp or "", {**metadata, **(extra or {})})
    if kind == "userMessage":
        return [event("user_message", "user", text_of(item.get("content", [])))]
    if kind == "agentMessage":
        return [event("assistant_message", "assistant", text_of(item.get("text", "")), {"phase": item.get("phase")})]
    if kind == "commandExecution":
        return [event("command", "assistant", text_of(item.get("aggregatedOutput", "")),
                      {"command": item.get("command"), "cwd": item.get("cwd"),
                       "exit_code": item.get("exitCode"), "status": item.get("status")})]
    if kind == "fileChange":
        return [event("file_change", "assistant", json.dumps(item.get("changes", []), ensure_ascii=False),
                      {"changes": item.get("changes", []), "status": item.get("status")})]
    if kind in ("mcpToolCall", "dynamicToolCall", "webSearch"):
        tool = {"tool_id": row["item_id"], "name": item.get("tool") or kind,
                "server": item.get("server") or item.get("namespace"), "status": item.get("status")}
        events = [event("tool_call", "assistant", json.dumps(item.get("arguments", item.get("query", "")), ensure_ascii=False), tool)]
        result = item.get("result", item.get("contentItems", item.get("results")))
        if result is not None or item.get("error"):
            events.append(event("tool_result", "tool", json.dumps(result if result is not None else item["error"], ensure_ascii=False), tool, 1))
        return events
    return []


def codex_sqlite_conversations(state_path, history_path, project, selected=None, issues=None):
    """Read source databases in read-only transactions; never create or modify them."""
    with closing(sqlite3.connect(state_path.resolve().as_uri() + "?mode=ro", uri=True)) as state:
        state.row_factory = sqlite3.Row
        state.execute("BEGIN")
        tables = {r[0] for r in state.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {r[1] for r in state.execute("PRAGMA table_info(threads)")}
        if not {"id", "cwd"} <= columns:
            raise sqlite3.DatabaseError("Unsupported Codex state schema: threads id/cwd missing")
        threads = [dict(r) for r in state.execute("SELECT * FROM threads")
                   if r["cwd"] and workspace_path(r["cwd"]) == project and (not selected or r["id"] == selected)]
        history = None
        try:
            if history_path:
                history = sqlite3.connect(history_path.resolve().as_uri() + "?mode=ro", uri=True)
                history.row_factory = sqlite3.Row
                history.execute("BEGIN")
                history_tables = {r[0] for r in history.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"thread_items", "thread_turns"} <= history_tables:
                    raise sqlite3.DatabaseError("Unsupported Codex history schema: thread_items/thread_turns missing")
                for table, required in (("thread_items", {"thread_id", "turn_id", "item_id", "rollout_ordinal", "item_json"}),
                                        ("thread_turns", {"thread_id", "turn_id", "rollout_ordinal"})):
                    available = {r[1] for r in history.execute(f"PRAGMA table_info({table})")}
                    if not required <= available:
                        raise sqlite3.DatabaseError(f"Unsupported Codex history schema: {table} columns missing")
                known = {r[0] for r in state.execute("SELECT id FROM threads")}
                orphans = {r[0] for r in history.execute("SELECT DISTINCT thread_id FROM thread_items")} - known
                if orphans and issues is not None:
                    issues.append({"source": "codex", "path": str(history_path),
                                   "reason": f"{len(orphans)} database thread(s) have no state metadata; project association unknown, not imported."})
            for thread in threads:
                sid = thread["id"]
                rows, events, gaps = [("thread", thread)], [], []
                metadata = {**thread, "source_format": "codex_sqlite", "history_database": str(history_path) if history_path else None}
                try:
                    metadata["source"] = json.loads(thread.get("source", ""))
                except (ValueError, TypeError):
                    pass
                if "thread_spawn_edges" in tables:
                    for edge in state.execute("SELECT * FROM thread_spawn_edges WHERE parent_thread_id=? OR child_thread_id=?", (sid, sid)):
                        value = dict(edge)
                        rows.append(("spawn:" + value["child_thread_id"], value))
                        if value["child_thread_id"] == sid:
                            metadata["parent_thread_id"] = value["parent_thread_id"]
                if "thread_attachments" in tables:
                    rows.extend(("attachment:" + r["id"], dict(r)) for r in state.execute("SELECT * FROM thread_attachments WHERE thread_id=?", (sid,)))
                if history:
                    turns = {r["turn_id"]: dict(r) for r in history.execute("SELECT * FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal,turn_id", (sid,))}
                    rows.extend(("turn:" + key, value) for key, value in turns.items())
                    count = 0
                    for native in history.execute("SELECT * FROM thread_items WHERE thread_id=? ORDER BY rollout_ordinal,turn_id,item_id", (sid,)):
                        value = dict(native)
                        rows.append(("item:" + value["turn_id"] + ":" + value["item_id"], value))
                        count += 1
                        try:
                            item = sanitize_value(json.loads(value["item_json"]))
                            if not isinstance(item, dict):
                                raise ValueError("Expected object")
                            events.extend(codex_sqlite_events(sid, item, value, turns.get(value["turn_id"], {})))
                        except (ValueError, TypeError, KeyError, OverflowError, OSError):
                            gaps.append("History item " + value["item_id"] + " could not be normalized; original row preserved.")
                    if "thread_realtime_items" in history_tables:
                        rows.extend(("realtime:" + r["item_id"], dict(r)) for r in history.execute("SELECT * FROM thread_realtime_items WHERE thread_id=? ORDER BY rollout_ordinal,item_id", (sid,)))
                    if "thread_history_projection_state" in history_tables:
                        cursor = history.execute("SELECT * FROM thread_history_projection_state WHERE thread_id=?", (sid,)).fetchone()
                        if cursor:
                            rows.append(("projection", dict(cursor)))
                            boundary = dict(cursor).get("next_rollout_ordinal")
                            if not gaps and count and isinstance(boundary, int) and boundary >= 0:
                                metadata["projected_rollout_ordinal"] = boundary
                    if not count:
                        gaps.append("No history items available for this database-listed thread; conversation content may be missing.")
                else:
                    gaps.append("Codex history database unavailable; only thread metadata captured.")
                gaps.append("SQLite rows preserved, including turn/item IDs. Unknown item types and realtime/attachment records are archive-only; attachment files are not copied.")
                captured = session("codex", sid, str(project), state_path, events)
                captured["metadata"] = metadata
                yield captured, rows, gaps, metadata
        finally:
            if history:
                history.close()


def deflate(text):
    return None if text is None else zlib.compress(text.encode(), 6)


def inflate(blob):
    return blob.decode() if isinstance(blob, str) else None if blob is None else zlib.decompress(blob).decode()


def register_functions(db):
    db.create_function("handoff_inflate", 1, inflate, deterministic=True)
    db.create_function("handoff_deflate", 1, deflate, deterministic=True)


def source_id(db, identity):
    """The number that stands for one (project, source, session, location) in the archive and links."""
    db.execute("INSERT OR IGNORE INTO capture_sources(project,source,session_id,location) VALUES (?,?,?,?)", identity)
    return db.execute("SELECT id FROM capture_sources WHERE project=? AND source=? AND session_id=? AND location=?",
                      identity).fetchone()[0]


def delete_sources(db, clause, params):
    """Remove archive rows and links for the matching sources; clause uses project/source/session_id/location."""
    ids = f"SELECT id FROM capture_sources WHERE {clause}"
    db.execute(f"DELETE FROM capture_archive WHERE src IN ({ids})", params)
    db.execute(f"DELETE FROM capture_links WHERE src IN ({ids})", params)


def initialize(db):
    register_functions(db)
    # Paths and chat ids are stored once in capture_sources; the archive is compressed. The old
    # capture_records and capture_event_links names stay readable as views.
    db.executescript("""
        CREATE TABLE IF NOT EXISTS capture_sources (
            id INTEGER PRIMARY KEY, project TEXT, source TEXT, session_id TEXT, location TEXT,
            UNIQUE(project, source, session_id, location)
        );
        CREATE TABLE IF NOT EXISTS capture_archive (
            src INTEGER, record_key TEXT, digest TEXT, payload BLOB, redacted INTEGER
        );
        CREATE UNIQUE INDEX IF NOT EXISTS capture_archive_key ON capture_archive(src, record_key, digest);
        CREATE TABLE IF NOT EXISTS capture_links (
            src INTEGER, event_id TEXT, PRIMARY KEY(src, event_id)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS capture_links_event ON capture_links(event_id);
        CREATE TABLE IF NOT EXISTS capture_state (
            project TEXT, source TEXT, session_id TEXT, location TEXT,
            digest TEXT, details TEXT,
            PRIMARY KEY(project, source, session_id, location)
        );
        CREATE TABLE IF NOT EXISTS capture_reports (project TEXT PRIMARY KEY, report TEXT);
        CREATE TABLE IF NOT EXISTS journal_meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS capture_snapshots (
            project TEXT, source TEXT, session_id TEXT, location TEXT, digest TEXT,
            captured_at TEXT, manifest TEXT,
            PRIMARY KEY(project, source, session_id, location, digest)
        );
    """)
    migrate_archive(db)
    db.executescript("""
        CREATE VIEW IF NOT EXISTS capture_records AS
            SELECT s.project, s.source, s.session_id, s.location, a.record_key, a.digest,
                   handoff_inflate(a.payload) AS payload, a.redacted
            FROM capture_archive a JOIN capture_sources s ON s.id=a.src;
        CREATE VIEW IF NOT EXISTS capture_event_links AS
            SELECT s.project, s.source, s.session_id, s.location, l.event_id
            FROM capture_links l JOIN capture_sources s ON s.id=l.src;
    """)
    scrub_journal(db)


def migrate_archive(db):
    """Move journals written before capture_sources into the smaller layout, once."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "capture_records" not in tables and "capture_event_links" not in tables:
        return
    with db:
        for old in ("capture_records", "capture_event_links"):
            if old in tables:
                db.execute(f"""INSERT OR IGNORE INTO capture_sources(project,source,session_id,location)
                               SELECT DISTINCT project,source,session_id,location FROM {old}""")
        if "capture_records" in tables:
            db.execute("""INSERT OR IGNORE INTO capture_archive
                          SELECT s.id, r.record_key, r.digest, handoff_deflate(r.payload), r.redacted
                          FROM capture_records r JOIN capture_sources s USING(project,source,session_id,location)""")
            db.execute("DROP TABLE capture_records")
        if "capture_event_links" in tables:
            db.execute("""INSERT OR IGNORE INTO capture_links
                          SELECT s.id, l.event_id
                          FROM capture_event_links l JOIN capture_sources s USING(project,source,session_id,location)""")
            db.execute("DROP TABLE capture_event_links")


def scrub_journal(db):
    """Apply current redaction to history stored under older rules. IDs stay stable so links still
    resolve; rows that held a secret are replaced under new IDs at the next capture."""
    row = db.execute("SELECT value FROM journal_meta WHERE key='redaction_version'").fetchone()
    if row and int(row[0]) >= REDACTION_VERSION:
        return
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    targets = {"events": ("content", "metadata"), "capture_archive": ("payload",), "capture_state": ("details",),
               "capture_reports": ("report",), "decision_annotations": ("payload",)}
    plain_text = {"content"}

    def cleaned_value(column, value):
        if not isinstance(value, str):
            return value
        if column in plain_text:
            return redact(value)
        # Redacting serialized JSON could remove an escape and corrupt it, so clean the values.
        try:
            parsed = json.loads(value)
        except ValueError:
            return redact(value)
        clean = sanitize_value(parsed)
        return value if clean == parsed else json.dumps(clean, ensure_ascii=False)

    with db:
        for table, columns in targets.items():
            if table not in tables:
                continue
            for rowid, *values in db.execute(f"SELECT rowid,{','.join(columns)} FROM {table}").fetchall():
                if table == "capture_archive":
                    values = [inflate(value) for value in values]
                cleaned = [cleaned_value(column, value) for column, value in zip(columns, values)]
                if cleaned != values:
                    if table == "capture_archive":
                        cleaned = [deflate(value) for value in cleaned]
                    assignment = ",".join(column + "=?" for column in columns)
                    db.execute(f"UPDATE {table} SET {assignment} WHERE rowid=?", (*cleaned, rowid))
        db.execute("INSERT OR REPLACE INTO journal_meta VALUES ('redaction_version',?)", (str(REDACTION_VERSION),))


def stored_report(journal, project):
    initialize(journal.db)
    row = journal.db.execute("SELECT report FROM capture_reports WHERE project=?",
                             (str(project.resolve()),)).fetchone()
    return json.loads(row[0]) if row else None


def summarize_report(report, gap_limit=5):
    """A capture report can reach megabytes; agents need its totals and its distinct gaps."""
    if not report:
        return None
    gaps = {}
    for gap in report.get("gaps", []):
        key = (gap.get("source"), gap.get("reason"))
        group = gaps.setdefault(key, {"source": key[0], "reason": str(key[1])[:160], "count": 0, "example": gap.get("path")})
        group["count"] += 1
    conversations = report.get("conversations", [])
    fields = ("project", "captured_at", "scope", "sources", "new_records", "new_events",
              "source_files_captured", "conversations_captured")
    return {**{key: report.get(key) for key in fields},
            "conversation_statuses": dict(Counter(item.get("status") for item in conversations)),
            "conversation_gaps": sum(len(item.get("gaps", [])) for item in conversations),
            "gaps_total": len(report.get("gaps", [])),
            "gaps": sorted(gaps.values(), key=lambda group: -group["count"])[:gap_limit]}


def coverage_page(report, start=0, limit=20):
    """Page per-conversation details without the source metadata kept in the full report."""
    if not report:
        return None
    if start < 0:
        raise ValueError("start must be nonnegative")
    conversations = report.get("conversations", [])
    rows = [{key: value for key, value in item.items() if key != "metadata"} for item in conversations[start:start+limit]]
    return {**summarize_report(report, gap_limit=50), "conversations": rows,
            "next_start": start + limit if start + limit < len(conversations) else None}


def record_types(kind, rows):
    """Describe archived record types without claiming their fields were normalized."""
    counts = Counter()
    for key, value in rows:
        if not isinstance(value, dict):
            counts["unstructured"] += 1
            continue
        label = str(value.get("type", "unknown"))
        payload = value.get("payload")
        if kind == "codex" and key.startswith("item:"):
            try:
                label = "sqlite/" + str(json.loads(value["item_json"]).get("type", "unknown"))
            except (ValueError, TypeError, KeyError, AttributeError):
                label = "sqlite/malformed_item"
        elif kind == "codex" and (key == "thread" or key == "projection" or key.startswith(("turn:", "spawn:", "attachment:", "realtime:"))):
            label = "sqlite/" + key.split(":")[0]
        elif kind == "codex" and isinstance(payload, dict) and payload.get("type"):
            label += "/" + str(payload["type"])
        elif kind == "muse" and isinstance(payload, dict):
            event = payload.get("event")
            if isinstance(event, dict):
                label = str(event.get("kind", "unknown"))
        elif kind == "cursor" and key == "header":
            label = "header"
        counts[label] += 1
    supported = {
        "codex": {"session_meta", "event_msg/user_message", "event_msg/agent_message", "event_msg/item_completed",
                  "response_item/function_call", "response_item/custom_tool_call", "response_item/function_call_output",
                  "response_item/custom_tool_call_output", "sqlite/userMessage", "sqlite/agentMessage",
                  "sqlite/commandExecution", "sqlite/fileChange", "sqlite/mcpToolCall", "sqlite/dynamicToolCall", "sqlite/webSearch"},
        "claude": {"user", "assistant"},
        "muse": {"started", "assistant_message_committed", "assistant_tool_calls_committed",
                 "tool_result_batch_committed", "failed", "terminal"},
        "cursor": {"header", "1", "2"},
    }[kind]
    return dict(counts), {key: count for key, count in counts.items() if key not in supported}


def cursor_conversations(path, project):
    """Read a consistent SQLite snapshot; never archive another workspace's rows."""
    global_db = path.parents[2] / "globalStorage" / "state.vscdb"
    if global_db.exists():
        with sqlite3.connect(global_db.resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            headers = db.execute("SELECT * FROM composerHeaders WHERE workspaceId=?",
                                 (path.parent.name,)).fetchall()
            for header in headers:
                sid = header["composerId"]
                rows = [("header", dict(header))]
                prefix = f"bubbleId:{sid}:"
                # A range uses the key index, so each chat does not scan every chat's rows.
                for key, value in db.execute("SELECT key,value FROM cursorDiskKV WHERE key>=? AND key<?",
                                             (prefix, prefix[:-1] + ";")):
                    try:
                        value = json.loads(value)
                    except (ValueError, TypeError):
                        pass
                    rows.append((key, value))
                # Normalize from this same snapshot; tool revisions remain in the archive.
                events = []
                for ordinal, (key, item) in enumerate(sorted(rows[1:], key=lambda r: (
                        str(r[1].get("createdAt", "")) if isinstance(r[1], dict) else "", r[0]))):
                    if not isinstance(item, dict):
                        continue
                    role = {1: "user", 2: "assistant"}.get(item.get("type"))
                    if role and item.get("text"):
                        events.append(make_event("cursor", sid, ordinal, role + "_message", role,
                                                 item["text"], str(item.get("createdAt", "")), {"source_key": key}))
                    tool = item.get("toolFormerData")
                    if isinstance(tool, dict):
                        events.append(make_event("cursor", sid, ordinal, "tool_result", "tool",
                                                 str(tool.get("result", "")), str(item.get("createdAt", "")),
                                                 cursor_tool_metadata(key, tool)))
                captured = session("cursor", sid, str(project), path, events)
                yield captured, rows, ["Cursor rows preserved; attachment files are not copied."], dict(header)
        return
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        rows = list(db.execute("SELECT key,value FROM ItemTable WHERE lower(key) LIKE '%chat%' OR lower(key) LIKE '%composer%' OR lower(key) LIKE '%prompt%'"))
    decoded = []
    for key, value in rows:
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            pass
        decoded.append((key, value))
    rows = decoded
    yield read_cursor(path, project), rows, [
        "Legacy Cursor storage: conversation boundaries unsupported; captured workspace records as one collection."
    ], {}


# Source files stay on disk, so the journal keeps what resume and search need, not a second copy.
# Raise this when the order of saved Codex events changes, so saved chats are read again.
CODEX_ORDER_VERSION = "v2"
RAW_RECORD_LIMIT = 2000
TOOL_ARGUMENT_LIMIT = 2000
TOOL_CONTENT_HEAD = 6000
TOOL_CONTENT_TAIL = 2000
MESSAGE_KINDS = ("user_message", "assistant_message")


def shortened_tool_content(content):
    """Keep the start and end of a long tool output. People's messages are never shortened."""
    if len(content) <= TOOL_CONTENT_HEAD + TOOL_CONTENT_TAIL:
        return None
    omitted = len(content) - TOOL_CONTENT_HEAD - TOOL_CONTENT_TAIL
    return f"{content[:TOOL_CONTENT_HEAD]}\n[... {omitted} characters not stored; metadata.source_path has the original ...]\n{content[-TOOL_CONTENT_TAIL:]}"


def cursor_tool_metadata(key, tool):
    """Cursor's tool data repeats the result and adds binary copies; keep what identifies the call."""
    arguments = tool.get("rawArgs") or tool.get("params") or ""
    arguments = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    return {"source_key": key, "name": tool.get("name", ""), "status": tool.get("status", ""),
            "toolCallId": tool.get("toolCallId", ""), "tool_id": tool.get("toolCallId", ""),
            "arguments": arguments[:TOOL_ARGUMENT_LIMIT]}


def codex_thread_homes(state_path):
    """A chat can change folders. The state database records where it lives now, for all its files."""
    if not state_path:
        return {}
    homes, resolved = {}, {}
    with closing(sqlite3.connect(f"file:{Path(state_path).resolve().as_posix()}?mode=ro", uri=True)) as db:
        for thread, cwd in db.execute("SELECT id,cwd FROM threads WHERE cwd IS NOT NULL AND cwd<>''"):
            if cwd not in resolved:
                resolved[cwd] = workspace_path(cwd)
            homes[thread] = resolved[cwd]
    return homes


def moments_of(events):
    """Database lines by message and second, to recognise the same message in a rollout file."""
    moments = {}
    for event in events:
        moments.setdefault((event_signature(event), str(event["timestamp"])[:19]), []).append(event["ordinal"])
    return moments


def rollout_offset(events, signatures, counts, positions, moments):
    """How far this file's lines sit from the database's, or None if the database wasn't read from this file.
    A fork's database lines continue from its parent's. Times suggest the distance, but only some
    are recorded exactly, so the lines decide: most database messages must sit at that distance."""
    distances = Counter()
    for event, signature in zip(events, signatures):
        distances.update({ordinal // 10 - event["ordinal"] for ordinal in moments.get((signature, str(event["timestamp"])[:19]), ())})
    for offset in dict.fromkeys([0, *(distance for distance, _ in distances.most_common(3))]):
        aligned = sum((signature, (event["ordinal"] + offset) * 10) in positions for event, signature in zip(events, signatures))
        if aligned * 2 >= sum(counts.values()):
            return offset
    return None


def codex_source_stamp(candidates, state_path, history_path):
    parts = [str(REDACTION_VERSION), CODEX_ORDER_VERSION]
    for path in (state_path, history_path):
        if path:
            wal = Path(str(path) + "-wal")
            parts += [str(path), file_stamp(path), file_stamp(wal) if wal.exists() else ""]
    parts += sorted(f"{path}:{file_stamp(path)}" for kind, path, _ in candidates if kind == "codex")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def file_stamp(path):
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


ROLLOUT_THREAD = re.compile(r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def compressed_capture(path):
    """Compressed rollouts stay on disk and can reach tens of megabytes, mostly compaction data and
    encrypted reasoning. Keep normalized messages and tool activity without copying raw records."""
    captured = READERS["codex"](path)
    metadata = {}
    for _, record in read_jsonl(path):
        if isinstance(record, dict) and record.get("type") == "session_meta":
            metadata.update(record.get("payload", {}))
        break
    return captured, [], ["Compressed rollout read directly: messages and tool activity normalized, raw records not copied. Attachment files are not copied."], metadata


def jsonl_capture(kind, path):
    rows, gaps, metadata = [], [], {}
    before = path.stat()
    # ponytail: reread histories to detect rewrites; byte cursors only if profiling justifies them.
    with path.open(encoding="utf-8") as stream:
        for ordinal, line in enumerate(stream):
            try:
                value = json.loads(line)
            except ValueError:
                gaps.append(f"Record {ordinal}: malformed or partial JSON; preserved as text, retry next sync.")
                value = line
            rows.append((str(ordinal), value))
            if isinstance(value, dict):
                if value.get("type") == "session_meta":
                    metadata.update(value.get("payload", {}))
                for key in ("sessionId", "parentUuid", "isSidechain", "gitBranch", "cwd", "title"):
                    if key in value:
                        metadata[key] = value[key]
    try:
        captured = READERS[kind](path)
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        sid, workspace = candidate_meta(kind, path)
        captured = session(kind, sid, workspace, path, [])
        gaps.append(f"Normalization failed ({type(exc).__name__}); source records preserved, normalized events unavailable.")
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError("Source changed during capture; retry synchronization.")
    if kind == "claude" and "subagents" in path.parts:
        parent_id = captured["id"]
        captured["id"] = parent_id + "/" + path.stem
        metadata["parent_session_id"] = parent_id
        captured["events"] = [make_event(kind, captured["id"], e["ordinal"], e["kind"], e["actor"],
                                          e["content"], e["timestamp"], e["metadata"]) for e in captured["events"]]
    if not rows:
        gaps.append("Empty source file.")
    gaps.append(f"Source records up to {RAW_RECORD_LIMIT} characters archived; larger ones stay in the source file. Normalization covers selected message/tool types only. Attachment files are not copied.")
    return captured, rows, gaps, metadata


def synchronize(journal, project, home, source="auto", selected=None, reread=False):
    """Archive all exact-workspace matches, preserving revisions and explicit gaps.
    Unchanged sources are skipped and not returned; reread=True returns every chat for exports."""
    project = workspace_path(project)
    initialize(journal.db)
    report = {"project": str(project), "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "scope": "Accessible local sources in known storage locations; exact workspace matches only. Cloud history and other worktrees are not automatically included.",
              "selection": {"source": source, "session": selected},
              "conversations": [], "gaps": [], "new_records": 0, "new_events": 0}
    seen, captured_sessions = set(), []
    candidates = candidate_paths(home, source, report["gaps"])
    state_path, history_path = codex_databases(home, report["gaps"]) if source in ("auto", "codex") else (None, None)
    if state_path:
        candidates.insert(0, ("codex", state_path, project))
    elif history_path:
        report["gaps"].append({"source": "codex", "path": str(history_path), "reason": "History database found without a state database; project association unavailable, not imported."})
    projected, sqlite_threads, state_threads, sqlite_signatures = {}, set(), set(), {}
    skipped_unchanged = 0
    skipped_chats = set()
    kinds = ("codex", "claude", "cursor", "muse") if source == "auto" else (source,)
    report["sources"] = {kind: {"candidate_files": sum(k == kind for k, _, _ in candidates)} for kind in kinds}
    if "codex" in report["sources"]:
        report["sources"]["codex"]["sqlite_databases"] = {"state": str(state_path) if state_path else None,
                                                        "history": str(history_path) if history_path else None}
    thread_homes = {}
    try:
        thread_homes = codex_thread_homes(state_path)
    except sqlite3.Error as exc:
        report["gaps"].append({"source": "codex", "path": str(state_path), "reason": f"Could not read chat folders: {exc}"})
    # When no Codex file changed, nothing below could change this project's Codex history.
    codex_stamp, codex_skip, codex_key = None, False, f"codex_stamp:{project}"
    if state_path and not selected and not reread:
        try:
            codex_stamp = codex_source_stamp(candidates, state_path, history_path)
        except OSError:
            codex_stamp = None
        known_stamp = journal.db.execute("SELECT value FROM journal_meta WHERE key=?", (codex_key,)).fetchone()
        stored = journal.db.execute("SELECT session_id,location,details FROM capture_state WHERE project=? AND source='codex'", (str(project),)).fetchall()
        if codex_stamp and known_stamp and known_stamp[0] == codex_stamp and stored:
            codex_skip = True
            for session_id, location, stored_details in stored:
                details = json.loads(stored_details)
                if details.get("status") != "identity_conflict":
                    details["status"] = "unchanged"
                    skipped_chats.add(("codex", session_id))
                report["conversations"].append(details)
                seen.add((str(project), "codex", session_id, location))
                skipped_unchanged += 1
    for kind, path, known_project in candidates:
        if kind == "codex" and codex_skip:
            continue
        try:
            stamp = None
            is_codex_db = kind == "codex" and path == state_path
            compressed = kind == "codex" and path.suffix == ".zst"
            if compressed:
                # Most compressed files belong to other projects; the name avoids opening them.
                named = ROLLOUT_THREAD.match(path.name)
                if (named and state_path and named.group(1) not in state_threads) or path.with_suffix("").exists():
                    continue
            elif kind != "cursor" and not is_codex_db:
                with path.open(encoding="utf-8"):
                    pass
            if is_codex_db:
                entries = codex_sqlite_conversations(path, history_path, project, selected, report["gaps"])
            elif kind == "cursor":
                if not known_project or workspace_path(known_project) != project:
                    continue
                location = str(path.resolve())
                wal = path.with_name(path.name + "-wal")
                cursor_stamp = f"cursor-v1:{REDACTION_VERSION}:{file_stamp(path)}:{file_stamp(wal) if wal.exists() else ''}"
                cursor_key = f"cursor_stamp:{project}:{location}"
                known = journal.db.execute("SELECT value FROM journal_meta WHERE key=?", (cursor_key,)).fetchone()
                captured_before = journal.db.execute("SELECT session_id,details FROM capture_state WHERE project=? AND source='cursor' AND location=?",
                                                     (str(project), location)).fetchall()
                if not reread and not selected and known and known[0] == cursor_stamp and captured_before:
                    for session_id, stored in captured_before:
                        details = json.loads(stored)
                        if details.get("status") != "identity_conflict":
                            details["status"] = "unchanged"
                            skipped_chats.add((kind, session_id))
                        report["conversations"].append(details)
                        seen.add((str(project), kind, session_id, location))
                        skipped_unchanged += 1
                    continue
                entries = cursor_conversations(path, project)
            else:
                sid, workspace = candidate_meta(kind, path, known_project)
                if kind == "codex" and sid in thread_homes:
                    workspace = str(thread_homes[sid])
                if not workspace:
                    report["gaps"].append({"source": kind, "path": str(path), "reason": "Project association unavailable; source not imported."})
                    continue
                if workspace_path(workspace) != project:
                    continue
                selection_id = sid + "/" + path.stem if kind == "claude" and "subagents" in path.parts else sid
                if selected and selected != selection_id and selected not in str(path):
                    continue
                if not selected:
                    identity = (str(project), kind, selection_id, str(path.resolve()))
                    stat = path.stat()
                    boundary = projected.get((sid, workspace_path(path.with_suffix("") if compressed else path)))
                    stamp = f"rollout-{CODEX_ORDER_VERSION}:{REDACTION_VERSION}:{stat.st_size}:{stat.st_mtime_ns}:{boundary[2] if boundary else sqlite_signatures.get(sid, ('',))[0]}"
                    row = journal.db.execute("SELECT details FROM capture_state WHERE project=? AND source=? AND session_id=? AND location=?", identity).fetchone()
                    if not reread and row and json.loads(row[0]).get("source_stamp") == stamp:
                        details = json.loads(row[0])
                        if details.get("status") != "identity_conflict":
                            details["status"] = "unchanged"
                            skipped_chats.add((kind, details.get("session_id", selection_id)))
                        seen.add(identity)
                        report["conversations"].append(details)
                        skipped_unchanged += 1
                        continue
                entries = [compressed_capture(path) if compressed else jsonl_capture(kind, path)]
            for captured, rows, gaps, metadata in entries:
                sid = captured["id"]
                if selected and selected != sid and selected not in str(path):
                    continue
                location = str(path.resolve())
                projection = projected.get((sid, workspace_path(path.with_suffix("") if compressed else path))) if kind == "codex" and not is_codex_db else None
                if compressed and sid in sqlite_signatures:
                    event_signatures = [event_signature({**e, "content": redact(str(e["content"]))}) for e in captured["events"]]
                    offset = rollout_offset(captured["events"], event_signatures, *sqlite_signatures[sid][1:])
                    if offset != 0:
                        # The database names this file, but its contents came from another one.
                        projection = None
                if projection is not None:
                    cursor, matches, overlap_digest = projection
                    original_count = len(captured["events"])
                    retained = []
                    for event in captured["events"]:
                        signature = event_signature({**event, "content": redact(str(event["content"]))})
                        # Identical words on another line are a separate message, such as a second "ok".
                        if (event["ordinal"] < cursor and matches[signature]
                                and {(signature, event["ordinal"] * 10), (signature, event["ordinal"] * 10 + 1)} & sqlite_signatures[sid][2]):
                            matches[signature] -= 1
                            continue
                        if compressed and event["ordinal"] < cursor and event["kind"] == "file_change":
                            continue  # The database records the same change in a different shape.
                        if compressed and event["ordinal"] < cursor:
                            # Merge into the database messages rather than after all of them.
                            event["metadata"].update(rollout_line=event["ordinal"], overlay_of=str(state_path.resolve()))
                        else:
                            event["metadata"]["capture_sequence"] = 1
                        retained.append(event)
                    captured["events"] = retained
                    metadata["sqlite_covered_events"] = original_count-len(captured["events"])
                    metadata["sqlite_projected_before_ordinal"] = cursor
                    metadata["sqlite_overlap_digest"] = overlap_digest
                    if not compressed and any(e["ordinal"] < cursor for e in retained):
                        gaps.append("Some JSONL records before the SQLite projection boundary have no exact normalized match; retained rather than discarded. Duplicate tool activity or source-order differences may remain.")
                elif compressed and sid in sqlite_signatures:
                    # The database can hold any one segment of a thread, whatever rollout_path
                    # says. Segments follow each other in time, so the same words in another
                    # segment are a new message; only the same line or second is the same message.
                    _, signatures, positions, moments = sqlite_signatures[sid]
                    matches = Counter(signatures)
                    retained = []
                    for event, signature in zip(captured["events"], event_signatures):
                        same_line = offset is not None and (signature, (event["ordinal"] + offset) * 10) in positions
                        if matches[signature] and (same_line or (signature, str(event["timestamp"])[:19]) in moments):
                            matches[signature] -= 1
                        else:
                            retained.append(event)
                    # Only the file the database was read from merges into its messages.
                    if offset is not None:
                        retained = [e for e in retained if e["kind"] != "file_change"]
                        for event in retained:
                            event["metadata"].update(rollout_line=event["ordinal"] + offset, overlay_of=str(state_path.resolve()))
                    captured["events"] = retained
                elif kind == "codex" and not is_codex_db and sid in sqlite_threads:
                    gaps.append("SQLite and this continuation segment overlap without a confirmed projection boundary; both retained, duplicate exchanges may appear.")
                identity = (str(project), kind, sid, location)
                seen.add(identity)
                clean_rows = []
                for key, value in rows:
                    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
                    if len(raw) > RAW_RECORD_LIMIT:
                        clean_rows.append((str(key), json.dumps({"not_copied_chars": len(raw)}), hashlib.sha256(raw.encode()).hexdigest(), False))
                        continue
                    clean = sanitize_value(value)
                    if is_codex_db and isinstance(clean, dict):
                        for field in ("item_json", "error_json", "payload"):
                            if isinstance(clean.get(field), str):
                                try:
                                    clean[field] = json.dumps(sanitize_value(json.loads(value[field])), ensure_ascii=False)
                                except ValueError:
                                    pass
                    payload = json.dumps(clean, ensure_ascii=False, sort_keys=True, default=str)
                    clean_rows.append((str(key), payload, hashlib.sha256(payload.encode()).hexdigest(), value != clean))
                serialized = json.dumps([[key, record_digest] for key, _, record_digest, _ in clean_rows])
                policy = "codex-sqlite-v1:" if is_codex_db else (f"codex-overlap-v1:{cursor}:{overlap_digest}:" if projection is not None else "capture-v3:")
                if compressed:
                    policy = stamp
                digest = hashlib.sha256((policy + serialized).encode()).hexdigest()
                previous = journal.db.execute("SELECT digest FROM capture_state WHERE project=? AND source=? AND session_id=? AND location=?", identity).fetchone()
                details = {"source": kind, "session_id": sid, "path": location,
                           "records": len(rows), "events": len(captured["events"]),
                           "status": "unchanged" if previous and previous[0] == digest else "imported",
                           "metadata": sanitize_value({key: value for key, value in metadata.items() if key not in ("base_instructions", "dynamic_tools")}),
                           "gaps": gaps, "redacted_records": 0}
                if stamp:
                    details["source_stamp"] = stamp
                details["record_types"], details["archive_only_record_types"] = record_types(kind, rows)
                owner = journal.db.execute("SELECT project, source_path FROM sessions WHERE source=? AND id=?",
                                           (kind, sid)).fetchone()
                conflict = owner and owner["project"] != str(project)
                if conflict and kind == "codex" and thread_homes.get(sid) == project:
                    # Codex moved this chat here, so it no longer belongs under its old folder.
                    journal.delete(Path(owner["project"]), sid)
                    conflict = False
                if conflict:
                    details["status"] = "identity_conflict"
                    gaps.append("Native session ID already belongs to another source location/project; records archived but normalized history not replaced.")
                captured["project"] = str(project)
                captured = sanitize_session(captured)
                captured["events"] = [make_event(kind, sid, e["ordinal"], e["kind"], e["actor"],
                                                 e["content"], e["timestamp"], {**e["metadata"], "source_path": location, "capture_order": order})
                                      for order, e in enumerate(captured["events"])]
                full_events = captured["events"]
                captured["events"] = []
                for e in full_events:
                    short = None if e["kind"] in MESSAGE_KINDS else shortened_tool_content(e["content"])
                    captured["events"].append(e if short is None else make_event(
                        kind, sid, e["ordinal"], e["kind"], e["actor"], short, e["timestamp"],
                        {**e["metadata"], "full_chars": len(e["content"])}))
                # Source revisions are append-only; normalized events reflect the latest source snapshot.
                with journal.db:
                    new_records, new_events = 0, 0
                    if compressed:
                        # Codex compressed this file after an earlier capture; this replaces those rows.
                        earlier = (str(project), kind, sid, str(path.with_suffix("").resolve()))
                        earlier_src = source_id(journal.db, earlier)
                        for (event_id,) in journal.db.execute("SELECT event_id FROM capture_links WHERE src=?", (earlier_src,)).fetchall():
                            journal.db.execute("DELETE FROM events WHERE id=?", (event_id,))
                        journal.db.execute("DELETE FROM capture_links WHERE src=?", (earlier_src,))
                        journal.db.execute("DELETE FROM capture_state WHERE project=? AND source=? AND session_id=? AND location=?", earlier)
                    manifest = []
                    src = source_id(journal.db, identity)
                    for key, payload, record_digest, redacted in clean_rows:
                        details["redacted_records"] += int(redacted)
                        manifest.append([key, record_digest])
                        inserted = journal.db.execute("INSERT OR IGNORE INTO capture_archive VALUES (?,?,?,?,?)",
                                                      (src, key, record_digest, deflate(payload), int(redacted)))
                        new_records += inserted.rowcount
                    if not conflict and details["status"] != "unchanged" and not any("Normalization failed" in gap for gap in gaps):
                        old_ids = {r[0] for r in journal.db.execute("""SELECT e.id FROM events e
                            WHERE e.project=? AND e.source=? AND e.session_id=? AND (
                                EXISTS (SELECT 1 FROM capture_links l WHERE l.event_id=e.id AND l.src=?)
                                OR NOT EXISTS (SELECT 1 FROM capture_links l WHERE l.event_id=e.id))""", (*identity[:3], src))}
                        new_ids = {e["id"] for e in captured["events"]}
                        new_events = len(new_ids - old_ids)
                        for event_id in old_ids - new_ids:
                            journal.db.execute("DELETE FROM events WHERE id=?", (event_id,))
                        journal.import_session(captured, commit=False)
                        # An event read before keeps its id, but may now belong in a different place.
                        journal.db.executemany("UPDATE events SET metadata=? WHERE id=?",
                                               [(json.dumps(e["metadata"], ensure_ascii=False), e["id"]) for e in captured["events"]])
                        journal.db.execute("DELETE FROM capture_links WHERE src=?", (src,))
                        journal.db.executemany("INSERT OR IGNORE INTO capture_links VALUES (?,?)",
                                               [(src, event_id) for event_id in new_ids])
                    journal.db.execute("INSERT OR IGNORE INTO capture_snapshots VALUES (?,?,?,?,?,?,?)",
                                       (*identity, digest, report["captured_at"], json.dumps(manifest)))
                    journal.db.execute("INSERT OR REPLACE INTO capture_state VALUES (?,?,?,?,?,?)",
                                       (*identity, digest, json.dumps(details)))
                report["new_records"] += new_records
                report["new_events"] += new_events
                report["conversations"].append(details)
                if not conflict:
                    captured_sessions.append(captured if reread or selected else {**captured, "events": []})
                    if is_codex_db:
                        state_threads.add(sid)
                    if is_codex_db and full_events:
                        sqlite_threads.add(sid)
                        signatures = Counter(event_signature(e) for e in full_events)
                        sqlite_signatures[sid] = (hashlib.sha256(json.dumps([e["id"] for e in full_events]).encode()).hexdigest(), signatures,
                                                  {(event_signature(e), e["ordinal"]) for e in full_events},
                                                  moments_of(full_events))
                    if is_codex_db and metadata.get("projected_rollout_ordinal") is not None and metadata.get("rollout_path"):
                        signatures = Counter(event_signature(e) for e in full_events)
                        overlap_digest = hashlib.sha256(json.dumps([e["id"] for e in full_events]).encode()).hexdigest()
                        projected[(sid, workspace_path(metadata["rollout_path"]))] = metadata["projected_rollout_ordinal"], signatures, overlap_digest
            if kind == "cursor" and not selected and known_project and workspace_path(known_project) == project and path.suffix != ".jsonl":
                journal.db.execute("INSERT OR REPLACE INTO journal_meta VALUES (?,?)", (cursor_key, cursor_stamp))
        except (OSError, ValueError, TypeError, AttributeError, KeyError, sqlite3.Error) as exc:
            report["gaps"].append({"source": kind, "path": str(path), "reason": f"Capture failed: {type(exc).__name__}: {exc}"})
    if codex_stamp and not codex_skip and not any(
            gap.get("source") == "codex" and "Capture failed" in gap.get("reason", "") for gap in report["gaps"] if isinstance(gap, dict)):
        journal.db.execute("INSERT OR REPLACE INTO journal_meta VALUES (?,?)", (codex_key, codex_stamp))
    for row in journal.db.execute("SELECT * FROM capture_state WHERE project=?", (str(project),)):
        identity = tuple(row[key] for key in ("project", "source", "session_id", "location"))
        if row["source"] in kinds and not selected and identity not in seen:
            details = json.loads(row["details"])
            details["status"] = "unavailable"
            details["gaps"] = ["Previously captured source was not available in this scan; archived history retained."]
            report["conversations"].append(details)
    for kind, info in report["sources"].items():
        if not info["candidate_files"]:
            info["status"] = "No sources found in known local locations; installation/access not verified."
    report["source_files_captured"] = len(captured_sessions) + skipped_unchanged
    grouped = {}
    for captured in captured_sessions:
        key = (captured["source"], captured["id"])
        if key not in grouped:
            grouped[key] = {**captured, "events": list(captured["events"])}
        else:
            grouped[key]["events"].extend(captured["events"])
            grouped[key]["updated"] = max(grouped[key]["updated"], captured["updated"])
    captured_sessions = list(grouped.values())
    for captured in captured_sessions:
        captured["events"].sort(key=lambda e: (e["metadata"].get("capture_sequence", 0), e["timestamp"], e["metadata"]["source_path"], e["ordinal"]))
    report["conversations_captured"] = len(skipped_chats | {(c["source"], c["id"]) for c in captured_sessions})
    safe_report = sanitize_value(report)
    journal.db.execute("INSERT OR REPLACE INTO capture_reports VALUES (?,?)", (str(project), json.dumps(safe_report)))
    journal.db.commit()
    return captured_sessions, safe_report
