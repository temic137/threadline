import json

from handoff_capture import initialize
from handoff_sessions import Journal, best_window, make_event, query_terms


def add(journal, project, sid, items, day="2026-03-01", child_of=None):
    events = []
    for i, (kind, content, *meta) in enumerate(items):
        actor = "user" if kind == "user_message" else "assistant"
        events.append(make_event("codex", sid, i, kind, actor, content, f"{day}T10:{i:02d}:00Z",
                                 {"capture_order": i, **(meta[0] if meta else {})}))
    journal.import_session({"id": sid, "source": "codex", "project": str(project), "path": str(project / sid),
                            "updated": day, "events": events})
    if child_of:
        initialize(journal.db)
        columns = [row[1] for row in journal.db.execute("PRAGMA table_info(capture_state)")]
        values = {"project": str(project), "source": "codex", "session_id": sid,
                  "details": json.dumps({"metadata": {"parent_thread_id": child_of}})}
        journal.db.execute(f"INSERT INTO capture_state VALUES ({','.join('?' * len(columns))})",
                           [values.get(column, sid) for column in columns])
        journal.db.commit()


def shown(result):
    return json.dumps(result["results"], ensure_ascii=False)


def test_filler_words_are_not_searched():
    assert query_terms("What did we decide about the TablePlus alternative?") == ["decide", "tableplus", "alternative"]
    assert query_terms("what is it") == ["what", "is", "it"]          # nothing left: keep the words


def test_the_matching_exchange_wins_over_newer_ones_sharing_common_words(tmp_path):
    journal = Journal(tmp_path / "j.db")
    project = tmp_path / "p"
    project.mkdir()
    items = [("user_message", "is there a free alternative to TablePlus?"),
             ("assistant_message", "Use DBeaver Community Edition.", {"phase": "final_answer"})]
    for n in range(30):     # later exchanges that share only filler words
        items += [("user_message", f"what did we do about the parser, step {n}?"),
                  ("assistant_message", f"We did step {n}.", {"phase": "final_answer"})]
    add(journal, project, "c1", items)
    found = journal.search_context(project, "What free alternative to TablePlus did we use?", max_chars=24000)
    assert "DBeaver" in shown(found)
    assert found["results"][0]["exchanges"][0]["index"] == 0


def test_long_reply_shows_the_passage_that_matches(tmp_path):
    journal = Journal(tmp_path / "j.db")
    project = tmp_path / "p"
    project.mkdir()
    reply = "filler text. " * 400 + "The Hugging Face Space Spock24/demo is now private. " + "more filler. " * 400
    add(journal, project, "c1", [("user_message", "make the space private"),
                                 ("assistant_message", reply, {"phase": "final_answer"})])
    found = journal.search_context(project, "Hugging Face Space private", max_chars=24000)
    event = next(e for e in found["results"][0]["exchanges"][0]["events"] if e["kind"] == "assistant_message")
    assert "Spock24/demo" in event["content"] and event["content_truncated"] and event["shown_from_char"] > 0
    assert best_window("aaa bbb", ["zzz"], 3) == 0


def test_copies_in_review_tasks_rank_after_the_main_chat(tmp_path):
    journal = Journal(tmp_path / "j.db")
    project = tmp_path / "p"
    project.mkdir()
    add(journal, project, "main", [("user_message", "which branch fixed the identity alias?"),
                                   ("assistant_message", "Fixed on codex/reference-identity-alias.", {"phase": "final_answer"})])
    for n in range(6):
        add(journal, project, f"review{n}", [("user_message", "The following is the Codex agent history: which branch "
                                              "fixed the identity alias? Fixed on codex/reference-identity-alias.")],
            day="2026-03-02", child_of="main")
    found = journal.search_context(project, "branch identity alias", limit=3, max_chars=24000)
    assert found["results"][0]["session_id"] == "main"


def test_a_long_chat_can_give_three_results(tmp_path):
    journal = Journal(tmp_path / "j.db")
    project = tmp_path / "p"
    project.mkdir()
    items = []
    for n in range(5):
        items += [("user_message", f"holdout accuracy run {n}"),
                  ("assistant_message", f"Holdout accuracy was {80 + n}%.", {"phase": "final_answer"})]
    add(journal, project, "c1", items)
    found = journal.search_context(project, "holdout accuracy", limit=5, max_chars=24000)
    assert len(found["results"]) == 3


def test_matches_that_do_not_fit_are_listed_as_snippets_and_can_be_opened(tmp_path):
    journal = Journal(tmp_path / "j.db")
    project = tmp_path / "p"
    project.mkdir()
    for n in range(12):
        add(journal, project, f"s{n}", [("user_message", f"deploy question {n} " + "filler " * 300),
                                        ("assistant_message", f"deploy answer {n} " + "detail " * 300, {"phase": "final_answer"})],
            day=f"2026-03-{n + 1:02d}")
    found = journal.search_context(project, "deploy", limit=5, max_chars=24000)
    assert len(json.dumps(found, ensure_ascii=False)) <= 24000 + 2000
    listed = found["more_matches"]
    shown = {e["id"] for r in found["results"] for x in r["exchanges"] for e in x["events"]}
    assert listed and not shown & {m["event_id"] for m in listed}
    opened = journal.read_exchange_of(project, listed[0]["event_id"])
    assert any(e["id"] == listed[0]["event_id"] for x in opened["exchanges"] for e in x["events"])


def test_a_chat_that_grows_between_searches_is_read_again(tmp_path):
    journal = Journal(tmp_path / "j.db")
    project = tmp_path / "p"
    project.mkdir()
    first = [("user_message", "which database viewer do we use?"),
             ("assistant_message", "DBeaver for the database.", {"phase": "final_answer"})]
    add(journal, project, "c1", first)
    assert journal.search_context(project, "database viewer", max_chars=24000)["results"][0]["total_exchanges"] == 1
    add(journal, project, "c1", first + [("user_message", "and which parser for the statements?"),
                                         ("assistant_message", "pdfplumber parses the statements.", {"phase": "final_answer"})])
    found = journal.search_context(project, "parser statements", max_chars=24000)
    assert "pdfplumber" in shown(found) and found["results"][0]["total_exchanges"] == 2


def test_words_in_most_messages_do_not_outrank_a_rare_one(tmp_path):
    journal = Journal(tmp_path / "j.db")
    project = tmp_path / "p"
    project.mkdir()
    items = [("user_message", "did the periculum id ever come back?"),
             ("assistant_message", "No, the periculum id was never saved.", {"phase": "final_answer"})]
    for n in range(120):     # "test" and "upload" are in nearly every message, so they say little
        items += [("user_message", f"run the test and upload file {n}"),
                  ("assistant_message", f"The test passed and file {n} was uploaded.", {"phase": "final_answer"})]
    add(journal, project, "c1", items)
    found = journal.search_context(project, "periculum test upload", max_chars=24000)
    assert found["results"][0]["exchanges"][0]["index"] == 0


def test_a_message_stored_twice_at_the_same_time_does_not_split_its_exchange(tmp_path):
    journal = Journal(tmp_path / "j.db")
    project = tmp_path / "p"
    project.mkdir()
    add(journal, project, "c1", [("user_message", "make the page responsive on phones"),
                                 ("assistant_message", "Done and published.", {"phase": "final_answer"})])
    copy = make_event("codex", "c1", 9, "user_message", "user", "make the page responsive on phones", "2026-03-01T10:00:00Z", {"capture_order": 9})
    journal.import_session({"id": "c1", "source": "codex", "project": str(project), "path": str(project / "c1"),
                            "updated": "2026-03-01", "events": [copy]})
    groups = journal.exchanges(project, "codex", "c1", kinds=("user_message", "assistant_message"))
    assert [[e["content"] for e in g] for g in groups] == [["make the page responsive on phones", "Done and published."]]
    assert journal.order_problems() == {"copies": 1, "untimed": 0}
