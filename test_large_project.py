import json
import sqlite3
from contextlib import closing

import handoff_capture
from handoff_capture import cursor_tool_metadata, shortened_tool_content, synchronize
from handoff_sessions import Journal
from test_compressed_history import MAIN, agent, call, databases, output, user


def write_plain(home, project, lines):
    folder = home / ".codex" / "sessions" / "2026" / "01" / "01"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-01-01T00-00-00-{MAIN}.jsonl"
    rows = [{"type": "session_meta", "payload": {"id": MAIN, "cwd": str(project)}}, *lines]
    path.write_text("".join(json.dumps({"timestamp": "2026-01-01T00:00:00Z", **row}) + chr(10) for row in rows), encoding="utf-8")


def capture_setup(tmp_path, lines):
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    write_plain(home, project, lines)
    databases(home, project, "unrelated")
    with closing(sqlite3.connect(home / ".codex" / "state_5.sqlite")) as db, db:
        db.execute("INSERT INTO threads VALUES (?,?,?,?,?)", (MAIN, str(project), "", "vscode", "Chat"))
    return home, project


def test_long_tool_output_keeps_both_ends_and_messages_stay_whole(tmp_path):
    long_message = "please read this " * 2000
    long_output = "START" + "x" * 50000 + "END"
    home, project = capture_setup(tmp_path, [user(long_message), call("c1", "ls"), output("c1", long_output), agent("done")])
    journal = Journal(tmp_path / "j.db")
    try:
        synchronize(journal, project, home, "codex")
        events = [e for group in journal.exchanges(project, "codex", MAIN) for e in group]
        assert next(e for e in events if e["kind"] == "user_message")["content"] == long_message
        stored = next(e for e in events if e["kind"] == "tool_result")
        assert stored["content"].startswith("START") and stored["content"].endswith("END")
        assert "characters not stored" in stored["content"] and len(stored["content"]) < 9000
        assert stored["metadata"]["full_chars"] == len(long_output)
        assert stored["metadata"]["source_path"]
    finally:
        journal.close()


def test_a_secret_in_long_tool_output_is_redacted_before_shortening(tmp_path):
    token = "xoxb-" + "1234567890-" * 3 + "abcdef"
    long_output = "y" * 5990 + " " + token + " " + "y" * 50000
    home, project = capture_setup(tmp_path, [user("go"), call("c1", "ls"), output("c1", long_output), agent("done")])
    journal = Journal(tmp_path / "j.db")
    try:
        synchronize(journal, project, home, "codex")
        stored = "".join(row[0] for row in journal.db.execute("SELECT content FROM events"))
        assert "xoxb-" not in stored and "1234567890" not in stored
    finally:
        journal.close()


def test_short_tool_output_is_untouched():
    assert shortened_tool_content("short") is None


def test_large_raw_record_leaves_a_fingerprint_and_small_ones_stay(tmp_path):
    home, project = capture_setup(tmp_path, [
        {"type": "future_small", "payload": {"note": "keep-this-record"}},
        {"type": "future_large", "payload": {"note": "drop-this-record " * 500}},
        user("go"), agent("done")])
    journal = Journal(tmp_path / "j.db")
    try:
        synchronize(journal, project, home, "codex")
        payloads = [row[0] for row in journal.db.execute("SELECT payload FROM capture_records")]
        assert any("keep-this-record" in p for p in payloads)
        assert not any("drop-this-record" in p for p in payloads)
        assert any("not_copied_chars" in p for p in payloads)
    finally:
        journal.close()


def test_a_change_inside_a_large_raw_record_is_still_noticed(tmp_path):
    home, project = capture_setup(tmp_path, [{"type": "future_large", "payload": {"note": "a" * 5000}}, user("go")])
    journal = Journal(tmp_path / "j.db")
    try:
        synchronize(journal, project, home, "codex")
        write_plain(home, project, [{"type": "future_large", "payload": {"note": "b" * 5000}}, user("go")])
        _, changed = synchronize(journal, project, home, "codex")
        assert changed["new_records"] == 1
    finally:
        journal.close()


def test_unchanged_files_are_not_read_again_and_still_count(tmp_path, monkeypatch):
    home, project = capture_setup(tmp_path, [user("first request"), agent("first reply")])
    journal = Journal(tmp_path / "j.db")
    try:
        _, first = synchronize(journal, project, home, "codex")

        def must_not_read(*_):
            raise AssertionError("An unchanged file was read again")

        monkeypatch.setattr(handoff_capture, "jsonl_capture", must_not_read)
        _, repeat = synchronize(journal, project, home, "codex")
        assert not [gap for gap in repeat["gaps"] if "Capture failed" in str(gap)]
        assert repeat["new_events"] == 0
        assert repeat["conversations_captured"] == first["conversations_captured"]
        assert journal.search(project, "first")
    finally:
        journal.close()


def test_a_changed_file_is_read_again(tmp_path):
    home, project = capture_setup(tmp_path, [user("first request")])
    journal = Journal(tmp_path / "j.db")
    try:
        synchronize(journal, project, home, "codex")
        write_plain(home, project, [user("first request"), agent("a new reply")])
        _, changed = synchronize(journal, project, home, "codex")
        assert changed["new_events"] == 1 and journal.search(project, "reply")
    finally:
        journal.close()


def test_cursor_tool_data_keeps_the_call_and_drops_binary_copies():
    kept = cursor_tool_metadata("bubble", {
        "name": "run", "status": "done", "toolCallId": "t1", "toolCallBinary": "B" * 9000,
        "result": "R" * 9000, "rawArgs": json.dumps({"command": "ls"}), "additionalData": {"x": "D" * 9000}})
    assert kept["name"] == "run" and kept["tool_id"] == "t1" and "ls" in kept["arguments"]
    assert not {"toolCallBinary", "result", "additionalData", "rawArgs"} & set(kept)
    assert len(cursor_tool_metadata("b", {"params": {"text": "p" * 9000}})["arguments"]) == 2000


def seeded_session(journal, project, source, session_id, texts):
    from handoff_sessions import make_event, session
    events = [make_event(source, session_id, number, "user_message", "user", text, f"2026-01-01T00:00:{number % 60:02d}Z", {})
              for number, text in enumerate(texts)]
    journal.import_session({**session(source, session_id, str(project), project / session_id, events)})


def test_one_long_chat_cannot_crowd_other_chats_out_of_search(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    journal = Journal(tmp_path / "j.db")
    try:
        seeded_session(journal, project, "cursor", "long", ["work on ticket_categorization " + str(n) for n in range(40)])
        seeded_session(journal, project, "codex", "short", ["other words", "plan for ticket_categorization here"])
        seeded_session(journal, project, "codex", "unrelated", ["nothing relevant"])
        found = journal.search_context(project, "ticket_categorization", limit=5, max_chars=60000)
        assert {c["session_id"] for c in found["matching_chats"]} == {"long", "short"}
        assert next(c for c in found["matching_chats"] if c["session_id"] == "long")["matches"] == 40
        assert {r["session_id"] for r in found["results"]} >= {"long", "short"}
    finally:
        journal.close()


def moved_chat(tmp_path):
    home, project = tmp_path / "home", tmp_path / "project"
    old_folder = project / "old_folder"
    old_folder.mkdir(parents=True)
    write_plain(home, old_folder, [user("work that began in the old folder"), agent("done")])
    rollout = next((home / ".codex" / "sessions").rglob("*.jsonl"))
    databases(home, project, MAIN, str(rollout))
    return home, project, old_folder


def test_a_chat_that_changed_folders_stays_whole_in_its_current_project(tmp_path):
    home, project, old_folder = moved_chat(tmp_path)
    journal = Journal(tmp_path / "j.db")
    try:
        synchronize(journal, project, home, "codex")
        assert journal.search(project, "began")
        _, elsewhere = synchronize(journal, old_folder, home, "codex")
        assert not journal.search(old_folder, "began") and journal.search(project, "began")
        assert not [c for c in elsewhere["conversations"] if c["status"] == "identity_conflict"]
    finally:
        journal.close()


def test_an_earlier_record_under_the_old_folder_moves_to_the_current_project(tmp_path):
    from handoff_sessions import make_event, session
    home, project, old_folder = moved_chat(tmp_path)
    journal = Journal(tmp_path / "j.db")
    try:
        old = make_event("codex", MAIN, 0, "user_message", "user", "stale record from the earlier capture", "2026-01-01T00:00:00Z", {})
        journal.import_session(session("codex", MAIN, str(old_folder), old_folder / "x", [old]))
        _, report = synchronize(journal, project, home, "codex")
        assert not [c for c in report["conversations"] if c["status"] == "identity_conflict"]
        assert journal.search(project, "began") and not journal.search(old_folder, "stale")
        assert not [c for c in journal.conversations(old_folder) if c["id"] == MAIN]
    finally:
        journal.close()


def test_a_chat_owned_by_a_project_codex_still_records_is_not_taken(tmp_path):
    from handoff_sessions import make_event, session
    home, project, _ = moved_chat(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    journal = Journal(tmp_path / "j.db")
    try:
        synchronize(journal, project, home, "codex")
        _, report = synchronize(journal, other, home, "codex")
        assert journal.search(project, "began") and not journal.search(other, "began")
    finally:
        journal.close()


def test_repeat_capture_skips_codex_until_a_file_changes(tmp_path, monkeypatch):
    home, project = capture_setup(tmp_path, [user("first request"), agent("first reply")])
    journal = Journal(tmp_path / "j.db")
    try:
        _, first = synchronize(journal, project, home, "codex")
        real_reader = handoff_capture.codex_sqlite_conversations

        def must_not_read(*_, **__):
            raise AssertionError("Codex was read although nothing changed")

        monkeypatch.setattr(handoff_capture, "codex_sqlite_conversations", must_not_read)
        _, repeat = synchronize(journal, project, home, "codex")
        assert repeat["conversations_captured"] == first["conversations_captured"] and repeat["new_events"] == 0
        assert {c["session_id"] for c in repeat["conversations"]} == {c["session_id"] for c in first["conversations"]}
        monkeypatch.setattr(handoff_capture, "codex_sqlite_conversations", real_reader)
        write_plain(home, project, [user("first request"), agent("first reply"), user("something new")])
        _, changed = synchronize(journal, project, home, "codex")
        assert changed["new_events"] == 1 and journal.search(project, "something")
    finally:
        journal.close()


def test_deleting_history_lets_the_next_capture_restore_it(tmp_path):
    home, project = capture_setup(tmp_path, [user("first request")])
    journal = Journal(tmp_path / "j.db")
    try:
        synchronize(journal, project, home, "codex")
        journal.delete(project)
        assert not journal.search(project, "first")
        synchronize(journal, project, home, "codex")
        assert journal.search(project, "first")
    finally:
        journal.close()


def test_search_results_are_short_and_point_to_the_whole_message(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    journal = Journal(tmp_path / "j.db")
    try:
        seeded_session(journal, project, "codex", "big", ["ticket_categorization " + "word " * 5000])
        found = journal.search_context(project, "ticket_categorization", max_chars=24000)
        event = found["results"][0]["exchanges"][0]["events"][0]
        assert event["content_truncated"] and event["full_chars"] > 20000 and len(event["content"]) == 1500
        whole = journal.read_conversation(project, "codex", "big", 0, 1)["exchanges"][0]["events"][0]
        assert len(whole["content"]) == event["full_chars"]
    finally:
        journal.close()


def test_a_repeated_short_request_in_a_later_file_keeps_its_own_work(tmp_path):
    """The database holds the first file only; the same words later are a new request."""
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    folder = home / ".codex" / "sessions" / "2026" / "01" / "01"
    folder.mkdir(parents=True)
    later = folder / f"rollout-2026-01-02T00-00-00-{MAIN}_55555555-5555-4555-8555-555555555555.jsonl"
    lines = [{"type": "session_meta", "payload": {"id": MAIN, "cwd": str(project)}}, user("new topic"), agent("sure"), user("ok do that"), call("c9", "ls"), output("c9", "listing"), agent("done")]
    later.write_text("".join(json.dumps({"timestamp": "2026-12-01T00:00:00Z", **row}) + chr(10) for row in lines), encoding="utf-8")
    earlier = folder / f"rollout-2026-01-01T00-00-00-{MAIN}.jsonl.zst"
    import zstandard
    old_lines = [{"type": "session_meta", "payload": {"id": MAIN, "cwd": str(project)}}, user("ok do that"), agent("an old reply")]
    earlier.write_bytes(zstandard.ZstdCompressor().compress("".join(json.dumps({"timestamp": "2026-01-01T00:00:00Z", **row}) + chr(10) for row in old_lines).encode()))
    _, history = databases(home, project, MAIN, str(earlier))
    from test_compressed_history import item as compressed_item
    compressed_item(history, MAIN, 1, "userMessage", content=[{"type": "text", "text": "ok do that"}])
    compressed_item(history, MAIN, 2, "agentMessage", text="an old reply")
    journal = Journal(tmp_path / "j.db")
    try:
        synchronize(journal, project, home, "codex")
        groups = [[e["content"] for e in group] for group in journal.exchanges(project, "codex", MAIN)]
        joined = [" | ".join(group) for group in groups]
        assert sum(group.count("ok do that") for group in groups) == 2, joined
        assert any("ok do that" in group and "ls" in group and "listing" in group for group in groups), joined
    finally:
        journal.close()


def test_the_same_words_later_in_one_file_stay_separate_requests(tmp_path):
    home, project = capture_setup(tmp_path, [user("continue"), call("c1", "ls"), output("c1", "one"), agent("first"),
                                             user("something else"), agent("second"),
                                             user("continue"), call("c2", "pwd"), output("c2", "two"), agent("third")])
    journal = Journal(tmp_path / "j.db")
    try:
        synchronize(journal, project, home, "codex")
        groups = [[e["content"] for e in group] for group in journal.exchanges(project, "codex", MAIN)]
        assert [group[0] for group in groups] == ["continue", "something else", "continue"]
    finally:
        journal.close()
