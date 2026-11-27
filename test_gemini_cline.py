"""Gemini CLI chats and Cline, Roo Code and Kilo Code tasks.

Gemini's layout follows gemini-cli (packages/core/src/services/chatRecordingService.ts, config/storage.ts,
config/projectRegistry.ts); Cline's follows cline (apps/vscode/src/core/storage/disk.ts,
src/sdk/legacy-state-reader.ts, src/shared/HistoryItem.ts).
"""
import hashlib
import json

import pytest

import handoff
from handoff_capture import synchronize
from handoff_sessions import Journal


@pytest.fixture
def home(tmp_path, monkeypatch):
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "OPENCODE_DATA_DIR", "COPILOT_HOME", "CLINE_DATA_DIR", "CLINE_DIR"):
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


def contents(journal, session_id):
    return [tuple(r) for r in journal.db.execute(
        "SELECT kind, content FROM events WHERE session_id=? ORDER BY ordinal", (session_id,))]


def jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in records))


def test_gemini_log_is_replayed(tmp_path, home):
    project = tmp_path / "project"
    slug = home / ".gemini" / "tmp" / "project"
    slug.mkdir(parents=True)
    (slug / ".project_root").write_text(str(project))
    jsonl(slug / "chats" / "session-2026-01-01T10-00-abcd1234.jsonl", [
        {"sessionId": "gem-1", "projectHash": "x", "startTime": "2026-01-01T10:00:00Z", "kind": "main"},
        {"id": "m1", "timestamp": "2026-01-01T10:00:01Z", "type": "user", "content": [{"text": "how do we migrate the kestrel table"}]},
        {"id": "m2", "timestamp": "2026-01-01T10:00:02Z", "type": "gemini", "content": [{"text": "draft", "thought": True}, {"text": "first draft"}],
         "toolCalls": [{"id": "t1", "name": "run_shell_command", "args": {"command": "ls migrations"}, "status": "executing", "timestamp": "x"}]},
        {"$patch": {"id": "m2", "content": [{"text": "Run the kestrel migration with alembic."}],
                    "toolCalls": [{"id": "t1", "result": [{"functionResponse": {"name": "run_shell_command", "response": {"output": "001_kestrel.py"}}}]}]}},
        {"id": "m3", "timestamp": "x", "type": "user", "content": "a question later rewound"},
        {"$rewindTo": "m3"},
        {"id": "m4", "timestamp": "x", "type": "info", "content": "Model switched"},
        {"$set": {"summary": "Kestrel migration"}},
        "not json",
        {"id": "m5", "type": "gemini", "content": None, "toolCalls": "not a list"},
        '{"cut off',
    ])
    jsonl(slug / "chats" / "gem-1" / "child-1.jsonl", [
        {"sessionId": "child-1", "projectHash": "x", "kind": "subagent"},
        {"id": "c1", "type": "user", "timestamp": "x", "content": "sub-agent heron task"}])
    legacy = home / ".gemini" / "tmp" / hashlib.sha256(str(project).encode()).hexdigest() / "chats"
    legacy.mkdir(parents=True)
    (legacy / "session-old.json").write_text(json.dumps({"sessionId": "gem-old", "projectHash": "h", "startTime": "x", "lastUpdated": "y",
                                                         "messages": [{"id": "o1", "type": "user", "timestamp": "x", "content": "legacy ibis question"}]}))
    elsewhere = home / ".gemini" / "tmp" / "other"
    elsewhere.mkdir(parents=True)
    (elsewhere / ".project_root").write_text(str(tmp_path / "other"))
    jsonl(elsewhere / "chats" / "session-x.jsonl", [{"sessionId": "gem-x", "projectHash": "z"},
                                                    {"id": "x1", "type": "user", "timestamp": "x", "content": "foreign narwhal"}])
    (slug / "chats" / "damaged.json").write_text("{half")
    journal, captured, _ = capture(tmp_path, home, "gemini")
    try:
        assert {c["id"] for c in captured} >= {"gem-1", "child-1", "gem-old"}
        assert "gem-x" not in {c["id"] for c in captured}
        rows = contents(journal, "gem-1")
        assert [kind for kind, _ in rows] == ["user_message", "assistant_message", "tool_call", "tool_result"]
        text = json.dumps(rows)
        assert "alembic" in text and "001_kestrel.py" in text
        assert "first draft" not in text and "rewound" not in text and "Model switched" not in text and '"draft' not in text
        assert journal.search(project, "ibis")
        chats = {c["id"]: c for c in journal.conversations(project)}
        assert chats["child-1"]["parent_id"] == "gem-1"
    finally:
        journal.close()


def cline_task(root, task_id, messages):
    folder = root / "tasks" / task_id
    folder.mkdir(parents=True)
    (folder / "api_conversation_history.json").write_text(json.dumps(messages) if not isinstance(messages, str) else messages)
    return folder


def test_cline_family_tasks(tmp_path, home):
    from handoff_agents import vscode_user_dirs
    project, other = tmp_path / "project", tmp_path / "other"
    cline = vscode_user_dirs(home)[0] / "globalStorage" / "saoudrizwan.claude-dev"
    (cline / "state").mkdir(parents=True)
    (cline / "state" / "taskHistory.json").write_text(json.dumps([
        {"id": "task-1", "ts": 1, "task": "x", "cwdOnTaskInitialization": str(project)},
        {"id": "task-other", "ts": 1, "task": "y", "cwdOnTaskInitialization": str(other)}]))
    cline_task(cline, "task-1", [
        {"role": "user", "content": [{"type": "text", "text": "<task>add a gecko endpoint</task>"},
                                     {"type": "text", "text": "<environment_details>\n# Current Working Directory (/x) Files\nsecret.txt\n</environment_details>"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "I'll add the gecko endpoint."},
                                          {"type": "tool_use", "id": "tu1", "name": "write_to_file", "input": {"path": "gecko.py"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": [{"type": "text", "text": "file written"}]},
                                     {"type": "image", "source": {"data": "aGk="}}]},
        {"role": "assistant", "content": "Done: plain string content."},
        "not a message", {"role": "system", "content": "ignored"}, {"role": "user", "content": None}])
    cline_task(cline, "task-other", [{"role": "user", "content": "foreign narwhal"}])
    # No task list entry: the folder comes from the environment details Cline writes into the first message.
    cline_task(cline, "task-unlisted", [{"role": "user", "content": [
        {"type": "text", "text": "fix the lemur bug"},
        {"type": "text", "text": f"<environment_details>\n# Current Working Directory ({project}) Files\n</environment_details>"}]}])
    cline_task(cline, "task-broken", "{not json")
    roo = vscode_user_dirs(home)[0] / "globalStorage" / "rooveterinaryinc.roo-cline"
    folder = cline_task(roo, "roo-1", [{"role": "user", "content": "roo tapir question"}])
    (folder / "history_item.json").write_text(json.dumps({"id": "roo-1", "workspace": str(project)}))
    kilo = home / ".cline" / "data"
    (kilo / "state").mkdir(parents=True)
    (kilo / "state" / "taskHistory.json").write_text(json.dumps([{"id": "cli-1", "cwdOnTaskInitialization": str(project)}]))
    cline_task(kilo, "cli-1", [{"role": "user", "content": "cline cli yak question"}])
    journal, captured, _ = capture(tmp_path, home, "auto")
    try:
        ids = {(c["source"], c["id"]) for c in captured}
        assert {("cline", "task-1"), ("cline", "task-unlisted"), ("roo", "roo-1"), ("cline", "cli-1")} <= ids
        assert ("cline", "task-other") not in ids
        rows = contents(journal, "task-1")
        text = json.dumps(rows)
        assert "gecko endpoint" in text and "file written" in text and "plain string content" in text
        assert "secret.txt" not in text and "environment_details" not in text and "ignored" not in text
        assert [kind for kind, _ in rows].count("tool_call") == 1
        for word in ("lemur", "tapir", "yak"):
            assert journal.search(project, word), word
        assert not journal.search(project, "narwhal")
    finally:
        journal.close()


def test_setup_connects_gemini_and_cline(tmp_path, home, monkeypatch):
    real_which = __import__("shutil").which
    monkeypatch.setattr("handoff.shutil.which", lambda name: real_which(name) if name == "git" else None)
    monkeypatch.setattr("handoff.zed_installed", lambda _: False)
    (home / ".gemini").mkdir(parents=True)
    (home / ".gemini" / "settings.json").write_text('{\n  // keep\n  "theme": "Default"\n}\n')
    (home / ".cline" / "data").mkdir(parents=True)
    assert handoff.configure("setup", home) == 0
    gemini = (home / ".gemini" / "settings.json").read_text()
    assert "// keep" in gemini and '"threadline"' in gemini
    cline = json.loads((home / ".cline" / "data" / "settings" / "cline_mcp_settings.json").read_text())
    assert cline["mcpServers"]["threadline"]["disabled"] is False
