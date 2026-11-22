"""Project handoff end-to-end using isolated histories and destination settings."""
import json
import subprocess
import sys

import pytest

import handoff
import handoff_typellm as provider
from handoff_sessions import Journal, mcp_response
from test_capture_acceptance import codex
from test_decisions import seed


def test_transfer_all_chats_archive_destination_and_real_mcp(tmp_path, monkeypatch):
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    for sid in ("main", "second", "child"):
        codex(home / f".codex/sessions/{sid}.jsonl", project, sid, [f"Please finish {sid}"])
    child_path = home / ".codex/sessions/child.jsonl"
    records = [json.loads(line) for line in child_path.read_text().splitlines()]
    records[0]["payload"]["source"] = {"subagent": {"spawn": {"parent_thread_id": "main"}}}
    child_path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    codex(home / ".codex/sessions/foreign.jsonl", tmp_path / "foreign", "foreign", ["private other project"])
    config = home / ".cursor/mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcpServers": {"unrelated": {"command": "keep"}}}))
    db = tmp_path / "journal.db"
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("Must not select chats"))
    assert handoff.transfer_project(project, "cursor", home, db) == 0
    index = json.loads((project / ".handoff/conversations.json").read_text())
    assert {c["id"] for c in index} == {"main", "second", "child"}
    child = next(c for c in index if c["id"] == "child")
    assert child["relationship"] == "child" and child["parent_id"] == "main"
    archive = (project / ".handoff/capture_records.jsonl").read_text()
    assert "Please finish main" in archive and "private other project" not in archive
    report = json.loads((project / ".handoff/transfer.json").read_text())
    assert report["events"] == 3 and report["integration_configured"]
    assert report["destination_loaded"] == "not_verified"
    settings = json.loads(config.read_text())
    assert settings["mcpServers"]["unrelated"] == {"command": "keep"}
    assert config.with_name("mcp.json.handoff.bak").exists()
    entry = settings["mcpServers"]["threadline"]
    request = {"id": 1, "method": "tools/call", "params": {"name": "resume",
               "arguments": {"project": str(project), "refresh": False}}}
    process = subprocess.run([entry["command"], *entry["args"]], input=json.dumps(request)+"\n",
                             capture_output=True, text=True, cwd=tmp_path, timeout=30)
    assert process.returncode == 0, process.stderr
    context = json.loads(process.stdout)["result"]["structuredContent"]
    assert context["scope"] == "entire_project" and len(context["conversations"]) == 3
    # A source disappearing later must not remove previously captured history from transfer.
    (home / ".codex/sessions/second.jsonl").unlink()
    assert handoff.transfer_project(project, "cursor", home, db) == 0
    assert len(json.loads((project / ".handoff/conversations.json").read_text())) == 3
    coverage = json.loads((project / ".handoff/coverage.json").read_text())
    assert any(c["session_id"] == "second" and c["status"] == "unavailable" for c in coverage["conversations"])


def test_transfer_failure_keeps_bundle_and_config(tmp_path):
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    codex(home / ".codex/sessions/a.jsonl", project, "A", ["Please finish this"])
    config = home / ".cursor/mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text("broken config")
    assert handoff.transfer_project(project, "cursor", home, tmp_path / "j.db") == 1
    assert config.read_text() == "broken config"
    assert (project / "HANDOFF.md").exists()
    assert not json.loads((project / ".handoff/transfer.json").read_text())["integration_configured"]


def test_project_extraction_shared_budget_empty_chat_and_cache(tmp_path, monkeypatch):
    j = Journal(tmp_path / "j.db")
    try:
        seed(j, tmp_path, "A", [("user", "Please build A")])
        seed(j, tmp_path, "B", [("user", "Please build B")])
        seed(j, tmp_path, "empty", [("assistant", "Internal child status")])
        j.selected_conversation(tmp_path, next(c for c in j.conversations(tmp_path) if c["id"] == "empty"))
        monkeypatch.setattr(provider, "credential", lambda _: "dummy")
        monkeypatch.setattr(provider, "request", lambda *args: {"result": {f"d{i}_kind": "none" for i in range(3)}})
        first = provider.extract_project(j, tmp_path, 1)
        assert first["calls"] == 1 and first["remaining"] == 1
        empty = next(c for c in first["conversations"] if c["session_id"] == "empty")
        assert empty["status"] == "no_eligible_user_exchanges"
        second = provider.extract_project(j, tmp_path, 1)
        assert second["calls"] == 1 and second["cached"] == 1 and second["remaining"] == 0
        assert len(j.project_context(tmp_path)["conversations"]) == 3
        assert provider.extract_project(j, tmp_path / "absent")["status"] == "no_captured_history"
    finally:
        j.close()


def test_default_cli_asks_destination_not_conversation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(["invalid", "cursor"])
    prompts = []
    def answer(prompt):
        prompts.append(prompt)
        return next(answers)
    monkeypatch.setattr("builtins.input", answer)
    calls = []
    monkeypatch.setattr(handoff, "transfer_project", lambda *args: calls.append(args) or 0)
    assert handoff.main([]) == 0
    assert calls[0][0] == tmp_path and calls[0][1] == "cursor"
    assert all("agent" in prompt and "Conversation" not in prompt for prompt in prompts)


@pytest.mark.parametrize("destination", ["codex", "claude", "muse"])
def test_destination_setup_uses_exact_journal_and_runtime(tmp_path, monkeypatch, destination):
    monkeypatch.setattr(handoff.shutil, "which", lambda name: name + ".exe")
    commands = []
    monkeypatch.setattr(handoff, "run_setup_command", lambda command: (commands.append(command) or True, "configured"))
    db = tmp_path / "custom.db"
    ok, _ = handoff.configure_destination(destination, tmp_path, db)
    assert ok
    if destination == "muse":
        entry = json.loads((tmp_path / ".config/muse/settings.json").read_text())["mcpServers"]["threadline"]
        command = [entry["command"], *entry["args"]]
    else:
        command = commands[0]
        assert command[:3] == [destination + ".exe", "mcp", "add"]
    assert command[-2:] == ["--db", str(db)]
    assert sys.executable in command


def test_empty_transfer_and_cli_validation_are_honest(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    assert handoff.transfer_project(project, "cursor", tmp_path / "home", tmp_path / "db") == 1
    report = json.loads((project / ".handoff/transfer.json").read_text())
    assert report["status"] == "no_captured_history" and report["destination_loaded"] == "not_verified"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(handoff, "transfer_project", lambda *args: pytest.fail("Must validate first"))
    assert handoff.main(["transfer"]) == 2
    assert handoff.main(["transfer", "--to", "cursor", "--project", str(tmp_path / "missing")]) == 2
