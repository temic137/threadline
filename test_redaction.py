import json

import pytest

from handoff_capture import initialize, stored_report
from handoff_sessions import Journal, make_event, redact

# Assembled at run time so secret scanners do not flag these made-up values.
SLACK = "xoxb-" + "1234567890-1234567890123-" + "abcdefghijklmnopqrstuvwx"
OPENAI = "sk-" + "proj-abcDEF123456_abcDEF"
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghij"


@pytest.mark.parametrize("text,secret", [
    (f"token is {SLACK} ok", SLACK),
    (f"SLACK_BOT_TOKEN={SLACK}", SLACK),
    ("xapp-" + "1-A123-456-abcdef", "1-A123-456-abcdef"),
    (f"key {OPENAI}", OPENAI),
    (JWT, JWT),
    ("postgresql://postgres.abc:Sup3rSecretValue@aws.example.com:5432/postgres", "Sup3rSecretValue"),
    ("DATABASE_URL=postgres://u:Sup3rSecretValue@host/db", "Sup3rSecretValue"),
    ("https://user:Sup3rSecretValue@example.com/repo.git", "Sup3rSecretValue"),
    ("SLACK_SIGNING_SECRET=abcd1234efgh", "abcd1234efgh"),
    ('DB_PASSWORD: "hunter2hunter2"', "hunter2hunter2"),
    ("Authorization: Bearer abcDEF123", "abcDEF123"),
    ("AIza" + "SyA1234567890abcdefghijklmnopqrstuvw", "SyA1234567890abcdefghijklmnopqrstuvw"),
])
def test_credentials_are_removed(text, secret):
    assert secret not in redact(text)
    assert "[REDACTED]" in redact(text)


@pytest.mark.parametrize("text", [
    "authenticates with an Autochek bearer token, or a password",
    "the risk-assessment and task-management docs",
    "see https://example.com:8080/path and C:\\Users\\temi",
    "token = os.environ.get('SLACK_BOT_TOKEN')",
    "SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN}",
    "Use dates like 2026-07-10 at 10:30",
    "the TOKEN: is required",
])
def test_ordinary_text_is_left_alone(text):
    assert redact(text) == text


def test_old_journal_is_cleaned_once_and_stays_usable(tmp_path):
    journal = Journal(tmp_path / "j.db")
    project = str(tmp_path)
    try:
        # A journal from before capture_sources, holding secrets saved under older redaction rules.
        journal.db.executescript("""
            CREATE TABLE capture_records (project TEXT, source TEXT, session_id TEXT, location TEXT,
                record_key TEXT, digest TEXT, payload TEXT, redacted INTEGER,
                PRIMARY KEY(project, source, session_id, location, record_key, digest));
            CREATE TABLE capture_reports (project TEXT PRIMARY KEY, report TEXT);
            CREATE TABLE journal_meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO journal_meta VALUES ('redaction_version', '1');
        """)
        event = make_event("codex", "A", 0, "user_message", "user", f"here is {SLACK} and more", "2026-01-01T00:00:00Z", {"note": SLACK})
        journal.import_session({"id": "A", "source": "codex", "project": project, "path": str(tmp_path / "A"), "updated": "2026-01-01", "events": [event]})
        journal.db.execute("INSERT INTO capture_reports VALUES (?,?)", (project, json.dumps({"note": SLACK, "project": project})))
        journal.db.execute("INSERT INTO capture_records VALUES (?,?,?,?,?,?,?,?)",
                           (project, "codex", "A", "loc", "key", "digest", SLACK, 0))
        journal.db.commit()

        initialize(journal.db)

        stored = journal.get([event["id"]])[0]
        assert stored["content"] == "here is [REDACTED] and more"
        assert SLACK not in json.dumps(stored["metadata"])
        assert journal.search(tmp_path, "more")[0]["id"] == event["id"]
        if journal.fts:
            assert not journal.db.execute("SELECT 1 FROM events_fts WHERE content LIKE ?", (f"%{SLACK}%",)).fetchone()
        assert journal.db.execute("SELECT payload FROM capture_records").fetchone()[0] == "[REDACTED]"
        assert SLACK not in json.dumps(stored_report(journal, tmp_path))
    finally:
        journal.close()


def test_cleaning_an_old_journal_keeps_stored_json_valid(tmp_path):
    journal = Journal(tmp_path / "j.db")
    project = str(tmp_path)
    tricky = {"command": 'set SLACK_TOKEN=abcd1234" && echo "DB_PASSWORD=hunter2hunter2\\"', "url": "postgres://u:Sup3rSecretValue@host/db"}
    try:
        initialize(journal.db)
        event = make_event("codex", "A", 0, "tool_call", "assistant", "call", "2026-01-01T00:00:00Z", tricky)
        journal.import_session({"id": "A", "source": "codex", "project": project, "path": str(tmp_path / "A"), "updated": "2026-01-01", "events": [event]})
        journal.db.execute("INSERT INTO capture_reports VALUES (?,?)", (project, json.dumps({"project": project, "note": tricky})))
        journal.db.execute("UPDATE journal_meta SET value='1' WHERE key='redaction_version'")
        journal.db.commit()

        initialize(journal.db)

        metadata = journal.get([event["id"]])[0]["metadata"]
        assert "Sup3rSecretValue" not in json.dumps(metadata) and "hunter2hunter2" not in json.dumps(metadata)
        assert "abcd1234" not in json.dumps(stored_report(journal, tmp_path))
        assert journal.exchanges(tmp_path, "codex", "A")[0][0]["id"] == event["id"]
    finally:
        journal.close()
