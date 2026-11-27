import json
import subprocess
import sys

import pytest

from handoff import main, doctor, configure
from handoff_decisions import annotate, track, page
from handoff_sessions import Journal, make_event


def seed(journal, project, sid="A", texts=None):
    texts = texts or [
        ("user", "I want this to work offline."),
        ("assistant", "We could use PostgreSQL."),
        ("user", "Use SQLite. Do not use PostgreSQL."),
        ("user", "Actually, keep all data on this machine."),
        ("user", "Please implement local storage."),
        ("assistant", "Implemented local storage."),
        ("user", "Please add the export button."),
    ]
    events = [make_event("codex", sid, i, role + "_message", role, text, str(i)) for i, (role, text) in enumerate(texts)]
    journal.import_session({"source": "codex", "id": sid, "project": str(project), "path": str(project / sid), "updated": "1", "events": events})
    return events


def test_tracking_automatic_evidence_and_completion(tmp_path):
    j = Journal(tmp_path / "j.db")
    try:
        events = seed(j, tmp_path)
        items = track(j, tmp_path)["items"]
        assert {i["kind"] for i in items} == {"goal", "accepted_decision", "rejected_approach", "correction", "request"}
        assert all(i["evidence"][0]["quote"] in next(e for e in events if e["id"] == i["evidence"][0]["event_id"])["content"] for i in items)
        assert not any(i["kind"] == "accepted_decision" and "PostgreSQL" in i["text"] for i in items)
        requests = [i for i in items if i["kind"] == "request"]
        assert requests[0]["status"] == "completion_claimed_unverified"
        assert requests[1]["status"] == "completion_unknown"
        assert j.context(tmp_path)["decision_tracking"]["total_items"] == len(items)
        assert track(j, tmp_path)["items"] == items
        assert page(j, tmp_path, start=0, limit=2)["next_start"] == 2
    finally:
        j.close()


@pytest.mark.parametrize("text", ["Maybe use SQLite.", "Use SQLite?", "Use SQLite if we need a database.",
                                  '> Use SQLite.', '```\nUse SQLite.\n```', '"Use SQLite."', "We could use SQLite."])
def test_no_false_acceptance(tmp_path, text):
    j = Journal(tmp_path / "j.db")
    try:
        seed(j, tmp_path, texts=[("user", text)])
        assert not track(j, tmp_path)["items"]
    finally:
        j.close()


def test_asking_the_agent_to_use_a_tool_is_a_request_and_repeats_count_once(tmp_path):
    j = Journal(tmp_path / "j.db")
    try:
        ask = "Use Handoff's resume tool on this folder and tell me where we left off."
        seed(j, tmp_path, texts=[("user", ask), ("user", ask), ("user", "Use SQLite.")])
        items = track(j, tmp_path)["items"]
        assert [(i["kind"], i["text"]) for i in items] == [("request", ask), ("accepted_decision", "Use SQLite.")]
    finally:
        j.close()


def test_annotations_validate_quotes_scope_relations_and_staleness(tmp_path):
    j = Journal(tmp_path / "j.db")
    try:
        events = seed(j, tmp_path)
        seed(j, tmp_path, "B", [("user", "Use PostgreSQL.")])
        evidence = [{"event_id": events[3]["id"], "quote": events[3]["content"]}]
        previous = next(i for i in track(j, tmp_path)["items"] if i["kind"] == "accepted_decision" and i["session_id"] == "A")
        other = next(i for i in track(j, tmp_path)["items"] if i["session_id"] == "B")
        item = annotate(j, tmp_path, "codex", "A", "correction", "Keep storage local", evidence, [previous["id"]], [other["id"]])
        assert item["status"] == "agent_interpretation"
        with pytest.raises(ValueError):
            annotate(j, tmp_path, "codex", "A", "correction", "bad", evidence, [other["id"]])
        with pytest.raises(ValueError):
            annotate(j, tmp_path / "other", "codex", "A", "goal", "bad", evidence)
        with pytest.raises(ValueError):
            annotate(j, tmp_path, "codex", "A", "goal", "bad", [{"event_id": events[3]["id"], "quote": "invented"}])
        with pytest.raises(ValueError):
            annotate(j, tmp_path, "codex", "A", "accepted_decision", "bad", [{"event_id": events[1]["id"], "quote": events[1]["content"]}])
        j.db.execute("DELETE FROM events WHERE id=?", (events[3]["id"],))
        j.db.commit()
        assert next(i for i in track(j, tmp_path)["items"] if i["id"] == item["id"])["status"] == "stale_evidence"
        j.delete(tmp_path, "A")
        assert j.db.execute("SELECT count(*) FROM decision_annotations").fetchone()[0] == 0
        assert track(j, tmp_path)["items"][0]["session_id"] == "B"
    finally:
        j.close()


def test_entire_cli_workflow_and_doctor(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("handoff.os.path.expanduser", lambda _: str(tmp_path))
    monkeypatch.setattr("handoff_sessions.cursor_root", lambda _: tmp_path / "cursor")
    # Do not inspect/configure real installed agents while testing doctor.
    real_which = __import__("shutil").which
    monkeypatch.setattr("handoff.shutil.which", lambda name: real_which(name) if name == "git" else None)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    source = tmp_path / ".codex/sessions/a.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": "A", "cwd": str(tmp_path)}}) + "\n" +
                      json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "Use SQLite. Please implement export."}}) + "\n")
    db = tmp_path / "j.db"
    args = ["--project", str(tmp_path), "--db", str(db)]
    for command in ("sync", "coverage", "conversations", "decisions", "resume", "export"):
        assert main([command, *args]) == 0
    assert "Decision evidence" in (tmp_path / "HANDOFF.md").read_text()
    assert (tmp_path / ".handoff/decisions.json").exists()
    assert main(["read", *args, "--source", "codex", "--session", "A"]) == 0
    assert main(["search", *args, "--query", "SQLite"]) == 0
    assert main(["history", "--db", str(db)]) == 0
    assert doctor(tmp_path, db) == 0
    assert main(["--check", str(tmp_path / "HANDOFF.md")]) == 0
    assert main(["delete", *args, "--yes"]) == 0
    j = Journal(db)
    try:
        assert not j.projects()
        assert not track(j, tmp_path)["items"]
    finally:
        j.close()


def test_setup_and_uninstall_all_adapters_in_isolation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("handoff.shutil.which", lambda name: name if name in ("codex", "claude", "cursor-agent", "muse") else None)
    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "ok", "")
    monkeypatch.setattr("handoff.subprocess.run", runner)
    assert configure("setup", tmp_path) == 0
    assert configure("uninstall", tmp_path) == 0
    assert any(c[:3] == ["codex", "mcp", "add"] for c in calls)
    assert any(c[:3] == ["claude", "mcp", "add"] for c in calls)
    assert "threadline" not in json.loads((tmp_path / ".cursor/mcp.json").read_text())["mcpServers"]


def test_decision_mcp_real_process(tmp_path):
    db = tmp_path / "j.db"
    j = Journal(db)
    events = seed(j, tmp_path)
    assert track(j, tmp_path)["items"]
    assert annotate(j, tmp_path, "codex", "A", "goal", "Offline operation",
                    [{"event_id": events[0]["id"], "quote": events[0]["content"]}])["status"] == "agent_interpretation"
    j.close()
    requests = [
        {"id": 3, "method": "tools/call", "params": {"name": "resume", "arguments": {"project": str(tmp_path), "refresh": False}}},
    ]
    result = subprocess.run([sys.executable, "-m", "handoff", "--mcp", "--db", str(db)],
                            input="\n".join(json.dumps(r) for r in requests) + "\n", text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert replies[0]["result"]["structuredContent"]["decision_tracking"]["total_items"] == 7


def test_mcp_bad_evidence_error_preserves_id_and_server_continues(tmp_path):
    requests = [
        {"id": 91, "method": "tools/call", "params": {"name": "read_conversation", "arguments": {"project": str(tmp_path)}}},
        {"id": 92, "method": "tools/list"},
        {"id": 93, "method": "prompts/list"},
        {"id": 94, "method": "prompts/get", "params": {"name": "handoff-resume", "arguments": {"project": str(tmp_path)}}},
    ]
    result = subprocess.run([sys.executable, "-m", "handoff", "--mcp", "--db", str(tmp_path / "j.db")],
                            input="\n".join(json.dumps(r) for r in requests) + "\n", text=True, capture_output=True, timeout=30)
    assert result.returncode == 0
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert replies[0]["id"] == 91 and replies[0]["error"]["code"] == -32602
    assert {t["name"] for t in replies[1]["result"]["tools"]} == {"resume", "search_context", "read_conversation"}
    assert replies[2]["result"]["prompts"]
    assert replies[3]["result"]["messages"]


def test_tracking_rebuilds_when_source_changes_and_fts_unavailable(tmp_path):
    j = Journal(tmp_path / "j.db")
    try:
        seed(j, tmp_path, texts=[("user", "Goal: work offline."), ("user", "Use SQLite.")])
        assert {i["kind"] for i in track(j, tmp_path)["items"]} == {"goal", "accepted_decision"}
        j.fts = False
        assert j.search_context(tmp_path, "SQLite")["results"]
        j.db.execute("DELETE FROM events WHERE kind='user_message'")
        j.db.commit()
        assert not track(j, tmp_path)["items"]
    finally:
        j.close()


def test_completion_of_later_request_does_not_close_earlier_request(tmp_path):
    j = Journal(tmp_path / "j.db")
    try:
        seed(j, tmp_path, texts=[("user", "Please build the database."),
                                ("user", "Please fix the button first."),
                                ("assistant", "Done: the button is fixed.")])
        requests = track(j, tmp_path)["items"]
        assert requests[0]["status"] == "completion_unknown"
        assert requests[1]["status"] == "completion_claimed_unverified"
    finally:
        j.close()
