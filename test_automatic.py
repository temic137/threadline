"""Nothing to run after setup: chats pick up earlier work by themselves, and search sees the newest chats."""
import io
import json
import sys

import pytest

import handoff
import handoff_sessions
from handoff_sessions import Journal, mcp_response

PROJECT_CHAT = "How do we rotate the zebracorn signing key?"


def claude_chat(home, project, session="earlier", text=PROJECT_CHAT):
    folder = home / ".claude" / "projects" / "project"
    folder.mkdir(parents=True, exist_ok=True)
    rows = [{"sessionId": session, "cwd": str(project), "type": "user", "timestamp": "2026-01-01T00:00:00Z",
             "message": {"role": "user", "content": text}},
            {"sessionId": session, "cwd": str(project), "type": "assistant", "timestamp": "2026-01-01T00:00:05Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "Run rotate.sh."}]}}]
    (folder / f"{session}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (tmp_path / "project").mkdir()
    monkeypatch.setattr("handoff_sessions.cursor_root", lambda _: home / "cursor")
    monkeypatch.setattr("handoff_sessions.zed_threads_db", lambda _: home / "zed" / "threads.db")
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "OPENCODE_DATA_DIR", "COPILOT_HOME", "CLINE_DATA_DIR", "CLINE_DIR"):
        monkeypatch.delenv(name, raising=False)
    return home


def test_claude_hook_is_added_once_kept_beside_other_hooks_and_removed(tmp_path):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    other = {"matcher": "startup", "hooks": [{"type": "command", "command": "echo hi"}]}
    path.write_text(json.dumps({"model": "x", "hooks": {"SessionStart": [other], "Stop": [{"hooks": []}]}}))
    assert handoff.update_claude_hook(path, "/usr/bin/python3")[0]
    data = json.loads(path.read_text())
    groups = data["hooks"]["SessionStart"]
    assert groups[0] == other and len(groups) == 2 and data["model"] == "x" and "Stop" in data["hooks"]
    assert groups[1]["hooks"][0]["command"] == '"/usr/bin/python3" -m handoff session-start'
    assert handoff.claude_hook_installed(path)
    text = path.read_text()
    assert handoff.update_claude_hook(path, "/usr/bin/python3")[1].endswith("(already set)")
    assert path.read_text() == text, "running setup twice changes nothing"
    assert handoff.update_claude_hook(path, "/new/python")[0]
    assert len(json.loads(path.read_text())["hooks"]["SessionStart"]) == 2, "a newer Python replaces the old hook"
    assert handoff.update_claude_hook(path, None)[0]
    assert json.loads(path.read_text())["hooks"]["SessionStart"] == [other]
    assert not handoff.claude_hook_installed(path)


def test_claude_hook_leaves_unreadable_settings_alone(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"model": "x",')
    ok, detail = handoff.update_claude_hook(path, "/usr/bin/python3")
    assert not ok and "left unchanged" in detail and path.read_text() == '{"model": "x",'
    path.write_text('{"hooks": {"SessionStart": "odd"}}')
    assert not handoff.update_claude_hook(path, "/usr/bin/python3")[0]
    fresh = tmp_path / "new" / "settings.json"
    assert handoff.update_claude_hook(fresh, "/usr/bin/python3")[0] and handoff.claude_hook_installed(fresh)


def test_instruction_note_keeps_the_users_own_text(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("# My rules\n\nUse tabs.\n")
    assert handoff.update_instructions(path, True)[0]
    text = path.read_text()
    assert text.startswith("# My rules\n\nUse tabs.\n\n") and handoff.AUTO_START_NOTE in text
    assert handoff.update_instructions(path, True)[1].endswith("(already set)") and path.read_text() == text
    assert handoff.update_instructions(path, False)[0]
    assert path.read_text() == "# My rules\n\nUse tabs.\n"
    created = tmp_path / "new" / "GEMINI.md"
    assert handoff.update_instructions(created, True)[0] and created.exists()
    assert handoff.update_instructions(created, False)[0] and not created.exists(), "a file holding only the note is removed"
    assert handoff.update_instructions(created, False)[0], "removing twice is harmless"


def test_setup_turns_on_pickup_and_says_to_restart(tmp_path, home, monkeypatch, capsys):
    monkeypatch.setattr("handoff.shutil.which", lambda name: None)
    monkeypatch.setattr("handoff.zed_installed", lambda _: False)
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir()
    (home / ".codex" / "AGENTS.md").write_text("Keep answers short.\n")
    (home / ".cursor").mkdir()
    assert handoff.configure("setup", home) == 0
    out = capsys.readouterr().out
    assert handoff.claude_hook_installed(home / ".claude" / "settings.json")
    agents = (home / ".codex" / "AGENTS.md").read_text()
    assert agents.startswith("Keep answers short.\n") and handoff.AUTO_START_NOTE in agents
    assert not (home / ".gemini").exists(), "agents that are not installed are not touched"
    assert "Last step: quit and reopen" in out and "Codex" in out and "Cursor" in out
    assert 'Cursor: ask "where did we stop?"' in out
    assert handoff.configure("uninstall", home) == 0
    assert not handoff.claude_hook_installed(home / ".claude" / "settings.json")
    assert (home / ".codex" / "AGENTS.md").read_text() == "Keep answers short.\n"


def run_hook(monkeypatch, capsys, request):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(request.encode())))
    assert handoff.main(["session-start"]) == 0
    return capsys.readouterr().out


def test_new_claude_chat_gets_a_brief_of_earlier_work(tmp_path, home, monkeypatch, capsys):
    project = tmp_path / "project"
    claude_chat(home, project)
    (home / ".handoff").mkdir(parents=True)
    monkeypatch.setattr(handoff.os.path, "expanduser", lambda _: str(home))
    out = run_hook(monkeypatch, capsys, json.dumps({"cwd": str(project), "source": "startup", "session_id": "new"}))
    assert out.startswith("Threadline: earlier work") and "zebracorn" in out
    # No history for this folder, a home folder, bad input or a broken history file: silence, never an error.
    (tmp_path / "empty").mkdir()
    assert run_hook(monkeypatch, capsys, json.dumps({"cwd": str(tmp_path / "empty")})) == ""
    assert run_hook(monkeypatch, capsys, json.dumps({"cwd": str(home)})) == ""
    assert run_hook(monkeypatch, capsys, "not json") == ""
    assert run_hook(monkeypatch, capsys, json.dumps({"cwd": str(tmp_path / "missing")})) == ""
    (home / ".handoff" / "handoff.db").write_bytes(b"not a database")
    assert run_hook(monkeypatch, capsys, json.dumps({"cwd": str(project)})) == ""


def test_hook_does_nothing_before_threadline_was_ever_set_up(tmp_path, home, monkeypatch, capsys):
    project = tmp_path / "project"
    claude_chat(home, project)
    monkeypatch.setattr(handoff.os.path, "expanduser", lambda _: str(home))
    assert run_hook(monkeypatch, capsys, json.dumps({"cwd": str(project)})) == ""
    assert not (home / ".handoff").exists()


def test_search_saves_new_chats_first_but_only_once_a_minute(tmp_path, home, monkeypatch):
    project = tmp_path / "project"
    monkeypatch.setattr(handoff_sessions.Path, "home", classmethod(lambda cls: home))
    journal = Journal(tmp_path / "journal.db")
    journal.auto_refresh = True

    def search(word):
        response = mcp_response({"id": 1, "method": "tools/call",
                                 "params": {"name": "search_context", "arguments": {"project": str(project), "query": word}}}, journal)
        return response["result"]["structuredContent"]["results"]
    try:
        claude_chat(home, project)
        assert search("zebracorn"), "a chat written a moment ago is found without asking for a capture"
        claude_chat(home, project, "later", "what about the platypus cache")
        assert not search("platypus"), "a second search within a minute does not scan again"
        journal._synced.clear()
        assert search("platypus")
    finally:
        journal.close()


def test_bare_threadline_shows_status_not_a_prompt(tmp_path, home, monkeypatch, capsys):
    monkeypatch.setattr(handoff.os.path, "expanduser", lambda _: str(home))
    monkeypatch.setattr("handoff.shutil.which", lambda name: None)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("status must not ask anything"))
    monkeypatch.chdir(tmp_path / "project")
    assert handoff.main([]) == 1
    assert "not set up yet" in capsys.readouterr().out
    (home / ".handoff").mkdir(parents=True)
    Journal(home / ".handoff" / "handoff.db").close()
    handoff.main([])
    out = capsys.readouterr().out
    assert out.startswith("Threadline is") and "Connected to:" in out and "This folder:" in out
    assert handoff.main(["--help"]) == 0 and "threadline setup" in capsys.readouterr().out


def test_status_reads_agent_settings_files_instead_of_waiting_on_their_commands(tmp_path, monkeypatch):
    import handoff
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text("[mcp_servers.threadline]\ncommand = 'python'\n")
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"threadline": {"command": "python"}}}))
    monkeypatch.setattr(handoff.shutil, "which", lambda name: f"/usr/bin/{name}" if name in ("codex", "claude") else None)
    asked = []
    monkeypatch.setattr(handoff, "run_setup_command", lambda command: asked.append(command) or (False, ""))
    rows, _ = handoff.health(home, home / ".handoff" / "handoff.db")
    states = {name: (state, detail) for name, state, detail in rows}
    assert states["Codex"][0] == "OK" and states["Claude Code"][0] == "OK"
    assert asked == [], "no agent command is run when its settings file already shows Threadline"
    (home / ".claude.json").write_text("{}")
    handoff.health(home, home / ".handoff" / "handoff.db")
    assert any(command[0] == "/usr/bin/claude" for command in asked), "otherwise the agent's own command still decides"
