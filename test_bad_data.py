"""Damaged, half-written, empty, binary, huge and unfamiliar history must never stop a capture.

Each agent's folder gets one good chat next to broken files. The good chat must be captured and
found, and the capture must finish without raising.
"""
import json
import sqlite3

import zstandard

from handoff_capture import synchronize
from handoff_sessions import Journal, mcp_response

GOOD = "the zebracorn signing key is rotated with rotate.sh"


def line(value):
    return (json.dumps(value) + "\n").encode()


def claude_rows(project, session):
    return [{"sessionId": session, "cwd": str(project), "type": "user", "timestamp": "2026-01-01T00:00:00Z",
             "message": {"role": "user", "content": "How is the zebracorn signing key rotated?"}},
            {"sessionId": session, "cwd": str(project), "type": "assistant", "timestamp": "2026-01-01T00:00:05Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": GOOD}]}}]


def codex_rows(project, session):
    return [{"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta", "payload": {"id": session, "cwd": str(project)}},
            {"timestamp": "2026-01-01T00:00:01Z", "type": "response_item",
             "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "codex zebracorn question"}]}}]


# Lines a reader might meet in a damaged or future-format file.
BAD_LINES = [
    b"not json at all\n",
    b"{\"half\": \n",
    b"[1, 2, 3]\n",
    b"42\n",
    b"\"just a string\"\n",
    b"null\n",
    b"\xff\xfe\xfa invalid utf-8 \x80\n",
    b"\x00\x00\x00\n",
    b"\n",
    line({"type": "user", "message": None}),
    line({"type": "user", "message": {"role": "user", "content": 12345}}),
    line({"type": "user", "message": {"role": "user", "content": [None, 7, {"type": "unknown"}, {"text": None}]}}),
    line({"type": "assistant", "message": {"content": [{"type": "tool_use", "input": "not a dict"}]}}),
    line({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": {"weird": True}}]}}),
    line({"type": "response_item", "payload": None}),
    line({"type": "response_item", "payload": {"type": "function_call", "arguments": 5}}),
    line({"type": "response_item", "payload": {"type": "message", "role": "user", "content": "plain string"}}),
    line({"type": "session_meta", "payload": {"id": None, "cwd": None}}),
    line({"type": "event_msg", "payload": {"type": "brand_new_event_kind", "data": [1, {"x": None}]}}),
    line({"timestamp": "not a time", "type": "user", "message": {"role": "user", "content": "bad time"}}),
    line({"timestamp": 1700000000, "type": "user", "message": {"role": "user", "content": "number time"}}),
    line({"type": "summary", "summary": None}),
    line({"deeply": [[[[[[[[[[{"nested": "value"}]]]]]]]]]]}),
]


def build_home(tmp_path, monkeypatch):
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir(parents=True)
    monkeypatch.setattr("handoff_sessions.cursor_root", lambda _: home / "cursor")

    claude = home / ".claude" / "projects" / "project"
    claude.mkdir(parents=True)
    good = b"".join(line(row) for row in claude_rows(project, "good-chat"))
    (claude / "good-chat.jsonl").write_bytes(BAD_LINES[0] + good + b"".join(BAD_LINES) + b"{\"cut off mid")
    (claude / "empty.jsonl").write_bytes(b"")
    (claude / "binary.jsonl").write_bytes(bytes(range(256)) * 64)
    (claude / "only-bad.jsonl").write_bytes(b"".join(BAD_LINES))
    (claude / "huge-line.jsonl").write_bytes(line({"sessionId": "huge", "cwd": str(project), "type": "user",
                                                   "message": {"role": "user", "content": "x" * 3_000_000}}))
    (claude / "folder.jsonl").mkdir()

    codex = home / ".codex" / "sessions" / "2026" / "01" / "01"
    codex.mkdir(parents=True)
    rows = b"".join(line(row) for row in codex_rows(project, "codex-good"))
    (codex / "rollout-2026-01-01T00-00-00-codex-good.jsonl").write_bytes(rows + b"".join(BAD_LINES))
    packed = zstandard.ZstdCompressor().compress(rows * 50)
    (codex / "rollout-2026-01-01T00-00-01-truncated.jsonl.zst").write_bytes(packed[: len(packed) // 2])
    (codex / "rollout-2026-01-01T00-00-02-notzstd.jsonl.zst").write_bytes(b"plain text pretending to be zstd")
    (codex / "rollout-2026-01-01T00-00-03-garbage.jsonl.zst").write_bytes(
        zstandard.ZstdCompressor().compress(b"".join(BAD_LINES)))
    (codex / "rollout-2026-01-01T00-00-04-empty.jsonl").write_bytes(b"")

    muse = home / ".local" / "share" / "muse" / "sessions" / "broken"
    muse.mkdir(parents=True)
    (muse / "session.jsonl").write_bytes(b"".join(BAD_LINES))

    cursor = home / "cursor"
    for name, content in (("not-sqlite", b"this is not a database"), ("empty-db", b"")):
        (cursor / name).mkdir(parents=True)
        (cursor / name / "workspace.json").write_text(json.dumps({"folder": project.as_uri()}))
        (cursor / name / "state.vscdb").write_bytes(content)
    (cursor / "bad-rows").mkdir()
    (cursor / "bad-rows" / "workspace.json").write_text("{not json")
    db = sqlite3.connect(cursor / "bad-rows" / "state.vscdb")
    db.execute("CREATE TABLE ItemTable (key TEXT, value BLOB)")
    db.execute("INSERT INTO ItemTable VALUES ('composer.composerData', 'not json')")
    db.execute("INSERT INTO ItemTable VALUES ('aiService.prompts', x'fffefd')")
    db.commit()
    db.close()
    return home, project


def test_capture_survives_damaged_history(tmp_path, monkeypatch):
    home, project = build_home(tmp_path, monkeypatch)
    journal = Journal(tmp_path / "journal.db")
    try:
        captured, report = synchronize(journal, project, home, "auto")
        ids = {c["id"] for c in captured}
        assert "good-chat" in ids and "codex-good" in ids
        assert journal.search(project, "zebracorn")
        found = journal.search_context(project, "zebracorn signing key rotated")
        assert GOOD in json.dumps(found)
        for name in ("resume",):
            response = mcp_response({"id": 1, "method": "tools/call",
                                     "params": {"name": name, "arguments": {"project": str(project)}}}, journal)
            assert "error" not in response, response
        # A second pass over the same damaged files must also finish.
        synchronize(journal, project, home, "auto")
    finally:
        journal.close()


def test_capture_survives_files_changing_underneath(tmp_path, monkeypatch):
    """A chat file that grows, shrinks or is replaced by garbage between captures."""
    home, project = build_home(tmp_path, monkeypatch)
    chat = home / ".claude" / "projects" / "project" / "good-chat.jsonl"
    journal = Journal(tmp_path / "journal.db")
    try:
        synchronize(journal, project, home, "claude")
        chat.write_bytes(chat.read_bytes()[:40])
        synchronize(journal, project, home, "claude")
        chat.write_bytes(bytes(range(256)) * 10)
        synchronize(journal, project, home, "claude")
        chat.unlink()
        synchronize(journal, project, home, "claude")
        assert journal.search(project, "zebracorn"), "history already captured must be kept"
    finally:
        journal.close()


def test_older_journal_is_backed_up_before_upgrade(tmp_path):
    from handoff_sessions import JOURNAL_VERSION
    path = tmp_path / "handoff.db"
    journal = Journal(path)
    journal.db.execute("INSERT INTO events (id, session_id, source, project, ordinal, timestamp, kind, actor, content, metadata) "
                       "VALUES ('e1', 's1', 'claude', 'p', 1, '', 'user_message', 'user', 'keep me safe', '{}')")
    journal.db.execute("PRAGMA user_version=1")
    journal.db.commit()
    journal.close()
    reopened = Journal(path)
    try:
        assert reopened.backup and reopened.backup.parent == tmp_path / "backups"
        copy = sqlite3.connect(reopened.backup)
        assert copy.execute("PRAGMA user_version").fetchone()[0] == 1
        copy.close()
        assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == JOURNAL_VERSION
    finally:
        reopened.close()
    again = Journal(path)  # Already upgraded: no second backup.
    try:
        assert again.backup is None
    finally:
        again.close()
    assert Journal(tmp_path / "new.db").backup is None  # A new journal has nothing to back up.


def test_codex_settings_file_is_edited_when_codex_command_is_missing(tmp_path):
    tomllib = __import__("pytest").importorskip("tomllib")  # Python 3.11 and newer can check the result.
    from handoff import toml_has_server, update_toml_server
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt"\n\n[mcp_servers.node_repl]\ncommand = "node"\n\n[mcp_servers.handoff]\n'
                      'command = "old"\n\n[mcp_servers.handoff.env]\nX = "1"\n\n[projects.\'c:\\\\x\']\ntrust_level = "trusted"\n')
    before = tomllib.loads(config.read_text())
    python = r"C:\Program Files\Python 3.13\python.exe"
    assert update_toml_server(config, [python, "-m", "handoff", "--mcp"])[0]
    data = tomllib.loads(config.read_text())
    assert data["mcp_servers"]["threadline"] == {"command": python, "args": ["-m", "handoff", "--mcp"]}
    assert "handoff" not in data["mcp_servers"] and data["mcp_servers"]["node_repl"] == {"command": "node"}
    assert data["model"] == "gpt" and data["projects"] == before["projects"]
    assert toml_has_server(config) == "threadline"
    assert update_toml_server(config, [python, "-m", "handoff", "--mcp"])[0]  # Running setup twice is harmless.
    assert config.read_text().count("[mcp_servers.threadline]") == 1
    assert update_toml_server(config, None)[0]
    assert "threadline" not in tomllib.loads(config.read_text())["mcp_servers"]
