"""Pick random request/answer pairs from chats whose original files are on this computer."""
import json, os, random, sqlite3, sys
from pathlib import Path

home = Path.home()
raw_ids = set()
for root in (home / ".codex" / "sessions", home / ".codex" / "archived_sessions"):
    for path in root.rglob("rollout-*"):
        name = path.name.split(".")[0]
        raw_ids.add(name[-36:])
        raw_ids.update(part for part in name.split("_") if len(part) == 36)
for path in (home / ".claude" / "projects").rglob("*.jsonl"):
    raw_ids.add(path.stem)

db = sqlite3.connect(home / ".handoff" / "handoff.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from handoff_sessions import Codec
Codec(home / ".handoff" / "handoff.db", db).register(db)
skip = ("<", "The following is the Codex", "This session is being continued", "[", "#", "Base directory")
rows = db.execute("""SELECT project, source, session_id, id, ordinal, timestamp, content FROM events
                     WHERE kind='user_message' AND source IN ('codex','claude')""").fetchall()
random.seed(int(sys.argv[1]) if len(sys.argv) > 1 else 7)
random.shuffle(rows)
picked, per_session = [], set()
for project, source, session, _event_id, ordinal, ts, content in rows:
    text = content.strip()
    if session not in raw_ids or session in per_session or not 40 <= len(text) <= 1200 or text.startswith(skip):
        continue
    reply = db.execute("""SELECT content FROM events WHERE session_id=? AND kind='assistant_message'
                          AND (timestamp > ? OR (timestamp = ? AND ordinal > ?)) ORDER BY timestamp, ordinal LIMIT 1""",
                       (session, ts, ts, ordinal)).fetchone()
    if not reply or len(reply[0]) < 200:
        continue
    per_session.add(session)
    picked.append({"n": len(picked) + 1, "project": project, "source": source, "session": session,
                   "request": text[:700], "answer": reply[0][:900]})
    if len(picked) == int(os.environ.get("COUNT", 30)):
        break
json.dump(picked, open(Path(__file__).parent / "picked.json", "w"), indent=1, ensure_ascii=False)
for p in picked:
    print(f"\n### {p['n']} [{p['source']} {Path(p['project']).name} {p['session'][:8]}]\nREQUEST: {p['request'][:400]}\nANSWER: {p['answer'][:600]}")
