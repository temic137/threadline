import json
from pathlib import Path

import pytest

from handoff import main
from handoff_sessions import Journal, make_event, mcp_response


def add(journal, project, sid, contents, source="codex"):
    events = [make_event(source, sid, i, kind, "user" if kind == "user_message" else "assistant",
                         text, f"2026-01-01T00:{i:04d}", metadata) for i, (kind, text, metadata) in enumerate(contents)]
    journal.import_session({"id": sid, "source": source, "project": str(project), "path": str(project / sid),
                            "updated": "2026-01-01", "events": events})


def test_three_chat_continuation_and_requirement_retrieval(tmp_path, capsys):
    db = tmp_path / "journal.db"
    journal = Journal(db)
    try:
        add(journal, tmp_path, "A", [("user_message", "Everything must work offline", {}),
                                      ("assistant_message", "Understood; no hosted service", {})])
        add(journal, tmp_path, "B", [("user_message", "Reject PostgreSQL; use SQLite", {})])
        add(journal, tmp_path, "C", [("user_message", "Finish the interface", {}),
                                      ("assistant_message", "The save button is unfinished", {}),
                                      ("user_message", "Continue", {})])
        assert journal.context(tmp_path)["status"] == "selection_required"
        response = mcp_response({"id": 1, "method": "tools/call", "params": {"name": "resume",
            "arguments": {"project": str(tmp_path), "source": "codex", "session_id": "C", "refresh": False}}}, journal)
        context = response["result"]["structuredContent"]
        assert context["status"] == "ready"
        events = [e for group in context["exchanges"] for e in group["events"]]
        assert {e["session_id"] for e in events} == {"C"}
        assert [e["content"] for e in events] == ["Finish the interface", "The save button is unfinished", "Continue"]
        assert "objective" not in context
        result = journal.search_context(tmp_path, "offline")
        assert result["results"][0]["session_id"] == "A"
        assert len(result["results"][0]["exchanges"][0]["events"]) == 2
    finally:
        journal.close()
    args = ["--project", str(tmp_path), "--db", str(db)]
    assert main(["conversations", *args]) == 0
    assert len(json.loads(capsys.readouterr().out)["conversations"]) == 3
    assert main(["resume", *args, "--source", "codex", "--session", "C"]) == 0
    assert json.loads(capsys.readouterr().out)["session_id"] == "C"
    assert main(["search", *args, "--query", "offline"]) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["session_id"] == "A"


def test_budget_does_not_split_tool_exchange(tmp_path):
    journal = Journal(tmp_path / "journal.db")
    try:
        add(journal, tmp_path, "A", [("user_message", "run it", {}),
            ("tool_call", "pytest", {"tool_id": "x"}), ("user_message", "also check this", {}),
            ("tool_result", "x" * 6000, {"tool_id": "x"}), ("assistant_message", "done", {})])
        groups = journal.exchanges(tmp_path, "codex", "A")
        assert len(groups) == 1 and len(groups[0]) == 5
        context = journal.context(tmp_path, 1000)
        assert context["status"] == "read_required"
        assert context["omitted_exchanges"] == 1
        assert not context["exchanges"]
        full = journal.read_conversation(tmp_path, "codex", "A", 0, 1)
        assert len(full["exchanges"][0]["events"][3]["content"]) == 6000
    finally:
        journal.close()


def test_pagination_old_history_and_project_isolation(tmp_path):
    journal = Journal(tmp_path / "journal.db")
    try:
        add(journal, tmp_path, "A", [("user_message", f"turn-{i}", {}) for i in range(620)])
        page = journal.read_conversation(tmp_path, "codex", "A", 0, 3)
        assert page["total_exchanges"] == 620 and page["next_start"] == 3
        assert page["exchanges"][0]["events"][0]["content"] == "turn-0"
        last = journal.read_conversation(tmp_path, "codex", "A", 619, 3)
        assert last["next_start"] is None
        assert len(last["exchanges"]) == 1
        assert journal.context(tmp_path, 1000)["omitted_exchanges"] > 0
        assert journal.search_context(tmp_path, "turn-0")["results"]
        with pytest.raises(ValueError):
            journal.read_conversation(tmp_path / "other", "codex", "A")
        with pytest.raises(ValueError):
            journal.read_conversation(tmp_path, "codex", "A", -1)
        with pytest.raises(ValueError):
            journal.read_conversation(tmp_path, "codex", "A", 0, 51)
    finally:
        journal.close()


def test_same_id_different_tools_requires_source(tmp_path):
    journal = Journal(tmp_path / "journal.db")
    try:
        for tool in ("codex", "claude"):
            add(journal, tmp_path, "A", [("user_message", tool, {})], tool)
        assert journal.context(tmp_path, session_id="A")["status"] == "selection_required"
        assert journal.context(tmp_path, source="claude", session_id="A")["exchanges"][0]["events"][0]["content"] == "claude"
        assert journal.context(tmp_path, session_id="absent")["status"] == "not_found"
    finally:
        journal.close()


def test_source_order_survives_missing_or_nonmonotonic_timestamps(tmp_path):
    journal = Journal(tmp_path / "journal.db")
    try:
        add(journal, tmp_path, "A", [("user_message", "first", {}), ("assistant_message", "second", {}),
                                     ("user_message", "third", {})])
        journal.db.execute("UPDATE events SET timestamp='' WHERE content='second'")
        journal.db.execute("UPDATE events SET timestamp='1900' WHERE content='third'")
        journal.db.commit()
        assert [e["content"] for group in journal.exchanges(tmp_path, "codex", "A") for e in group] == ["first", "second", "third"]
    finally:
        journal.close()
