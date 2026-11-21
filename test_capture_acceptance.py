"""Failure-path and process-level checks for project capture."""
import json
import os
import sqlite3
import subprocess
import sys
import shutil
import zipfile
from pathlib import Path

import pytest

from handoff_capture import synchronize
from handoff_sessions import Journal


def codex(path, project, sid, messages):
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [{"type": "session_meta", "payload": {"id": sid, "cwd": str(project)}}]
    records += [{"type": "event_msg", "payload": {"type": "user_message", "message": text}} for text in messages]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_selected_claude_child(tmp_path):
    path = tmp_path / ".claude/projects/p/parent/subagents/child.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"sessionId": "parent", "cwd": str(tmp_path), "type": "user",
                                "message": {"role": "user", "content": "child work"}}) + "\n")
    journal = Journal(tmp_path / "journal.db")
    try:
        captured, _ = synchronize(journal, tmp_path, tmp_path, "claude", "parent/child")
        assert [c["id"] for c in captured] == ["parent/child"]
    finally:
        journal.close()


def test_legacy_cursor_json_secret_redacted(tmp_path, monkeypatch):
    workspace = tmp_path / "User/workspaceStorage/w1"
    workspace.mkdir(parents=True)
    (workspace / "workspace.json").write_text(json.dumps({"folder": tmp_path.as_uri()}))
    with sqlite3.connect(workspace / "state.vscdb") as db:
        db.execute("CREATE TABLE ItemTable(key TEXT,value TEXT)")
        db.execute("INSERT INTO ItemTable VALUES (?,?)", ("composer", json.dumps({"api_key": "sensitive-value",
                    "messages": [{"role": "user", "content": "hello"}]})))
    monkeypatch.setattr("handoff_sessions.cursor_root", lambda _: workspace.parent)
    journal = Journal(tmp_path / "journal.db")
    try:
        _, report = synchronize(journal, tmp_path, tmp_path, "cursor")
        assert report["conversations_captured"] == 1
        payloads = "".join(row[0] for row in journal.db.execute("SELECT payload FROM capture_records"))
        assert "sensitive-value" not in payloads
    finally:
        journal.close()


def test_storage_failure_rolls_back_entire_conversation(tmp_path, monkeypatch):
    path = tmp_path / ".codex/sessions/a.jsonl"
    codex(path, tmp_path, "a", ["original"])
    journal = Journal(tmp_path / "journal.db")
    try:
        synchronize(journal, tmp_path, tmp_path, "codex")
        codex(path, tmp_path, "a", ["replacement"])
        def fail(*args, **kwargs):
            raise sqlite3.OperationalError("injected write failure")
        with monkeypatch.context() as patch:
            patch.setattr(journal, "import_session", fail)
            _, report = synchronize(journal, tmp_path, tmp_path, "codex")
        assert report["new_records"] == report["new_events"] == 0
        assert report["gaps"]
        assert journal.search(tmp_path, "original")
        assert not journal.search(tmp_path, "replacement")
        assert journal.db.execute("SELECT count(*) FROM capture_records").fetchone()[0] == 2
        _, retried = synchronize(journal, tmp_path, tmp_path, "codex")
        assert retried["new_records"] == 1
        assert journal.search(tmp_path, "replacement")
    finally:
        journal.close()


def test_partial_record_repaired_and_history_over_500(tmp_path):
    path = tmp_path / ".codex/sessions/a.jsonl"
    codex(path, tmp_path, "a", [f"message-{i}" for i in range(1200)])
    with path.open("a") as stream:
        stream.write('{"type":"event_msg","payload":')
    journal = Journal(tmp_path / "journal.db")
    try:
        _, report = synchronize(journal, tmp_path, tmp_path, "codex")
        assert report["new_events"] == 1200
        assert report["new_records"] == 1202
        with path.open("a") as stream:
            stream.write('{"type":"user_message","message":"repaired"}}\n')
        _, report = synchronize(journal, tmp_path, tmp_path, "codex")
        assert report["new_events"] == report["new_records"] == 1
        assert journal.search(tmp_path, "message-0")
        assert journal.search(tmp_path, "repaired")
        assert not any("partial JSON" in gap for gap in report["conversations"][0]["gaps"])
    finally:
        journal.close()


def test_source_changed_during_read_is_not_committed(tmp_path, monkeypatch):
    from handoff_capture import READERS
    path = tmp_path / ".codex/sessions/a.jsonl"
    codex(path, tmp_path, "a", ["before"])
    original = READERS["codex"]
    def mutate(path):
        captured = original(path)
        with path.open("a") as stream:
            stream.write("{}\n")
        return captured
    monkeypatch.setitem(READERS, "codex", mutate)
    journal = Journal(tmp_path / "journal.db")
    try:
        _, report = synchronize(journal, tmp_path, tmp_path, "codex")
        assert report["conversations_captured"] == report["new_records"] == 0
        assert "changed during capture" in report["gaps"][0]["reason"]
    finally:
        journal.close()


def test_mcp_real_process_with_isolated_home(tmp_path):
    project = tmp_path / "project with spaces"
    project.mkdir()
    for i in range(3):
        codex(tmp_path / f".codex/sessions/{i}.jsonl", project, str(i), [f"task-{i}"])
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
           "APPDATA": str(tmp_path / "AppData"), "PYTHONIOENCODING": "utf-8"}
    requests = [
        {"id": 1, "method": "initialize"},
        {"method": "notifications/initialized"},
        {"id": 2, "method": "tools/list"},
        {"id": 3, "method": "tools/call", "params": {"name": "capture", "arguments": {"project": str(project)}}},
        {"id": 4, "method": "tools/call", "params": {"name": "resume", "arguments": {"project": str(project), "refresh": False}}},
        {"id": 5, "method": "tools/call", "params": {"name": "resume", "arguments": {"project": str(project), "source": "codex", "session_id": "2", "refresh": False}}},
        {"id": 6, "method": "tools/call", "params": {"name": "search_context", "arguments": {"project": str(project), "query": "task-0"}}},
        {"id": 7, "method": "tools/call", "params": {"name": "read_conversation", "arguments": {"project": str(project), "source": "codex", "session_id": "0", "start": 0}}},
    ]
    process = subprocess.run([sys.executable, "-m", "handoff", "--mcp", "--db", str(tmp_path / "journal.db")],
                             input="\n".join(json.dumps(r) for r in requests) + "\n", text=True,
                             encoding="utf-8", capture_output=True, env=env, timeout=30)
    assert process.returncode == 0, process.stderr
    replies = [json.loads(line) for line in process.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3, 4, 5, 6, 7]
    assert replies[2]["result"]["structuredContent"]["conversations_captured"] == 3
    assert replies[3]["result"]["structuredContent"]["status"] == "project_ready"
    assert len(replies[3]["result"]["structuredContent"]["conversations"]) == 3
    assert replies[4]["result"]["structuredContent"]["session_id"] == "2"
    assert replies[5]["result"]["structuredContent"]["results"][0]["session_id"] == "0"
    assert replies[6]["result"]["structuredContent"]["exchanges"][0]["events"][0]["content"] == "task-0"


def test_duplicate_session_ids_do_not_overwrite_other_project(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    codex(tmp_path / ".codex/sessions/first.jsonl", first, "same-id", ["first-message"])
    codex(tmp_path / ".codex/sessions/second.jsonl", second, "same-id", ["second-message"])
    journal = Journal(tmp_path / "journal.db")
    try:
        synchronize(journal, first, tmp_path, "codex")
        _, report = synchronize(journal, second, tmp_path, "codex")
        # A conflicting native identity must be reported, never silently overwrite ownership.
        assert report["gaps"] or any(c["gaps"] and c["status"] == "identity_conflict" for c in report["conversations"])
        assert {p["project"] for p in journal.projects()} >= {str(first)}
    finally:
        journal.close()


def test_codex_native_threads_and_continuation_segments(tmp_path):
    root = tmp_path / ".codex/sessions"
    codex(root / "part-one.jsonl", tmp_path, "parent", ["first segment"])
    codex(root / "part-two.jsonl", tmp_path, "parent", ["second segment"])
    codex(root / "child.jsonl", tmp_path, "child", ["child work"])
    path = root / "child.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[0]["payload"].update(session_id="parent", parent_thread_id="parent")
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    journal = Journal(tmp_path / "journal.db")
    try:
        captured, report = synchronize(journal, tmp_path, tmp_path, "codex")
        assert report["conversations_captured"] == 2
        assert report["source_files_captured"] == 3
        assert {c["id"] for c in captured} == {"parent", "child"}
        assert len(journal.search(tmp_path, "segment")) == 2
        codex(root / "part-two.jsonl", tmp_path, "parent", ["replacement segment"])
        synchronize(journal, tmp_path, tmp_path, "codex")
        assert journal.search(tmp_path, "first")
        assert not journal.search(tmp_path, "second")
        assert journal.search(tmp_path, "replacement")
        for snapshot in journal.db.execute("SELECT * FROM capture_snapshots"):
            restored = []
            for key, digest in json.loads(snapshot["manifest"]):
                row = journal.db.execute("SELECT payload FROM capture_records WHERE project=? AND source=? AND session_id=? AND location=? AND record_key=? AND digest=?",
                    (snapshot["project"], snapshot["source"], snapshot["session_id"], snapshot["location"], key, digest)).fetchone()
                restored.append(json.loads(row[0]))
            assert len(restored) == 2
        journal.delete(tmp_path, "parent")
        assert journal.search(tmp_path, "child")
        assert journal.db.execute("SELECT count(*) FROM capture_snapshots WHERE session_id='parent'").fetchone()[0] == 0
    finally:
        journal.close()


def test_wheel_contains_capture_module_and_runs_outside_checkout(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    repo = Path(__file__).parent
    for name in ("pyproject.toml", "handoff.py", "handoff_sessions.py", "handoff_capture.py", "handoff_decisions.py", "handoff_typellm.py"):
        shutil.copy2(repo / name, source / name)
    built = subprocess.run([sys.executable, "-m", "pip", "wheel", str(source), "--no-deps", "--no-build-isolation",
                            "--wheel-dir", str(tmp_path / "wheels")], capture_output=True, text=True, timeout=60)
    assert built.returncode == 0, built.stdout + built.stderr
    wheel = next((tmp_path / "wheels").glob("*.whl"))
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel) as archive:
        assert "handoff_capture.py" in archive.namelist()
        archive.extractall(installed)
    env = {**os.environ, "PYTHONPATH": str(installed), "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
           "APPDATA": str(tmp_path / "appdata")}
    result = subprocess.run([sys.executable, "-m", "handoff", "sync", "--project", str(tmp_path),
                             "--db", str(tmp_path / "journal.db")], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["conversations_captured"] == 0


def test_old_archive_layout_moves_to_numbered_sources_without_loss(tmp_path):
    from handoff_capture import initialize
    from handoff_sessions import Journal
    journal = Journal(tmp_path / "j.db")
    try:
        journal.db.executescript("""
            CREATE TABLE capture_records (project TEXT, source TEXT, session_id TEXT, location TEXT,
                record_key TEXT, digest TEXT, payload TEXT, redacted INTEGER,
                PRIMARY KEY(project, source, session_id, location, record_key, digest));
            CREATE TABLE capture_event_links (project TEXT, source TEXT, session_id TEXT, location TEXT, event_id TEXT,
                PRIMARY KEY(project, source, session_id, location, event_id));
        """)
        records = [("/p", "codex", "A", "/long/path/a.jsonl", f"line:{n}", f"d{n}", '{"text": "hello %d"}' % n, 0) for n in range(3)]
        records.append(("/p", "claude", "B", "/long/path/b.jsonl", "line:0", "d0", "plain text", 1))
        links = [("/p", "codex", "A", "/long/path/a.jsonl", f"e{n}") for n in range(3)] + [("/p", "claude", "B", "/long/path/b.jsonl", "e9")]
        journal.db.executemany("INSERT INTO capture_records VALUES (?,?,?,?,?,?,?,?)", records)
        journal.db.executemany("INSERT INTO capture_event_links VALUES (?,?,?,?,?)", links)
        journal.db.commit()

        initialize(journal.db)
        initialize(journal.db)  # Running again changes nothing.

        kinds = dict(journal.db.execute("SELECT name, type FROM sqlite_master WHERE name IN ('capture_records','capture_event_links')").fetchall())
        assert kinds == {"capture_records": "view", "capture_event_links": "view"}
        assert sorted(tuple(r) for r in journal.db.execute("SELECT * FROM capture_records")) == sorted(records)
        assert sorted(tuple(r) for r in journal.db.execute("SELECT * FROM capture_event_links")) == sorted(links)
        assert journal.db.execute("SELECT count(*) FROM capture_sources").fetchone()[0] == 2
        assert isinstance(journal.db.execute("SELECT payload FROM capture_archive").fetchone()[0], bytes)

        journal.delete(Path("/p"), "A")
        assert journal.db.execute("SELECT count(*) FROM capture_records").fetchone()[0] == 1
        assert journal.db.execute("SELECT count(*) FROM capture_event_links").fetchone()[0] == 1
    finally:
        journal.close()


def test_compact_shrinks_the_file_and_keeps_history(tmp_path):
    from handoff_sessions import Journal, make_event
    journal = Journal(tmp_path / "j.db")
    try:
        events = [make_event("codex", "A", n, "user_message", "user", "word " * 2000, "2026-01-01T00:00:00Z", {}) for n in range(200)]
        journal.import_session({"id": "A", "source": "codex", "project": str(tmp_path), "path": "x", "updated": "2026-01-01", "events": events})
        journal.db.execute("DELETE FROM events WHERE ordinal >= 10")
        journal.db.commit()
        before, after = journal.compact()
        assert after < before / 4
        assert journal.db.execute("SELECT count(*) FROM events").fetchone()[0] == 10
        assert journal.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        journal.close()
