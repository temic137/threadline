"""Failure-path and process-level checks for project capture."""
import json
import os
import sqlite3
import subprocess
import sys
import shutil
import zipfile
from pathlib import Path


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
        {"id": 3, "method": "tools/call", "params": {"name": "resume", "arguments": {"project": str(project)}}},
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
    assert replies[2]["result"]["structuredContent"]["coverage"]["conversations_captured"] == 3
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
    for name in ("pyproject.toml", "README.md", "LICENSE", "handoff.py", "handoff_sessions.py", "handoff_capture.py",
                 "handoff_decisions.py", "handoff_meaning.py", "handoff_agents.py"):
        shutil.copy2(repo / name, source / name)
    shutil.copytree(repo / "handoff_web", source / "handoff_web", ignore=shutil.ignore_patterns("__pycache__"))
    built = subprocess.run([sys.executable, "-m", "pip", "wheel", str(source), "--no-deps", "--no-build-isolation",
                            "--wheel-dir", str(tmp_path / "wheels")], capture_output=True, text=True, timeout=60)
    assert built.returncode == 0, built.stdout + built.stderr
    wheel = next((tmp_path / "wheels").glob("*.whl"))
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel) as archive:
        assert "handoff_capture.py" in archive.namelist()
        assert {"handoff_web/index.html", "handoff_web/app.html", "handoff_web/test.html", "handoff_web/ai.js", "handoff_web/app.js",
                "handoff_web/landing.js", "handoff_web/decision_test.json", "handoff_web/demo.json"} <= set(archive.namelist())
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
        records = [("/p", "codex", "A", "/long/path/a.jsonl", f"line:{n}", f"d{n}", '{"text": "hello %d %s"}' % (n, "x" * 80), 0) for n in range(3)]
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
        stored = dict(journal.db.execute("SELECT record_key, payload FROM capture_archive a JOIN capture_sources s ON s.id=a.src WHERE s.source='codex'").fetchall())
        assert all(isinstance(value, bytes) for value in stored.values())  # Long values are compressed.

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


def test_review_requests_and_repeated_tool_output_are_slimmed_once(tmp_path):
    from handoff_capture import initialize, slim_session
    from handoff_sessions import Journal, make_event
    journal = Journal(tmp_path / "j.db")
    project = str(tmp_path)
    try:
        initialize(journal.db)
        history = "The following is the Codex agent history whose request action you are assessing.\n" + "old turn " * 5000
        action = '>>> APPROVAL REQUEST START\nPlanned action JSON:\n{"command": ["ls"]}\n>>> APPROVAL REQUEST END'
        output = "file contents line\n" * 100
        user_text = "please do it " * 100
        events = [
            make_event("codex", "R", 0, "user_message", "user", history + action, "2026-01-01T00:00:00Z", {}),
            make_event("codex", "R", 1, "assistant_message", "assistant", '{"outcome":"allow"}', "2026-01-01T00:00:01Z", {}),
            make_event("codex", "R", 2, "tool_result", "tool", output, "2026-01-01T00:00:02Z", {}),
            make_event("codex", "R", 3, "tool_result", "tool", output, "2026-01-01T00:00:03Z", {}),
            make_event("codex", "R", 4, "tool_result", "tool", "short", "2026-01-01T00:00:04Z", {}),
            make_event("codex", "R", 5, "tool_result", "tool", "short", "2026-01-01T00:00:05Z", {}),
            make_event("codex", "R", 6, "user_message", "user", user_text, "2026-01-01T00:00:06Z", {}),
            make_event("codex", "R", 7, "user_message", "user", user_text, "2026-01-01T00:00:07Z", {}),
        ]
        journal.import_session({"id": "R", "source": "codex", "project": project, "path": "x", "updated": "2026-01-01", "events": events})
        assert slim_session(journal.db, project, "codex", "R") == 2
        assert slim_session(journal.db, project, "codex", "R") == 0
        content = {row[0]: row[1] for row in journal.db.execute("SELECT ordinal, content FROM events")}
        assert content[0].startswith("[Codex review request;") and content[0].endswith(action) and "old turn" not in content[0]
        assert content[1] == '{"outcome":"allow"}'
        assert content[2] == output
        assert content[3] == f"[Same output as earlier tool result {events[2]['id']} in this chat; {len(output)} characters; sha256 " + content[3][-17:]
        assert content[4] == content[5] == "short"
        assert content[6] == content[7] == user_text  # People's messages are never changed.
        assert {row[0] for row in journal.db.execute("SELECT id FROM events")} == {e["id"] for e in events}

        # A later capture replaces the first copy: the repeat then points to the new one.
        journal.db.execute("DELETE FROM events WHERE ordinal=2")
        moved = make_event("codex", "R", 2, "tool_result", "tool", output, "2026-01-01T00:00:02Z", {"capture_order": 9})
        journal.import_session({"id": "R", "source": "codex", "project": project, "path": "x", "updated": "2026-01-01", "events": [moved]})
        assert slim_session(journal.db, project, "codex", "R") == 1
        assert moved["id"] in journal.db.execute("SELECT content FROM events WHERE ordinal=3").fetchone()[0]
        if journal.fts:
            assert not journal.search(tmp_path, "turn")
    finally:
        journal.close()


def test_old_journal_is_slimmed_on_first_open(tmp_path):
    from handoff_capture import initialize
    from handoff_sessions import Journal, make_event
    journal = Journal(tmp_path / "j.db")
    try:
        output = "x" * 600
        events = [make_event("cursor", "C", n, "tool_result", "tool", output, f"2026-01-01T00:00:0{n}Z", {}) for n in range(3)]
        journal.import_session({"id": "C", "source": "cursor", "project": str(tmp_path), "path": "x", "updated": "2026-01-01", "events": events})
        initialize(journal.db)
        stored = [row[0] for row in journal.db.execute("SELECT content FROM events ORDER BY ordinal")]
        assert stored[0] == output and all(s.startswith("[Same output as earlier tool result") for s in stored[1:])
        assert journal.db.execute("SELECT value FROM journal_meta WHERE key='slim_version'").fetchone()[0] == "1"
    finally:
        journal.close()


def _plain_journal_with_history(path, count):
    """A journal as written before events_data: messages as plain text in an events table."""
    import sqlite3
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE events (id TEXT PRIMARY KEY, session_id TEXT, source TEXT, project TEXT,
            ordinal INTEGER, timestamp TEXT, kind TEXT, actor TEXT, content TEXT, metadata TEXT);
        CREATE VIRTUAL TABLE events_fts USING fts5(content, content='events', content_rowid='rowid');
    """)
    rows = [(f"e{n}", "S", "codex", str(path.parent), n, f"2026-01-01T00:{n // 60 % 60:02d}:{n % 60:02d}Z",
             "tool_result", "tool", f"Output {n}: the build finished with {n % 7} warnings in module_{n % 13}.py " * 3,
             json.dumps({"source_path": "/Users/me/.codex/sessions/rollout-2026-01-01.jsonl", "capture_order": n}))
            for n in range(count)]
    db.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    db.execute("INSERT INTO events_fts(events_fts) VALUES ('rebuild')")
    db.commit()
    db.close()
    return rows


def test_plain_events_move_to_compressed_storage_and_stay_searchable(tmp_path):
    from handoff_sessions import Journal
    rows = _plain_journal_with_history(tmp_path / "j.db", 1500)
    journal = Journal(tmp_path / "j.db")
    try:
        assert journal.db.execute("SELECT type FROM sqlite_master WHERE name='events'").fetchone()[0] == "view"
        assert sorted(journal.codec.latest) == ["content", "metadata"]
        stored = journal.db.execute("SELECT content, metadata FROM events_data WHERE id='e5'").fetchone()
        assert isinstance(stored[0], bytes) and isinstance(stored[1], bytes)
        assert [tuple(r) for r in journal.db.execute("SELECT * FROM events ORDER BY ordinal")] == rows
        if journal.fts:
            found = journal.search(tmp_path, "module_12.py")
            assert found and all("module_12.py" in e["content"] for e in found)
        # Writes through the view are compressed, and edits and deletes reach the index.
        journal.db.execute("UPDATE events SET content=? WHERE id='e5'", ("replacement words " * 10,))
        journal.db.execute("DELETE FROM events WHERE id='e6'")
        journal.db.commit()
        assert isinstance(journal.db.execute("SELECT content FROM events_data WHERE id='e5'").fetchone()[0], bytes)
        if journal.fts:
            assert [e["id"] for e in journal.search(tmp_path, "replacement")] == ["e5"]
            assert all(e["id"] != "e6" for e in journal.search(tmp_path, "module_6.py", limit=500))
    finally:
        journal.close()
    reopened = Journal(tmp_path / "j.db")  # A second open converts nothing again.
    try:
        assert reopened.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1499
    finally:
        reopened.close()


def test_compact_retrains_and_old_dictionaries_still_read(tmp_path):
    from handoff_sessions import Journal, make_event
    _plain_journal_with_history(tmp_path / "j.db", 1500)
    journal = Journal(tmp_path / "j.db")
    try:
        first = journal.codec.latest["content"]
        event = make_event("codex", "S", 99999, "assistant_message", "assistant", "A reply long enough to be compressed. " * 4, "2026-02-01", {})
        newer = [make_event("claude", "T", n, "user_message", "user", f"Please rename variable_{n} and update docs/page_{n}.md " * 3,
                            "2026-02-02", {"cwd": "/Users/me/project"}) for n in range(3000)]
        journal.import_session({"id": "S", "source": "codex", "project": str(tmp_path), "path": "x", "updated": "2026-02-01", "events": [event]})
        journal.import_session({"id": "T", "source": "claude", "project": str(tmp_path), "path": "y", "updated": "2026-02-02", "events": newer})
        # A newer dictionary from different text (zstd names a dictionary after its contents, so the
        # same text would give the same one); rows made with the first still read.
        journal.codec.train(journal.db, {"content": [f"zebra {n} quantum {n * 7} lattice harbor {n % 11} " * 4 for n in range(1500)]})
        journal.db.commit()
        assert journal.codec.latest["content"] != first
        assert journal.get([event["id"]])[0]["content"] == event["content"]
        journal.compact()
        assert journal.db.execute("SELECT count(*) FROM zstd_dicts").fetchone()[0] == 2  # Only the newest per kind.
        assert journal.get([event["id"]])[0]["content"] == event["content"]
        assert journal.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        journal.close()


def test_every_module_is_packaged():
    """A module missing from py-modules installs fine but crashes on first use."""
    import re
    repo = Path(__file__).parent
    listed = set(re.findall(r'"(handoff\w*)"', re.search(r"py-modules\s*=\s*\[(.*?)\]", (repo / "pyproject.toml").read_text(), re.S).group(1)))
    present = {p.stem for p in repo.glob("handoff*.py")}
    assert present <= listed, f"add to py-modules in pyproject.toml: {sorted(present - listed)}"
