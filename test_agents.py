"""OpenCode, Kilo, GitHub Copilot CLI and VS Code Copilot Chat: captured per project, damage skipped.

Store layouts follow memex's readers (src/sources/opencode.rs, kilocode.rs, copilot.rs) and cass's
Copilot tests (workspaceStorage/<id>/chatSessions plus workspace.json).
"""
import json
import sqlite3

import pytest

import handoff
from handoff_capture import synchronize
from handoff_sessions import Journal


@pytest.fixture
def home(tmp_path, monkeypatch):
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "OPENCODE_DATA_DIR", "COPILOT_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("handoff_sessions.cursor_root", lambda _: tmp_path / "home" / "cursor")
    monkeypatch.setattr("handoff_sessions.zed_threads_db", lambda _: tmp_path / "home" / "zed" / "threads.db")
    (tmp_path / "project").mkdir()
    (tmp_path / "other").mkdir()
    return tmp_path / "home"


def capture(tmp_path, home, source):
    journal = Journal(tmp_path / "journal.db")
    captured, report = synchronize(journal, tmp_path / "project", home, source)
    return journal, captured, report


def opencode_db(path, project, other):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT NOT NULL, title TEXT,
                              time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);""")
    db.executemany("INSERT INTO session VALUES (?,?,?,?,?,?)", [
        ("ses_main", None, str(project), "Main", 1000, 9000),
        ("ses_child", "ses_main", str(project), "Child", 1100, 9500),
        ("ses_other", None, str(other), "Other", 1000, 2000)])
    db.executemany("INSERT INTO message VALUES (?,?,?,?,?)", [
        ("msg_u", "ses_main", 1000, 1000, json.dumps({"role": "user", "time": {"created": 1000}})),
        ("msg_a", "ses_main", 2000, 2000, json.dumps({"role": "assistant"})),
        ("msg_bad", "ses_main", 2500, 2500, "{not json"),
        ("msg_c", "ses_child", 3000, 3000, json.dumps({"role": "assistant"})),
        ("msg_o", "ses_other", 1000, 1000, json.dumps({"role": "user"}))])
    db.executemany("INSERT INTO part VALUES (?,?,?,?,?)", [
        ("prt_1", "msg_u", "ses_main", 1000, json.dumps({"type": "text", "text": "how do we deploy the wombat service"})),
        ("prt_2", "msg_u", "ses_main", 1001, json.dumps({"type": "text", "text": "hidden context", "synthetic": True})),
        ("prt_3", "msg_a", "ses_main", 2000, json.dumps({"type": "reasoning", "text": "private thinking"})),
        ("prt_4", "msg_a", "ses_main", 2100, json.dumps({"type": "tool", "tool": "read", "callID": "call-1",
                                                          "state": {"status": "completed", "input": {"path": "deploy.sh"}, "output": "kubectl apply"}})),
        ("prt_5", "msg_a", "ses_main", 2200, json.dumps({"type": "text", "text": "Run deploy.sh to ship the wombat service."})),
        ("prt_6", "msg_a", "ses_main", 2300, "garbage"),
        ("prt_7", "msg_a", "ses_main", 2400, json.dumps({"type": "tool", "tool": "bash", "state": {"status": "error", "error": "denied"}})),
        ("prt_8", "msg_bad", "ses_main", 2500, json.dumps({"type": "text", "text": "kept despite bad message row"})),
        ("prt_9", "msg_c", "ses_child", 3000, json.dumps({"type": "text", "text": "child echidna answer"})),
        ("prt_o", "msg_o", "ses_other", 1000, json.dumps({"type": "text", "text": "foreign narwhal text"}))])
    db.commit()
    db.close()


def test_opencode_database(tmp_path, home):
    opencode_db(home / ".local" / "share" / "opencode" / "opencode.db", tmp_path / "project", tmp_path / "other")
    journal, captured, _ = capture(tmp_path, home, "opencode")
    try:
        assert {c["id"] for c in captured} == {"ses_main", "ses_child"}
        rows = [tuple(r) for r in journal.db.execute("SELECT kind, content FROM events WHERE session_id='ses_main' ORDER BY ordinal")]
        kinds = [kind for kind, _ in rows]
        assert kinds.count("tool_call") == 2 and kinds.count("tool_result") == 2
        text = json.dumps(rows)
        assert "wombat service" in text and "kubectl apply" in text and "denied" in text and "kept despite" in text
        assert "hidden context" not in text and "private thinking" not in text
        assert not journal.search(tmp_path / "project", "narwhal")
        chats = {c["id"]: c for c in journal.conversations(tmp_path / "project")}
        assert chats["ses_child"]["parent_id"] == "ses_main"
        _, again = synchronize(journal, tmp_path / "project", home, "opencode")
        assert again["new_events"] == 0
    finally:
        journal.close()


def test_opencode_v2_table_and_kilo(tmp_path, home):
    path = home / ".local" / "share" / "opencode" / "opencode-next.db"
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE session_v2 (id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT NOT NULL, time_created INTEGER, time_updated INTEGER);
        CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER, data TEXT);""")
    db.execute("INSERT INTO session_v2 VALUES ('v2', NULL, ?, 1, 2)", (str(tmp_path / "project"),))
    db.executemany("INSERT INTO session_message VALUES (?,?,?,?,?,?)", [
        ("a", "v2", "user", 1, 1000, json.dumps({"text": "v2 platypus question"})),
        ("b", "v2", "assistant", 2, 2000, json.dumps({"content": [
            {"type": "text", "text": "v2 platypus answer"},
            {"type": "tool", "id": "t1", "name": "grep", "state": {"input": {"q": "x"}, "content": [{"type": "text", "text": "grep hit"}]}},
            {"type": "reasoning", "text": "secret"}]})),
        ("c", "v2", "assistant", 3, 3000, "[broken"),
        ("d", "v2", "shell", 4, 4000, json.dumps({"command": "ls"}))])
    db.commit()
    db.close()
    opencode_db(home / ".local" / "share" / "kilo" / "kilo.db", tmp_path / "project", tmp_path / "other")
    journal, captured, _ = capture(tmp_path, home, "auto")
    try:
        assert {(c["source"], c["id"]) for c in captured} >= {("opencode", "v2"), ("kilo", "ses_main"), ("kilo", "ses_child")}
        text = json.dumps([r[0] for r in journal.db.execute("SELECT content FROM events WHERE session_id='v2'")])
        assert "platypus question" in text and "platypus answer" in text and "grep hit" in text and "secret" not in text
    finally:
        journal.close()


def test_opencode_older_file_layout(tmp_path, home):
    storage = home / ".local" / "share" / "opencode" / "storage"
    (storage / "session" / "proj1").mkdir(parents=True)
    (storage / "session" / "proj1" / "ses_f.json").write_text(json.dumps(
        {"id": "ses_f", "directory": str(tmp_path / "project"), "title": "Files"}))
    (storage / "session" / "proj1" / "ses_x.json").write_text(json.dumps({"id": "ses_x", "directory": str(tmp_path / "other")}))
    (storage / "session" / "proj1" / "broken.json").write_text("{nope")
    (storage / "message" / "ses_f").mkdir(parents=True)
    (storage / "message" / "ses_f" / "msg_1.json").write_text(json.dumps({"id": "msg_1", "role": "user", "time": {"created": 5}}))
    (storage / "message" / "ses_f" / "msg_2.json").write_text("garbage")
    (storage / "part" / "msg_1").mkdir(parents=True)
    (storage / "part" / "msg_1" / "prt_1.json").write_text(json.dumps({"type": "text", "text": "file layout quetzal"}))
    (storage / "part" / "msg_1" / "prt_2.json").write_text("[1,2")
    journal, captured, _ = capture(tmp_path, home, "opencode")
    try:
        assert {c["id"] for c in captured} == {"ses_f"}
        assert journal.search(tmp_path / "project", "quetzal")
    finally:
        journal.close()


def test_copilot_cli(tmp_path, home):
    folder = home / ".copilot" / "session-state" / "11111111-1111-4111-8111-111111111111"
    folder.mkdir(parents=True)
    (folder / "workspace.yaml").write_text(f"# workspace\ncwd: {tmp_path / 'project'}\nnested:\n  cwd: /wrong\n")
    lines = [
        {"type": "session.start", "timestamp": "2026-01-01T00:00:00Z", "data": {"sessionId": "copilot-chat", "context": {"cwd": str(tmp_path / "project")}}},
        {"type": "user.message", "data": {"content": "where is the ocelot config"}},
        {"type": "user.message", "data": {"content": "injected memory text", "source": "memory"}},
        {"type": "tool.execution_start", "data": {"toolName": "view", "toolCallId": "c1", "arguments": {"path": "ocelot.toml"}}},
        {"type": "tool.execution_complete", "data": {"toolCallId": "c1", "result": {"content": "port = 8080"}}},
        {"type": "tool.execution_complete", "data": {"toolCallId": "c2", "error": {"message": "permission denied"}}},
        {"type": "assistant.message", "data": {"content": "The ocelot config is ocelot.toml."}},
        {"type": "assistant.message", "data": None},
        {"type": "brand.new.event", "data": [1]},
    ]
    (folder / "events.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines) + "not json\n{\"cut")
    other = home / ".copilot" / "session-state" / "22222222-2222-4222-8222-222222222222"
    other.mkdir()
    (other / "events.jsonl").write_text(json.dumps({"type": "session.start", "data": {"context": {"cwd": str(tmp_path / "other")}}}) + "\n")
    journal, captured, _ = capture(tmp_path, home, "copilot")
    try:
        assert {c["id"] for c in captured} == {"copilot-chat"}
        rows = [tuple(r) for r in journal.db.execute("SELECT kind, content FROM events WHERE session_id='copilot-chat' ORDER BY ordinal")]
        assert [kind for kind, _ in rows] == ["user_message", "tool_call", "tool_result", "tool_result", "assistant_message"]
        text = json.dumps(rows)
        assert "port = 8080" in text and "permission denied" in text and "injected memory" not in text
    finally:
        journal.close()


def test_vscode_copilot_chat(tmp_path, home):
    from handoff_agents import vscode_user_dirs
    storage = vscode_user_dirs(home)[0] / "workspaceStorage"
    mine, theirs = storage / "ws-1", storage / "ws-2"
    for workspace, folder in ((mine, tmp_path / "project"), (theirs, tmp_path / "other")):
        (workspace / "chatSessions").mkdir(parents=True)
        (workspace / "workspace.json").write_text(json.dumps({"folder": folder.as_uri()}))
    chat = {"version": 3, "sessionId": "vs-chat", "creationDate": 1700000000000, "lastMessageDate": 1700000100000,
            "requests": [
                {"message": {"text": "explain the badger cache"}, "timestamp": 1700000000000,
                 "response": [{"value": "The badger cache "}, {"kind": "markdownContent", "content": {"value": "keeps hot keys."}},
                              {"kind": "toolInvocationSerialized", "toolId": "search", "invocationMessage": {"value": "Searched badger"}},
                              {"kind": "unknownKind"}, None]},
                "not a request",
                {"message": None, "response": "not a list"}]}
    (mine / "chatSessions" / "vs-chat.json").write_text(json.dumps(chat))
    (mine / "chatSessions" / "newer.jsonl").write_text('{"kind":0}\n')
    (mine / "chatSessions" / "damaged.json").write_text("{half")
    (theirs / "chatSessions" / "foreign.json").write_text(json.dumps({**chat, "sessionId": "foreign",
                                                                     "requests": [{"message": {"text": "foreign narwhal"}}]}))
    journal, captured, report = capture(tmp_path, home, "vscode")
    try:
        assert {c["id"] for c in captured} == {"vs-chat", "newer", "damaged"}
        text = json.dumps([r[0] for r in journal.db.execute("SELECT content FROM events WHERE session_id='vs-chat'")])
        assert "badger cache" in text and "keeps hot keys." in text and "Searched badger" in text
        assert not journal.search(tmp_path / "project", "narwhal")
        newer = next(c for c in report["conversations"] if c.get("session_id") == "newer")
        assert any("newer .jsonl format" in gap for gap in newer["gaps"])
    finally:
        journal.close()


def test_damaged_stores_do_not_stop_capture(tmp_path, home):
    for path in (home / ".local" / "share" / "opencode" / "opencode.db", home / ".local" / "share" / "kilo" / "kilo.db"):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not a database")
    claude = home / ".claude" / "projects" / "p"
    claude.mkdir(parents=True)
    (claude / "good.jsonl").write_text(json.dumps({"sessionId": "good", "cwd": str(tmp_path / "project"), "type": "user",
                                                   "message": {"role": "user", "content": "still captured"}}) + "\n")
    journal, captured, report = capture(tmp_path, home, "auto")
    try:
        assert "good" in {c["id"] for c in captured}
        assert {g.get("source") for g in report["gaps"] if isinstance(g, dict)} >= {"opencode", "kilo"}
    finally:
        journal.close()


def test_setup_connects_new_agents_without_touching_other_settings(tmp_path, home, monkeypatch):
    real_which = __import__("shutil").which
    monkeypatch.setattr("handoff.shutil.which", lambda name: real_which(name) if name == "git" else None)
    monkeypatch.setattr("handoff.zed_installed", lambda _: False)
    from handoff_agents import vscode_user_dirs
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "opencode.json").write_text('{\n  // my theme\n  "theme": "tokyonight",\n}\n')
    (home / ".copilot").mkdir()
    vscode_user_dirs(home)[0].mkdir(parents=True)
    assert handoff.configure("setup", home) == 0
    opencode = (home / ".config" / "opencode" / "opencode.json").read_text()
    assert "// my theme" in opencode and '"threadline"' in opencode and '"type": "local"' in opencode
    copilot = json.loads((home / ".copilot" / "mcp-config.json").read_text())
    assert copilot["mcpServers"]["threadline"]["args"] == ["-m", "handoff", "--mcp"]
    from test_zed import loose
    vscode = loose((vscode_user_dirs(home)[0] / "mcp.json").read_text())
    assert vscode["servers"]["threadline"]["type"] == "stdio"
    assert handoff.configure("uninstall", home) in (0, 1)
    assert '"threadline"' not in (home / ".config" / "opencode" / "opencode.json").read_text()


def test_settings_edits_stay_strict_json(tmp_path):
    """Adding and removing Threadline never leaves a trailing comma behind."""
    path = tmp_path / "mcp-config.json"
    entry = {"command": "py", "args": []}
    for start in ("", "{}", '{"mcpServers": {}}', '{"mcpServers": {"a": {"command": "x"}}}', '{"other": 1}',
                  '{"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}, "z": [1, 2]}'):
        path.write_text(start)
        before = json.loads(start) if start else {}
        assert handoff.update_jsonc_server(path, "mcpServers", entry)[0]
        after = json.loads(path.read_text())
        assert after["mcpServers"]["threadline"] == entry
        assert handoff.update_jsonc_server(path, "mcpServers", None)[0]
        removed = json.loads(path.read_text())
        removed["mcpServers"] = {k: v for k, v in removed["mcpServers"].items()}
        assert removed == {**before, "mcpServers": before.get("mcpServers", {})}
