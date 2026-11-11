import json
import threading

import pytest

np = pytest.importorskip("numpy")

from handoff_meaning import MAX_PASSAGES, MeaningIndex, passages
from handoff_sessions import Journal, mcp_response
from test_search import add

# Words with the same meaning share a dimension, standing in for a real model.
GROUPS = [{"gui", "viewer", "client", "dbeaver", "datagrip"}, {"throttle", "throttled", "429", "rate", "limit"},
          {"invoice", "bill", "supplier"}, {"deploy", "release", "ship"}]


class FakeEmbedder:
    calls = 0

    def __call__(self, texts):
        FakeEmbedder.calls += 1
        out = np.zeros((len(texts), len(GROUPS) + 1), dtype=np.float32)
        for row, text in enumerate(texts):
            words = set(text.lower().replace("?", " ").replace(".", " ").split())
            for column, group in enumerate(GROUPS):
                out[row, column] = len(words & group)
            out[row, -1] = 0.01
        return out


def journal_with_index(tmp_path):
    journal = Journal(tmp_path / "j.db")
    journal.meaning = MeaningIndex(tmp_path / "j.db", embedder=FakeEmbedder())
    project = tmp_path / "p"
    project.mkdir()
    return journal, project


def test_long_messages_are_cut_into_a_few_overlapping_passages():
    assert passages("") == []
    assert [start for start, _ in passages("x" * 500)] == [0]
    pieces = passages("x" * 50000)
    assert len(pieces) == MAX_PASSAGES and pieces[1][0] < len(pieces[0][1])  # overlap, first ~3,000 characters only


def test_search_finds_an_answer_that_shares_no_words_with_the_question(tmp_path):
    journal, project = journal_with_index(tmp_path)
    add(journal, project, "s1", [("user_message", "Which free GUI should replace the paid one?"),
                                 ("assistant_message", "Use DBeaver.", {"phase": "final_answer"})])
    for n in range(20):
        add(journal, project, f"other{n}", [("user_message", f"database table question number {n}"),
                                            ("assistant_message", f"unrelated reply {n}")], day=f"2026-04-{n + 1:02d}")
    journal.meaning.index(project)
    query = "what database client did we pick"
    words_only = Journal(tmp_path / "j.db")
    words_only.meaning = False
    assert words_only.search_context(project, query, limit=1)["searched_by_meaning"] is False
    found = journal.search_context(project, query, limit=3)
    assert found["searched_by_meaning"] is True
    assert "Use DBeaver." in json.dumps(found["results"])


def test_indexing_goes_newest_first_and_resumes_where_it_stopped(tmp_path):
    journal, project = journal_with_index(tmp_path)
    for n in range(50):
        add(journal, project, f"s{n:02d}", [("user_message", f"ship release {n}"), ("assistant_message", f"done {n}")],
            day=f"2026-{1 + n // 28:02d}-{1 + n % 28:02d}")
    stop = threading.Event()
    added = journal.meaning.index(project, progress=lambda done, total: stop.set(), stop=stop)
    assert added == 64
    done = {row[0] for row in journal.db.execute("SELECT event_id FROM meaning_done")}
    newest = [row[0] for row in journal.db.execute(
        "SELECT id FROM events ORDER BY timestamp DESC, ordinal DESC LIMIT 64")]
    assert done == set(newest)
    assert journal.meaning.index(project) == 36
    assert journal.meaning.index(project) == 0
    assert journal.meaning.status(project)["messages_indexed"] == 100


def test_review_copies_are_not_indexed(tmp_path):
    journal, project = journal_with_index(tmp_path)
    add(journal, project, "main", [("user_message", "supplier invoice check"), ("assistant_message", "passed")])
    add(journal, project, "review", [("user_message", "supplier invoice check"), ("assistant_message", "passed")],
        child_of="main")
    journal.meaning.index(project)
    sessions = {row[0] for row in journal.db.execute(
        "SELECT e.session_id FROM meaning_vectors v JOIN events e ON e.id=v.event_id")}
    assert sessions == {"main"}
    assert journal.meaning.status(project) == {"messages_indexed": 2, "messages_total": 2, "indexing": False}


def test_deleting_history_deletes_its_vectors(tmp_path):
    journal, project = journal_with_index(tmp_path)
    add(journal, project, "s1", [("user_message", "rate limit"), ("assistant_message", "throttled")])
    add(journal, project, "s2", [("user_message", "ship it"), ("assistant_message", "released")])
    journal.meaning.index(project)
    journal.delete(project, "s1")
    assert journal.db.execute("SELECT count(*) FROM meaning_done").fetchone()[0] == 2
    journal.delete(project)
    assert journal.db.execute("SELECT count(*) FROM meaning_vectors").fetchone()[0] == 0
    assert journal.db.execute("SELECT count(*) FROM meaning_done").fetchone()[0] == 0


def test_mcp_search_indexes_new_messages_in_the_background(tmp_path):
    journal, project = journal_with_index(tmp_path)
    add(journal, project, "s1", [("user_message", "were we throttled by the provider?"),
                                 ("assistant_message", "Yes, 429 errors.", {"phase": "final_answer"})])
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "search_context", "arguments": {"project": str(project), "query": "rate limit"}}}
    first = mcp_response(call, journal)["result"]["structuredContent"]
    assert "meaning_index" in first
    journal.meaning._threads[str(project.resolve())].join(10)
    second = mcp_response(call, journal)["result"]["structuredContent"]
    assert second["meaning_index"]["messages_indexed"] == 2
    assert second["searched_by_meaning"] is True
    assert "429 errors" in json.dumps(second["results"])


def test_search_still_works_without_the_optional_packages(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_MEANING", "0")
    journal = Journal(tmp_path / "j.db")
    project = tmp_path / "p"
    project.mkdir()
    add(journal, project, "s1", [("user_message", "free alternative to TablePlus"), ("assistant_message", "DBeaver")])
    assert journal.meaning is None
    assert journal.search_context(project, "TablePlus alternative")["results"]
