"""Zed agent threads: captured per project from Zed's threads.db, and Threadline registered in its settings.

The database layout and thread JSON follow Zed's own code (crates/agent/src/db.rs, thread.rs and
legacy_thread.rs): rows hold zstd-compressed JSON, and folder_paths lists a thread's folders one per line.
"""
import json
import sqlite3

import zstandard

from handoff import update_zed_settings
from handoff_capture import synchronize
from handoff_sessions import Journal

SCHEMA = """CREATE TABLE threads (id TEXT PRIMARY KEY, summary TEXT NOT NULL, updated_at TEXT NOT NULL,
    data_type TEXT NOT NULL, data BLOB NOT NULL, parent_id TEXT, folder_paths TEXT, folder_paths_order TEXT, created_at TEXT)"""

CURRENT = {
    "version": "0.3.0", "title": "Rotate keys", "updated_at": "2026-03-01T10:00:00Z",
    "messages": [
        {"User": {"id": "u1", "content": [
            {"Text": "How do we rotate the zebracorn signing key?"},
            {"Mention": {"uri": "file:///repo/rotate.sh", "content": "#!/bin/sh\necho secret-file-body"}},
            {"Image": {"source": "aGVsbG8=", "size": None}}]}},
        {"Agent": {"content": [
            {"Thinking": {"text": "private reasoning", "signature": None}},
            {"Text": "Run rotate.sh, then restart the signer."},
            {"ToolUse": {"id": "t1", "name": "terminal", "raw_input": "{\"command\":\"ls\"}",
                         "input": {"command": "ls"}, "is_input_complete": True, "thought_signature": None}},
            {"RedactedThinking": "xxx"}],
            "tool_results": {"t1": {"tool_use_id": "t1", "tool_name": "terminal", "is_error": False,
                                    "content": "rotate.sh\nsigner.py", "output": None}},
            "reasoning_details": None}},
        "Resume",
        {"Compaction": {"Summary": "Earlier work summarised"}},
        {"FutureMessageKind": {"anything": [1, 2]}},
        {"User": None},
    ],
}

LEGACY = {
    "version": "0.2.0", "summary": "Old thread", "updated_at": "2025-01-01T00:00:00Z",
    "messages": [
        {"id": 0, "role": "user", "segments": [{"type": "text", "text": "legacy quokka question"}],
         "tool_uses": [], "tool_results": []},
        {"id": 1, "role": "assistant", "segments": [{"type": "thinking", "text": "hmm"}, {"type": "text", "text": "legacy quokka answer"}],
         "tool_uses": [{"id": "lt1", "name": "grep", "input": {"regex": "quokka"}}],
         "tool_results": [{"tool_use_id": "lt1", "is_error": False, "content": "found quokka", "output": None}]},
    ],
}


def pack(value):
    return zstandard.ZstdCompressor(level=3).compress(json.dumps(value).encode())


def build(tmp_path, monkeypatch):
    home, project, other = tmp_path / "home", tmp_path / "project", tmp_path / "other"
    project.mkdir(parents=True)
    other.mkdir()
    path = home / "zed" / "threads" / "threads.db"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr("handoff_sessions.zed_threads_db", lambda _: path)
    monkeypatch.setattr("handoff_sessions.cursor_root", lambda _: home / "cursor")
    db = sqlite3.connect(path)
    db.execute(SCHEMA)
    rows = [
        ("current", "Rotate keys", "2026-03-01T10:00:00Z", "zstd", pack(CURRENT), None, str(project), "0", "2026-03-01T09:00:00Z"),
        ("legacy", "Old thread", "2025-01-01T00:00:00Z", "json", json.dumps(LEGACY).encode(), None, str(project), "0", None),
        ("child", "Sub-agent", "2026-03-01T10:05:00Z", "zstd", pack({**CURRENT, "messages": [
            {"User": {"id": "c1", "content": [{"Text": "child pangolin task"}]}}]}), "current", str(project), "0", None),
        ("multi", "Two folders", "2026-03-02T00:00:00Z", "zstd", pack({**CURRENT, "messages": [
            {"User": {"id": "m1", "content": [{"Text": "multi folder axolotl"}]}}]}), None, f"{other}\n{project}", "1,0", None),
        ("elsewhere", "Other project", "2026-03-03T00:00:00Z", "zstd", pack({**CURRENT, "messages": [
            {"User": {"id": "e1", "content": [{"Text": "foreign narwhal content"}]}}]}), None, str(other), "0", None),
        ("broken", "Damaged", "2026-03-04T00:00:00Z", "zstd", b"not zstd at all", None, str(project), "0", None),
        ("not-object", "List", "2026-03-04T00:00:00Z", "json", b"[1, 2, 3]", None, str(project), "0", None),
        ("no-folder", "No folder", "2026-03-05T00:00:00Z", "zstd", pack(CURRENT), None, None, None, None),
    ]
    db.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()
    return home, project, path


def test_zed_threads_are_captured_for_their_project(tmp_path, monkeypatch):
    home, project, _ = build(tmp_path, monkeypatch)
    journal = Journal(tmp_path / "journal.db")
    try:
        captured, report = synchronize(journal, project, home, "zed")
        assert {c["id"] for c in captured} == {"current", "legacy", "child", "multi", "broken", "not-object"}
        events = {e["content"] for e in journal.search(project, "zebracorn")}
        assert any("rotate the zebracorn signing key" in text for text in events)
        assert journal.search(project, "quokka") and journal.search(project, "axolotl") and journal.search(project, "pangolin")
        assert not journal.search(project, "narwhal"), "another project's thread must not be captured"
        stored = list(journal.db.execute("SELECT kind, content, metadata FROM events WHERE session_id='current' ORDER BY ordinal"))
        kinds = [row[0] for row in stored]
        assert kinds == ["user_message", "assistant_message", "tool_call", "tool_result"]
        user, agent, call, result = (row[1] for row in stored)
        assert "@file:///repo/rotate.sh" in user and "secret-file-body" not in user
        assert agent == "Run rotate.sh, then restart the signer." and "private reasoning" not in agent
        assert json.loads(call) == {"command": "ls"} and "signer.py" in result
        assert json.loads(stored[2][2])["name"] == "terminal"
        chats = {c["id"]: c for c in journal.conversations(project)}
        assert chats["child"]["parent_id"] == "current"
        broken = next(c for c in report["conversations"] if c.get("session_id") == "broken")
        assert any("could not be read" in gap for gap in broken["gaps"])
        _, again = synchronize(journal, project, home, "zed")
        assert again["new_events"] == 0
    finally:
        journal.close()


def test_damaged_zed_database_does_not_stop_capture(tmp_path, monkeypatch):
    home, project, path = build(tmp_path, monkeypatch)
    path.write_bytes(b"this is not a database")
    journal = Journal(tmp_path / "journal.db")
    try:
        _, report = synchronize(journal, project, home, "auto")
        assert any(gap.get("source") == "zed" for gap in report["gaps"] if isinstance(gap, dict))
    finally:
        journal.close()


SETTINGS = """// Zed settings
//
// See https://zed.dev/docs/configuring-zed
{
  "theme": {
    "mode": "dark", // keep this comment
  },
  /* agent profiles reuse the context_servers name */
  "agent": {
    "profiles": {"write": {"context_servers": {"container-use": {"tools": {}}}}},
  },
}
"""


def loose(text):
    """Parse Zed's settings the forgiving way Zed does, to check the result."""
    import re
    text = re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.S)
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", text))


def test_zed_settings_keep_comments_and_other_servers(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(SETTINGS)
    entry = {"command": "/usr/bin/python3", "args": ["-m", "handoff", "--mcp"]}
    assert update_zed_settings(path, entry)[0]
    text = path.read_text()
    assert "// keep this comment" in text and "/* agent profiles reuse" in text
    data = loose(text)
    assert data["context_servers"] == {"threadline": entry}
    assert data["agent"]["profiles"]["write"]["context_servers"] == {"container-use": {"tools": {}}}
    assert update_zed_settings(path, entry)[1].endswith("(already set)")
    assert path.read_text() == text
    newer = {"command": r"C:\Program Files\Python\python.exe", "args": ["-m", "handoff", "--mcp"]}
    assert update_zed_settings(path, newer)[0]
    assert loose(path.read_text())["context_servers"] == {"threadline": newer}
    assert update_zed_settings(path, None)[0]
    assert loose(path.read_text())["context_servers"] == {}


def test_zed_settings_join_existing_servers_and_handle_missing_or_broken_files(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"context_servers": {"other": {"command": "x"}}}')
    entry = {"command": "py", "args": []}
    assert update_zed_settings(path, entry)[0]
    assert loose(path.read_text())["context_servers"] == {"threadline": entry, "other": {"command": "x"}}

    missing = tmp_path / "new" / "settings.json"
    missing.parent.mkdir()
    assert update_zed_settings(missing, entry)[0]
    assert loose(missing.read_text()) == {"context_servers": {"threadline": entry}}

    broken = tmp_path / "broken.json"
    broken.write_text('{"theme": {"mode": "dark"')
    ok, detail = update_zed_settings(broken, entry)
    assert not ok and "Add this to it" in detail
    assert broken.read_text() == '{"theme": {"mode": "dark"', "a file Threadline cannot parse is left alone"
