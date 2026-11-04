import datetime
import json

import pytest

from handoff import transfer_project
from handoff_capture import coverage_page, initialize, summarize_report
from handoff_sessions import Journal, make_event, mcp_response

NOW = datetime.datetime.now(datetime.timezone.utc).isoformat()


def add(journal, project, sid, contents, source="codex", stamp="2026-01-01T00:00:00Z", parent=None):
    events = [make_event(source, sid, i, kind, "user" if kind == "user_message" else "assistant", text, stamp, {})
              for i, (kind, text) in enumerate(contents)]
    journal.import_session({"id": sid, "source": source, "project": str(project), "path": str(project / sid),
                            "updated": "2026-01-01", "events": events})
    if parent:
        columns = [row[1] for row in journal.db.execute("PRAGMA table_info(capture_state)")]
        values = {"project": str(project), "source": source, "session_id": sid,
                  "details": json.dumps({"metadata": {"parent_thread_id": parent}})}
        row = [values.get(column, sid) for column in columns]
        journal.db.execute(f"INSERT INTO capture_state VALUES ({','.join('?' * len(columns))})", row)
        journal.db.commit()


def resume(journal, project, **arguments):
    response = mcp_response({"id": 1, "method": "tools/call", "params": {"name": "resume",
        "arguments": {"project": str(project), "refresh": False, **arguments}}}, journal)
    return response["result"]["content"][0]["text"], response["result"]["structuredContent"]


@pytest.fixture
def journal(tmp_path):
    instance = Journal(tmp_path / "j.db")
    initialize(instance.db)
    yield instance
    instance.close()


@pytest.mark.parametrize("budget", [1000, 4000, 32000])
def test_oversized_history_stays_within_budget_and_keeps_latest_request(journal, tmp_path, budget):
    add(journal, tmp_path, "main", [("user_message", "older work"), ("assistant_message", "older reply"),
                                    ("user_message", "ship the parser"), ("tool_result", "x" * 2_000_000),
                                    ("assistant_message", "parser shipped")], stamp="2026-02-01T00:00:00Z")
    for number in range(300):
        add(journal, tmp_path, f"child-{number}", [("user_message", "I want review " + "y" * 3000)],
            stamp="2026-03-01T00:00:00Z", parent="main")
    text, data = resume(journal, tmp_path, token_budget=budget)
    assert len(text) <= budget * 4
    assert data["latest_request"]["text"] == "ship the parser"
    assert data["recent_exchanges"][0]["session_id"] == "main"
    assert data["conversations_total"] == 301
    assert data["conversations"][0]["id"] == "main"


def test_shortened_preview_points_to_the_complete_message(journal, tmp_path):
    long_reply = "reply " * 20000
    add(journal, tmp_path, "main", [("user_message", "do it"), ("tool_result", "x" * 100000), ("assistant_message", long_reply)])
    _, data = resume(journal, tmp_path)
    preview = data["recent_exchanges"][0]
    assert preview["omitted_events"] == 1
    reply = next(e for e in preview["events"] if e["kind"] == "assistant_message")
    assert reply["content_truncated"] and reply["full_chars"] == len(long_reply)
    assert journal.get([reply["id"]])[0]["content"] == long_reply
    full = journal.read_conversation(tmp_path, "codex", "main", preview["read_full"]["start"], 1, messages_only=True)
    assert [e["kind"] for e in full["exchanges"][0]["events"]] == ["user_message", "assistant_message"]
    assert full["exchanges"][0]["events_total"] == 3


def test_small_exchange_is_shown_whole(journal, tmp_path):
    add(journal, tmp_path, "main", [("user_message", "run it"), ("tool_result", "ok"), ("assistant_message", "done")])
    _, data = resume(journal, tmp_path)
    assert [e["kind"] for e in data["recent_exchanges"][0]["events"]] == ["user_message", "tool_result", "assistant_message"]
    assert "omitted_events" not in data["recent_exchanges"][0]


def test_newest_message_decides_recency_not_the_shared_source_time(journal, tmp_path):
    add(journal, tmp_path, "old", [("user_message", "old request")], stamp="2026-01-01T00:00:00Z")
    add(journal, tmp_path, "new", [("user_message", "new request")], stamp="2026-02-01T00:00:00Z")
    _, data = resume(journal, tmp_path)
    assert data["latest_request"]["text"] == "new request"


def test_calling_session_is_labelled_and_not_reported_as_the_latest_request(journal, tmp_path):
    add(journal, tmp_path, "codex-chat", [("user_message", "the real work")])
    add(journal, tmp_path, "this-chat", [("user_message", "I want you to resume")], source="claude", stamp=NOW)
    mcp_response({"id": 0, "method": "initialize", "params": {"clientInfo": {"name": "claude-code"}}}, journal)
    _, data = resume(journal, tmp_path)
    assert data["latest_request"]["text"] == "the real work"
    assert {row["id"]: row.get("likely_current_session", False) for row in data["conversations"]} == {"this-chat": True, "codex-chat": False}
    assert all(item["session_id"] != "this-chat" for item in data["decision_tracking"]["items"])


def test_older_chat_from_the_calling_tool_is_still_reported(journal, tmp_path):
    add(journal, tmp_path, "earlier", [("user_message", "finish the work")], source="claude")
    mcp_response({"id": 0, "method": "initialize", "params": {"clientInfo": {"name": "claude-code"}}}, journal)
    _, data = resume(journal, tmp_path)
    assert data["latest_request"]["text"] == "finish the work"


def test_injected_client_context_is_hidden_from_previews_and_titles(journal, tmp_path):
    add(journal, tmp_path, "main", [("user_message", "<system-reminder>Use it only for attribution.</system-reminder>\nfix the parser")])
    _, data = resume(journal, tmp_path)
    assert data["latest_request"]["text"] == "fix the parser"
    assert data["conversations"][0]["title"] == "fix the parser"


def test_mcp_read_pages_reach_every_exchange_without_shortening(journal, tmp_path):
    contents = []
    for number in range(6):
        contents += [("user_message", f"request {number}"), ("assistant_message", str(number) * 30000)]
    add(journal, tmp_path, "main", contents)
    start, seen = 0, []
    while start is not None:
        response = mcp_response({"id": 1, "method": "tools/call", "params": {"name": "read_conversation", "arguments": {
            "project": str(tmp_path), "source": "codex", "session_id": "main", "start": start, "limit": 50}}}, journal)
        page = response["result"]["structuredContent"]
        assert len(json.dumps(page["exchanges"])) < 130000
        seen += [e["content"] for exchange in page["exchanges"] for e in exchange["events"]]
        start = page["next_start"]
    assert seen == [text for _, text in contents]


def test_mcp_search_context_leaves_out_neighbouring_tool_output_but_keeps_the_match(journal, tmp_path):
    add(journal, tmp_path, "main", [("user_message", "first"), ("tool_result", "z" * 500000), ("assistant_message", "reply"),
                                    ("user_message", "second"), ("tool_result", "needle in output"), ("assistant_message", "reply two")])
    response = mcp_response({"id": 1, "method": "tools/call", "params": {"name": "search_context",
        "arguments": {"project": str(tmp_path), "query": "needle"}}}, journal)
    result = response["result"]["structuredContent"]["results"][0]
    contents = [e["content"] for exchange in result["exchanges"] for e in exchange["events"]]
    assert "needle in output" in contents and "z" * 500000 not in contents


def test_transfer_exports_every_chat_even_when_the_overview_is_limited(journal, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    for number in range(120):
        add(journal, project, f"chat-{number:03}", [("user_message", f"work {number}")])
    assert journal.project_context(project)["conversations_listed"] < 120
    journal.db.commit()
    (tmp_path / "home").mkdir()
    transfer_project(project, "cursor", tmp_path / "home", tmp_path / "j.db")
    assert len(json.loads((project / ".handoff" / "conversations.json").read_text(encoding="utf-8"))) == 120
    assert len((project / ".handoff" / "events.jsonl").read_text(encoding="utf-8").splitlines()) == 120


def test_coverage_summary_groups_gaps_and_pages_details():
    report = {"project": "p", "captured_at": "now", "scope": "s", "sources": {}, "new_records": 1, "new_events": 1,
              "source_files_captured": 3, "conversations_captured": 3,
              "gaps": [{"source": "muse", "path": f"/{n}", "reason": "no project"} for n in range(9)] + [{"source": "codex", "path": "/db", "reason": "other"}],
              "conversations": [{"session_id": str(n), "status": "captured", "metadata": {"base": "x" * 20000}, "gaps": ["g"]} for n in range(3)]}
    summary = summarize_report(report)
    assert summary["gaps_total"] == 10 and summary["conversation_gaps"] == 3
    assert summary["gaps"][0] == {"source": "muse", "reason": "no project", "count": 9, "example": "/0"}
    assert len(json.dumps(summary)) < 1000
    page = coverage_page(report, start=1, limit=1)
    assert [row["session_id"] for row in page["conversations"]] == ["1"] and page["next_start"] == 2
    assert "metadata" not in page["conversations"][0]
