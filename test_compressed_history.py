"""Codex's compressed rollouts hold the commands and outputs its history database leaves out."""
import json
import os
import sqlite3
from contextlib import closing

import pytest

import handoff_capture
import handoff_sessions
from handoff_capture import synchronize
from handoff_decisions import track
from handoff_sessions import Journal, make_event
from test_codex_sqlite import databases, item

zstandard = pytest.importorskip("zstandard")

MAIN = "11111111-1111-4111-8111-111111111111"
CHILD = "22222222-2222-4222-8222-222222222222"
FOREIGN = "33333333-3333-4333-8333-333333333333"
SLACK = "xoxb-" + "1234567890-1234567890123-" + "abcdefghijklmnopqrstuvwx"


def write_rollout(home, sid, project, records):
    folder = home / ".codex" / "sessions" / "2026" / "01" / "01"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-01-01T00-00-00-{sid}.jsonl.zst"
    lines = [{"type": "session_meta", "payload": {"id": sid, "cwd": str(project), "base_instructions": "x" * 1000}}] + records
    text = "".join(json.dumps({"timestamp": "2026-01-01T00:00:00Z", **line}) + "\n" for line in lines)
    path.write_bytes(zstandard.ZstdCompressor().compress(text.encode()))
    return path


def user(text):
    return {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": text}]}}}


def agent(text):
    return {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "AgentMessage", "content": [{"type": "text", "text": text}]}}}


def call(call_id, text):
    return {"type": "response_item", "payload": {"type": "function_call", "name": "shell_command", "call_id": call_id, "arguments": text}}


def output(call_id, text):
    return {"type": "response_item", "payload": {"type": "function_call_output", "call_id": call_id, "output": text}}


@pytest.fixture
def setup(tmp_path):
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    records = [user("first request"), call("c1", "pytest"), output("c1", "2 passed"), agent("first reply"),
               user("second request"), call("c2", "cat .env"), output("c2", f"TOKEN {SLACK}"),
               {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "FileChange", "changes": [{"path": "a.py"}], "status": "completed"}}},
               agent("second reply")]
    path = write_rollout(home, MAIN, project, records)
    state, history = databases(home, project, MAIN, str(path)[:-4])
    item(history, MAIN, 1, "userMessage", content=[{"type": "text", "text": "first request"}])
    item(history, MAIN, 4, "agentMessage", text="first reply")
    item(history, MAIN, 5, "userMessage", content=[{"type": "text", "text": "second request"}])
    item(history, MAIN, 8, "fileChange", changes=[{"path": "a.py", "diff": "+x"}], status="completed")
    item(history, MAIN, 9, "agentMessage", text="second reply")
    with closing(sqlite3.connect(history)) as db, db:
        db.execute("INSERT INTO thread_history_projection_state VALUES (?,?,?)", (MAIN, 10, 0))
    journal = Journal(tmp_path / "j.db")
    yield journal, project, home, path, state
    journal.close()


def test_tool_activity_merges_under_the_request_it_belongs_to(setup):
    journal, project, home, *_ = setup
    synchronize(journal, project, home, "codex")
    groups = journal.exchanges(project, "codex", MAIN)
    assert [[e["kind"] for e in group] for group in groups] == [
        ["user_message", "tool_call", "tool_result", "assistant_message"],
        ["user_message", "tool_call", "tool_result", "file_change", "assistant_message"]]
    assert groups[0][2]["content"] == "2 passed"


def test_secrets_in_compressed_output_are_redacted(setup):
    journal, project, home, *_ = setup
    synchronize(journal, project, home, "codex")
    contents = [e["content"] for group in journal.exchanges(project, "codex", MAIN) for e in group]
    assert "TOKEN [REDACTED]" in contents
    assert not any(SLACK in text for text in contents)


def test_raw_compressed_records_are_not_copied_into_the_journal(setup):
    journal, project, home, path, _ = setup
    synchronize(journal, project, home, "codex")
    copied = journal.db.execute("SELECT count(*) FROM capture_records WHERE location=?", (str(path.resolve()),)).fetchone()[0]
    assert copied == 0
    details = [json.loads(row[0]) for row in journal.db.execute("SELECT details FROM capture_state")]
    assert not any("base_instructions" in d["metadata"] for d in details)


def test_thread_missing_from_the_database_is_read_from_its_rollout(setup):
    journal, project, home, _, state = setup
    write_rollout(home, CHILD, project, [user("review this"), agent("allowed")])
    with closing(sqlite3.connect(state)) as db, db:
        db.execute("INSERT INTO threads VALUES (?,?,?,?,?)", (CHILD, str(project), "", '{"subagent":{"other":"guardian"}}', "Guardian review"))
    synchronize(journal, project, home, "codex")
    assert [e["content"] for g in journal.exchanges(project, "codex", CHILD) for e in g] == ["review this", "allowed"]
    assert next(c for c in journal.conversations(project) if c["id"] == CHILD)["relationship"] == "child"


def test_older_segment_adds_only_what_the_database_lacks(setup):
    journal, project, home, _, _ = setup
    folder = home / ".codex" / "sessions" / "2026" / "01" / "01"
    lines = [{"type": "session_meta", "payload": {"id": MAIN, "cwd": str(project)}}, user("first request"), call("old", "ls"), output("old", "listing")]
    older = folder / f"rollout-2025-12-31T00-00-00-{MAIN}_44444444-4444-4444-8444-444444444444.jsonl.zst"
    older.write_bytes(zstandard.ZstdCompressor().compress("".join(json.dumps({"timestamp": "2025-12-31T00:00:00Z", **x}) + "\n" for x in lines).encode()))
    synchronize(journal, project, home, "codex")
    events = [e for g in journal.exchanges(project, "codex", MAIN) for e in g]
    assert [e["content"] for e in events].count("first request") == 1
    assert "listing" in [e["content"] for e in events]


def test_other_projects_compressed_files_are_never_opened(setup):
    journal, project, home, *_ = setup
    folder = home / ".codex" / "sessions" / "2026" / "01" / "01"
    (folder / f"rollout-2026-01-01T00-00-00-{FOREIGN}.jsonl.zst").write_bytes(b"not a zstd file")
    _, report = synchronize(journal, project, home, "codex")
    assert not [gap for gap in report["gaps"] if FOREIGN in str(gap)]


def test_unchanged_rollout_is_skipped_and_a_changed_one_is_read_again(setup, monkeypatch):
    journal, project, home, path, _ = setup
    synchronize(journal, project, home, "codex")
    before = journal.db.execute("SELECT count(*) FROM events").fetchone()[0]

    def fail(_):
        raise AssertionError("an unchanged rollout should not be read")

    with monkeypatch.context() as patch:
        patch.setattr(handoff_capture, "compressed_capture", fail)
        _, report = synchronize(journal, project, home, "codex")
    assert not [gap for gap in report["gaps"] if "Capture failed" in gap["reason"]]
    assert [c["status"] for c in report["conversations"] if c["path"] == str(path.resolve())] == ["unchanged"]
    assert journal.db.execute("SELECT count(*) FROM events").fetchone()[0] == before

    os.utime(path, ns=(1, 1))
    _, report = synchronize(journal, project, home, "codex")
    assert [c["status"] for c in report["conversations"] if c["path"] == str(path.resolve())] == ["imported"]
    assert journal.db.execute("SELECT count(*) FROM events").fetchone()[0] == before


def test_missing_decompressor_is_reported_and_the_database_still_captures(setup, monkeypatch):
    journal, project, home, *_ = setup
    monkeypatch.setattr(handoff_sessions, "zstd_reader", lambda: None)
    _, report = synchronize(journal, project, home, "codex")
    assert any("pip install zstandard" in gap["reason"] for gap in report["gaps"])
    kinds = {e["kind"] for g in journal.exchanges(project, "codex", MAIN) for e in g}
    assert "user_message" in kinds and "tool_call" not in kinds


def test_old_search_index_migrates_and_stays_in_step(tmp_path):
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE events (id TEXT PRIMARY KEY, session_id TEXT, source TEXT, project TEXT, ordinal INTEGER,
                                 timestamp TEXT, kind TEXT, actor TEXT, content TEXT, metadata TEXT);
            CREATE VIRTUAL TABLE events_fts USING fts5(id UNINDEXED, project UNINDEXED, content, metadata);
        """)
        db.execute("INSERT INTO events VALUES ('a','s','codex',?,0,'t','user_message','user','migrated words','{}')", (str(tmp_path.resolve()),))
        db.execute("INSERT INTO events_fts VALUES ('a',?,'migrated words','{}')", (str(tmp_path.resolve()),))
    journal = Journal(path)
    try:
        assert [row[1] for row in journal.db.execute("PRAGMA table_info(events_fts)")] == ["content"]
        assert [e["id"] for e in journal.search(tmp_path, "migrated")] == ["a"]
        event = make_event("codex", "s", 1, "user_message", "user", "fresh words", "t", {})
        journal.import_session({"id": "s", "source": "codex", "project": str(tmp_path), "path": "p", "updated": "u", "events": [event]})
        assert [e["content"] for e in journal.search(tmp_path, "fresh")] == ["fresh words"]
        journal.db.execute("UPDATE events SET content='changed text' WHERE id=?", (event["id"],))
        assert journal.search(tmp_path, "fresh") == []
        assert [e["content"] for e in journal.search(tmp_path, "changed")] == ["changed text"]
        journal.db.execute("DELETE FROM events WHERE id=?", (event["id"],))
        assert journal.search(tmp_path, "changed") == []
    finally:
        journal.close()


def _chat(journal, project, sid, contents):
    events = [make_event("codex", sid, i, kind, "user" if kind == "user_message" else "assistant", text, "2026-01-01T00:00:00Z", {})
              for i, (kind, text) in enumerate(contents)]
    journal.import_session({"id": sid, "source": "codex", "project": str(project), "path": str(project / sid), "updated": "u", "events": events})


def test_decisions_ignore_context_terminal_pastes_and_the_agents_own_words(tmp_path):
    journal = Journal(tmp_path / "j.db")
    handoff_capture.initialize(journal.db)
    try:
        _chat(journal, tmp_path, "A", [
            ("user_message", "<system-reminder>Use it only for attribution.</system-reminder>\nI want offline support."),
            ("assistant_message", "Add migrations."),
            ("user_message", "Add migrations."),
            ("user_message", "what now : PS C:\\work> python x.py\nUse --choose to change."),
            ("user_message", "Build"),
            ("user_message", "Please add retries.")])
        assert [item["text"] for item in track(journal, tmp_path)["items"]] == ["I want offline support.", "Please add retries."]
    finally:
        journal.close()


def test_small_talk_does_not_hide_the_last_real_request(tmp_path):
    journal = Journal(tmp_path / "j.db")
    handoff_capture.initialize(journal.db)
    try:
        _chat(journal, tmp_path, "A", [("user_message", "older"), ("assistant_message", "a"), ("user_message", "prepare the meeting notes"),
                                       ("assistant_message", "b"), ("user_message", "Hi"), ("assistant_message", "Hello")])
        context = journal.project_context(tmp_path)
        assert context["latest_request"]["text"] == "prepare the meeting notes"
        assert context["latest_request"]["later_small_talk"] == ["Hi"]
        assert [p["index"] for p in context["recent_exchanges"]] == [2, 1, 0]
    finally:
        journal.close()
