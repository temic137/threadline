"""Real SQLite fixtures for Codex history migration and overlapping JSONL."""
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from handoff_capture import synchronize
from handoff_sessions import Journal, mcp_response
from test_capture_acceptance import codex


def databases(home, project, sid="main", rollout=""):
    root = home / ".codex"
    root.mkdir(parents=True, exist_ok=True)
    state, history = root / "state_5.sqlite", root / "thread_history_1.sqlite"
    with closing(sqlite3.connect(state)) as db, db:
        db.executescript("""
            CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT,rollout_path TEXT,source TEXT,title TEXT);
            CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT);
            CREATE TABLE thread_attachments(id TEXT,thread_id TEXT,payload TEXT);
        """)
        db.execute("INSERT INTO threads VALUES (?,?,?,?,?)", (sid, "\\\\?\\"+str(project), str(rollout), "vscode", "Project chat"))
    with closing(sqlite3.connect(history)) as db, db:
        db.executescript("""
            CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,rollout_ordinal INTEGER,status TEXT,started_at INTEGER,PRIMARY KEY(thread_id,turn_id));
            CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,rollout_ordinal INTEGER,created_at_ms INTEGER,item_json TEXT,PRIMARY KEY(thread_id,turn_id,item_id));
            CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,next_rollout_ordinal INTEGER,next_rollout_byte_offset INTEGER);
            CREATE TABLE thread_realtime_items(thread_id TEXT,item_id TEXT,rollout_ordinal INTEGER,item_json TEXT);
        """)
        db.execute("INSERT INTO thread_turns VALUES (?,?,?,?,?)", (sid, "turn", 0, "completed", 1793491200))
    return state, history


def item(history, sid, ordinal, kind, **fields):
    with closing(sqlite3.connect(history)) as db, db:
        db.execute("INSERT OR REPLACE INTO thread_items VALUES (?,?,?,?,?,?)",
                   (sid, "turn", str(ordinal), ordinal, 1793491200000 + ordinal,
                    json.dumps({"type": kind, "id": str(ordinal), **fields})))


def test_database_only_capture_identity_tools_redaction_and_incremental(tmp_path):
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    state, history = databases(home, project)
    with sqlite3.connect(state) as db:
        db.execute("INSERT INTO threads VALUES (?,?,?,?,?)", ("child", str(project), "", '{"subagent":{"other":"worker"}}', "Child"))
        db.execute("INSERT INTO thread_spawn_edges VALUES (?,?)", ("main", "child"))
        db.execute("INSERT INTO thread_attachments VALUES (?,?,?)", ("att", "main", '{"password":"do-not-save"}'))
        db.execute("INSERT INTO threads VALUES (?,?,?,?,?)", ("other", str(tmp_path / "other"), "", "vscode", "Other project"))
    item(history, "main", 1, "userMessage", content=[{"type": "text", "text": "Please build SQLite capture"}])
    item(history, "main", 2, "agentMessage", text="Working", phase="commentary")
    item(history, "main", 3, "commandExecution", command="pytest", aggregatedOutput="2 passed", exitCode=0, status="completed")
    item(history, "main", 4, "fileChange", changes=[{"path": "app.py", "diff": "+new"}])
    item(history, "main", 5, "mcpToolCall", tool="test", arguments={"password": "do-not-save"}, result={"content": [{"type": "text", "text": "result"}]})
    item(history, "main", 6, "futureType", future="unknown-record-must-survive")
    item(history, "child", 1, "userMessage", content=[{"type": "text", "text": "Child work"}])
    item(history, "other", 1, "userMessage", content=[{"type": "text", "text": "foreign-project-content"}])
    before = {p: p.read_bytes() for p in (state, history)}
    j = Journal(tmp_path / "journal.db")
    try:
        captured, report = synchronize(j, project, home, "codex")
        assert {c["id"] for c in captured} == {"main", "child"}
        child = next(c for c in j.conversations(project) if c["id"] == "child")
        assert child["relationship"] == "child" and child["parent_id"] == "main"
        events = [e for g in j.exchanges(project, "codex", "main") for e in g]
        assert {e["kind"] for e in events} >= {"user_message", "assistant_message", "command", "file_change", "tool_call", "tool_result"}
        assert all(e["metadata"]["native_turn_id"] == "turn" for e in events)
        archive = "".join(r[0] for r in j.db.execute("SELECT payload FROM capture_records"))
        assert "unknown-record-must-survive" in archive
        assert "do-not-save" not in archive + json.dumps(events)
        assert "foreign-project-content" not in archive
        assert any("sqlite/futureType" in c["archive_only_record_types"] for c in report["conversations"])
        snapshots = j.db.execute("SELECT COUNT(*) FROM capture_snapshots").fetchone()[0]
        _, repeated = synchronize(j, project, home, "codex")
        assert repeated["new_records"] == repeated["new_events"] == 0
        assert j.db.execute("SELECT COUNT(*) FROM capture_snapshots").fetchone()[0] == snapshots
        item(history, "main", 7, "userMessage", content=[{"type": "text", "text": "Please add incremental sync"}])
        _, updated = synchronize(j, project, home, "codex")
        assert updated["new_events"] == updated["new_records"] == 1
        assert j.search_context(project, "incremental")["results"]
        item(history, "main", 7, "userMessage", content=[{"type": "text", "text": "Please fix rewritten history"}])
        synchronize(j, project, home, "codex")
        assert not j.search_context(project, "incremental")["results"]
        assert j.search_context(project, "rewritten")["results"]
        assert j.db.execute("SELECT COUNT(*) FROM capture_records WHERE record_key='item:turn:7'").fetchone()[0] == 2
        assert state.read_bytes() == before[state]
    finally:
        j.close()


def test_projected_overlap_migrates_existing_jsonl_and_preserves_tail(tmp_path):
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    rollout = home / ".codex/sessions/thread.jsonl"
    codex(rollout, project, "main", ["first", "tail"])
    j = Journal(tmp_path / "j.db")
    try:
        synchronize(j, project, home, "codex")
        _, history = databases(home, project, rollout=rollout)
        item(history, "main", 1, "userMessage", content=[{"type": "text", "text": "first"}])
        with sqlite3.connect(history) as db:
            db.execute("INSERT INTO thread_history_projection_state VALUES (?,?,?)", ("main", 2, 0))
        _, report = synchronize(j, project, home, "codex")
        events = [e for group in j.exchanges(project, "codex", "main") for e in group]
        assert [e["content"] for e in events] == ["first", "tail"]
        assert any(c["metadata"].get("sqlite_covered_events") == 1 for c in report["conversations"])
        assert j.db.execute("SELECT COUNT(*) FROM capture_records WHERE location=?", (str(rollout),)).fetchone()[0] == 3
        _, repeated = synchronize(j, project, home, "codex")
        assert repeated["new_events"] == repeated["new_records"] == 0
        item(history, "main", 2, "userMessage", content=[{"type": "text", "text": "tail"}])
        with sqlite3.connect(history) as db:
            db.execute("UPDATE thread_history_projection_state SET next_rollout_ordinal=3")
        synchronize(j, project, home, "codex")
        assert [e["content"] for g in j.exchanges(project, "codex", "main") for e in g] == ["first", "tail"]
    finally:
        j.close()


def test_unconfirmed_overlap_is_retained_and_reported(tmp_path):
    rollout = tmp_path / ".codex/sessions/a.jsonl"
    codex(rollout, tmp_path, "main", ["same text"])
    _, history = databases(tmp_path, tmp_path, rollout=rollout)
    item(history, "main", 1, "userMessage", content=[{"type": "text", "text": "same text"}])
    j = Journal(tmp_path / "j.db")
    try:
        _, report = synchronize(j, tmp_path, tmp_path, "codex")
        assert any("duplicate exchanges may appear" in gap for c in report["conversations"] for gap in c["gaps"])
        assert j.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    finally:
        j.close()


def test_rewritten_jsonl_is_not_discarded_by_stale_projection(tmp_path):
    rollout = tmp_path / ".codex/sessions/a.jsonl"
    codex(rollout, tmp_path, "main", ["new source text"])
    _, history = databases(tmp_path, tmp_path, rollout=rollout)
    item(history, "main", 1, "userMessage", content=[{"type": "text", "text": "old database text"}])
    with closing(sqlite3.connect(history)) as db, db:
        db.execute("INSERT INTO thread_history_projection_state VALUES (?,?,?)", ("main", 100, 100000))
    j = Journal(tmp_path / "j.db")
    try:
        _, report = synchronize(j, tmp_path, tmp_path, "codex")
        assert {e["content"] for g in j.exchanges(tmp_path, "codex", "main") for e in g} == {"old database text", "new source text"}
        assert any("no exact normalized match" in gap for c in report["conversations"] for gap in c["gaps"])
    finally:
        j.close()


@pytest.mark.parametrize("failure", ["missing", "schema", "malformed"])
def test_history_gaps_and_jsonl_fallback(tmp_path, failure):
    rollout = tmp_path / ".codex/sessions/a.jsonl"
    codex(rollout, tmp_path, "main", ["JSONL remains available"])
    state, history = databases(tmp_path, tmp_path, rollout=rollout)
    if failure == "missing":
        history.unlink()
    elif failure == "schema":
        with sqlite3.connect(history) as db:
            db.execute("DROP TABLE thread_items")
    else:
        with sqlite3.connect(history) as db:
            db.execute("INSERT INTO thread_items VALUES (?,?,?,?,?,?)", ("main", "turn", "bad", 1, 0, "not-json"))
    j = Journal(tmp_path / "j.db")
    try:
        _, report = synchronize(j, tmp_path, tmp_path, "codex")
        assert j.search_context(tmp_path, "available")["results"]
        assert report["gaps"] or any(any("unavailable" in gap or "could not be normalized" in gap for gap in c["gaps"]) for c in report["conversations"])
    finally:
        j.close()


def test_metadata_only_and_orphan_threads_have_visible_coverage(tmp_path):
    _, history = databases(tmp_path, tmp_path)
    item(history, "orphan", 1, "userMessage", content=[{"type": "text", "text": "unknown ownership"}])
    j = Journal(tmp_path / "j.db")
    try:
        _, report = synchronize(j, tmp_path, tmp_path, "codex")
        assert report["conversations_captured"] == 1
        assert any("no state metadata" in gap["reason"] for gap in report["gaps"])
        assert any("No history items" in gap for c in report["conversations"] for gap in c["gaps"])
        assert not j.search(tmp_path, "unknown ownership")
    finally:
        j.close()


def test_sqlite_transfer_and_real_destination_mcp(tmp_path):
    from handoff import transfer_project
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    _, history = databases(home, project)
    item(history, "main", 1, "userMessage", content=[{"type": "text", "text": "Please continue from the database history"}])
    item(history, "main", 2, "commandExecution", command="pytest", aggregatedOutput="Passed", exitCode=0)
    db = tmp_path / "handoff.db"
    assert transfer_project(project, "cursor", home, db) == 0
    index = json.loads((project / ".handoff/conversations.json").read_text())
    assert [c["id"] for c in index] == ["main"]
    assert "Please continue from the database history" in (project / ".handoff/events.jsonl").read_text()
    assert "item_json" in (project / ".handoff/capture_records.jsonl").read_text()
    entry = json.loads((home / ".cursor/mcp.json").read_text())["mcpServers"]["threadline"]
    request = {"id": 1, "method": "tools/call", "params": {"name": "resume", "arguments": {"project": str(project), "refresh": False}}}
    result = subprocess.run([entry["command"], *entry["args"]], input=json.dumps(request)+"\n",
                            capture_output=True, text=True, cwd=tmp_path, timeout=30)
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["result"]["structuredContent"]
    assert context["status"] == "project_ready"
    assert [e["kind"] for p in context["recent_exchanges"] for e in p["events"]] == ["user_message", "command"]
    assert context["decision_tracking"]["items"][0]["evidence_ids"]


def test_sqlite_capture_rolls_back_on_journal_write_failure(tmp_path, monkeypatch):
    _, history = databases(tmp_path, tmp_path)
    item(history, "main", 1, "userMessage", content=[{"type": "text", "text": "Please save the original"}])
    j = Journal(tmp_path / "j.db")
    try:
        synchronize(j, tmp_path, tmp_path, "codex")
        records = j.db.execute("SELECT COUNT(*) FROM capture_records").fetchone()[0]
        item(history, "main", 1, "userMessage", content=[{"type": "text", "text": "Please save the replacement"}])
        original = j.import_session
        def fail(*args, **kwargs):
            raise sqlite3.OperationalError("Injected write failure")
        monkeypatch.setattr(j, "import_session", fail)
        _, report = synchronize(j, tmp_path, tmp_path, "codex")
        assert report["gaps"] and report["new_records"] == 0
        assert j.db.execute("SELECT COUNT(*) FROM capture_records").fetchone()[0] == records
        assert j.search(tmp_path, "original") and not j.search(tmp_path, "replacement")
        monkeypatch.setattr(j, "import_session", original)
        synchronize(j, tmp_path, tmp_path, "codex")
        assert j.search(tmp_path, "replacement")
    finally:
        j.close()


def test_database_discovery_failure_keeps_jsonl_capture(tmp_path, monkeypatch):
    rollout = tmp_path / ".codex/sessions/a.jsonl"
    codex(rollout, tmp_path, "main", ["Please retain JSONL fallback"])
    def denied(*args, **kwargs):
        raise PermissionError("Database discovery denied")
    monkeypatch.setattr(Path, "glob", denied)
    j = Journal(tmp_path / "j.db")
    try:
        _, report = synchronize(j, tmp_path, tmp_path, "codex")
        assert report["conversations_captured"] == 1
        assert any("Database discovery failed" in gap["reason"] for gap in report["gaps"])
        assert j.search(tmp_path, "fallback")
    finally:
        j.close()
