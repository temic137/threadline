import json
import re
import sqlite3
import subprocess
from pathlib import Path

from handoff import build_handoff, configure, existing_summary, main, redact, update_json_server
from handoff_sessions import Journal, mcp_response, read_claude, read_codex, read_cursor, read_muse, sanitize_session


def write_jsonl(path: Path, records: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


def test_project_capture_three_chats_incremental_and_rewrite(tmp_path):
    from handoff_capture import synchronize
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    paths = []
    for i in range(3):
        path = home / ".codex" / "sessions" / f"chat-{i}.jsonl"
        write_jsonl(path, [
            {"type": "session_meta", "payload": {"id": f"chat-{i}", "cwd": str(project), "git": {"branch": "main"}}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": f"requirement {i}"}},
            {"type": "future_record", "payload": {"text": "api_key=secret123"}},
        ])
        paths.append(path)
    unrelated = home / ".codex" / "sessions" / "other.jsonl"
    write_jsonl(unrelated, [{"type": "session_meta", "payload": {"id": "other", "cwd": str(tmp_path / "other")}}])
    journal = Journal(tmp_path / "capture.db")
    try:
        captures, report = synchronize(journal, project, home, "codex")
        assert {c["id"] for c in captures} == {"chat-0", "chat-1", "chat-2"}
        assert report["new_records"] == 9
        assert report["new_events"] == 3
        archived = "".join(r[0] for r in journal.db.execute("SELECT payload FROM capture_records"))
        assert "future_record" in archived and "secret123" not in archived
        assert sum(c["redacted_records"] for c in report["conversations"]) == 3
        assert all(c["archive_only_record_types"] == {"future_record": 1} for c in report["conversations"])
        _, repeat = synchronize(journal, project, home, "codex")
        assert repeat["new_records"] == repeat["new_events"] == 0
        with paths[0].open("a") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "new correction"}}) + "\n")
        _, appended = synchronize(journal, project, home, "codex")
        assert appended["new_records"] == appended["new_events"] == 1
        write_jsonl(paths[0], [
            {"type": "session_meta", "payload": {"id": "chat-0", "cwd": str(project)}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "replacement"}},
        ])
        synchronize(journal, project, home, "codex")
        assert not journal.search(project, "correction")
        assert any("new correction" in r[0] for r in journal.db.execute("SELECT payload FROM capture_records"))
        paths[1].unlink()
        _, missing = synchronize(journal, project, home, "codex")
        assert any(c["session_id"] == "chat-1" and c["status"] == "unavailable" for c in missing["conversations"])
        assert journal.search(project, "requirement")
        journal.delete(project)
        assert journal.db.execute("SELECT count(*) FROM capture_records").fetchone()[0] == 0
    finally:
        journal.close()


def test_capture_partial_unknown_and_archived(tmp_path):
    from handoff_capture import synchronize
    path = tmp_path / ".codex" / "archived_sessions" / "old.jsonl"
    write_jsonl(path, [{"type": "session_meta", "payload": {"id": "archived", "cwd": str(tmp_path)}}])
    with path.open("a") as stream:
        stream.write('{"partial":')
    journal = Journal(tmp_path / "journal.db")
    try:
        _, report = synchronize(journal, tmp_path, tmp_path, "codex")
        assert report["conversations_captured"] == 1
        assert any("partial" in gap for gap in report["conversations"][0]["gaps"])
        assert report["new_records"] == 2
    finally:
        journal.close()


def test_capture_all_cursor_chats_from_snapshot(tmp_path, monkeypatch):
    from handoff_capture import synchronize
    root = tmp_path / "User" / "workspaceStorage"
    workspace = root / "w1"
    workspace.mkdir(parents=True)
    (workspace / "workspace.json").write_text(json.dumps({"folder": tmp_path.as_uri()}))
    sqlite3.connect(workspace / "state.vscdb").close()
    global_path = root.parent / "globalStorage" / "state.vscdb"
    global_path.parent.mkdir()
    with sqlite3.connect(global_path) as db:
        db.execute("CREATE TABLE composerHeaders (composerId TEXT, workspaceId TEXT, isSubagent INTEGER)")
        db.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
        for sid, wid in (("a", "w1"), ("b", "w1"), ("child", "w1"), ("private", "w2")):
            db.execute("INSERT INTO composerHeaders VALUES (?,?,?)", (sid, wid, int(sid == "child")))
            db.execute("INSERT INTO cursorDiskKV VALUES (?,?)", (f"bubbleId:{sid}:1", json.dumps({"type": 1, "text": sid})))
    monkeypatch.setattr("handoff_sessions.cursor_root", lambda _: root)
    journal = Journal(tmp_path / "capture.db")
    try:
        captures, report = synchronize(journal, tmp_path, tmp_path, "cursor")
        assert {c["id"] for c in captures} == {"a", "b", "child"}
        assert report["new_records"] == 6
        assert not journal.search(tmp_path, "private")
        assert synchronize(journal, tmp_path, tmp_path, "cursor")[1]["new_records"] == 0
    finally:
        journal.close()


def test_capture_failure_does_not_block_other_chats(tmp_path, monkeypatch):
    from handoff_capture import synchronize
    good = tmp_path / ".codex" / "sessions" / "good.jsonl"
    bad = good.with_name("bad.jsonl")
    write_jsonl(good, [{"type": "session_meta", "payload": {"id": "good", "cwd": str(tmp_path)}}])
    write_jsonl(bad, [{"type": "session_meta", "payload": {"id": "bad", "cwd": str(tmp_path)}}, []])
    journal = Journal(tmp_path / "capture.db")
    try:
        captures, report = synchronize(journal, tmp_path, tmp_path, "codex")
        assert {c["id"] for c in captures} == {"good", "bad"}
        # A line that is not a JSON object is skipped; the rest of the chat is still normalized.
        assert not any("Normalization failed" in gap for c in report["conversations"] for gap in c["gaps"])
        assert report["new_records"] == 3
    finally:
        journal.close()


def test_claude_child_identity_and_mcp_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("handoff_sessions.cursor_root", lambda _: tmp_path / "cursor")
    base = tmp_path / ".claude" / "projects" / "project"
    for path, content in ((base / "parent.jsonl", "parent task"),
                          (base / "parent" / "subagents" / "agent-one.jsonl", "child task")):
        write_jsonl(path, [{"sessionId": "parent", "cwd": str(tmp_path), "type": "user",
                            "message": {"role": "user", "content": content}}])
    journal = Journal(tmp_path / "capture.db")
    try:
        response = mcp_response({"id": 1, "method": "tools/call", "params": {
            "name": "resume", "arguments": {"project": str(tmp_path)}}}, journal)
        summary = response["result"]["structuredContent"]["coverage"]
        assert summary["conversations_captured"] == 2 and "conversations" not in summary
        assert len(journal.search(tmp_path, "task")) == 2
        from handoff_capture import coverage_page, stored_report
        report = coverage_page(stored_report(journal, tmp_path), 0, 20)
        assert {c["session_id"] for c in report["conversations"]} == {"parent", "parent/agent-one"}
        assert all("metadata" not in c for c in report["conversations"])
    finally:
        journal.close()


def test_cli_sync_and_export_all_chats(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("handoff.os.path.expanduser", lambda _: str(tmp_path))
    monkeypatch.setattr("handoff_sessions.cursor_root", lambda _: tmp_path / "cursor")
    for index in range(3):
        write_jsonl(tmp_path / ".codex" / "sessions" / f"{index}.jsonl", [
            {"type": "session_meta", "payload": {"id": str(index), "cwd": str(tmp_path)}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": f"unique-chat-{index}"}},
        ])
    args = ["--project", str(tmp_path), "--db", str(tmp_path / "capture.db")]
    assert main(["sync", *args]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["conversations_captured"] == 3
    assert not (tmp_path / "HANDOFF.md").exists()
    assert main(["coverage", *args]) == 0
    assert json.loads(capsys.readouterr().out) == report
    assert main(["export", *args]) == 0
    transcript = (tmp_path / ".handoff" / "transcript.md").read_text()
    assert all(f"unique-chat-{i}" in transcript for i in range(3))
    assert "Capture coverage" in (tmp_path / "HANDOFF.md").read_text()


def test_unreadable_source_reported(tmp_path, monkeypatch):
    from handoff_capture import synchronize
    path = tmp_path / ".codex" / "sessions" / "blocked.jsonl"
    write_jsonl(path, [])
    original = Path.open
    def guarded_open(self, *args, **kwargs):
        if self == path:
            raise PermissionError("access denied")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded_open)
    journal = Journal(tmp_path / "capture.db")
    try:
        _, report = synchronize(journal, tmp_path, tmp_path, "codex")
        assert report["conversations_captured"] == 0
        assert "PermissionError" in report["gaps"][0]["reason"]
    finally:
        journal.close()


def test_redact_removes_secret_values():
    text = redact("api-key: secret123\napi_key='quoted123'\nbearer abcDEF123\nsk-1234567890abcdef\nnormal line")
    assert "secret123" not in text
    assert "sk-1234567890abcdef" not in text
    assert "[REDACTED]" in text
    assert "normal line" in text
    captured = {"events": [{"content": "safe", "metadata": {"command": "api_key='quoted123'"}}]}
    assert sanitize_session(captured)["events"][0]["metadata"]["command"] == "api_key='[REDACTED]'"


def test_build_handoff_contains_git_sections(tmp_path: Path):
    out = build_handoff(
        tmp_path,
        max_diff_lines=50,
        home=tmp_path,
        summary={"goal": "Ship it", "next": "Run tests"},
    )
    assert "## Git" in out
    assert "## Resume prompt" in out
    assert "- Goal: Ship it" in out
    assert "- Next: Run tests" in out
    assert "State fingerprint:" in out
    assert "No matching Codex, Claude Code, Cursor, or Muse Code session" in out


def test_existing_summary_reads_resume_fields(tmp_path: Path):
    path = tmp_path / "HANDOFF.md"
    path.write_text("- Goal: Keep this\n- Done: Parser\n- Next: Tests\n- Blocker: None\n")
    assert existing_summary(path) == {
        "goal": "Keep this",
        "done": "Parser",
        "next": "Tests",
        "blocker": "None",
    }

    path.write_text("- Goal: \n- Done: \n- Next: Work\n- Blocker: \n")
    assert existing_summary(path) == {
        "goal": "",
        "done": "",
        "next": "Work",
        "blocker": "",
    }


def test_build_handoff_includes_staged_and_unstaged_changes(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    tracked = tmp_path / "work.txt"
    tracked.write_text("staged\n")
    subprocess.run(["git", "add", "work.txt"], cwd=tmp_path, check=True)
    tracked.write_text("staged\nunstaged\n")

    out = build_handoff(tmp_path, max_diff_lines=200, home=tmp_path)

    assert "+staged" in out
    assert "+unstaged" in out

    fingerprint = re.search(r"State fingerprint: `([^`]+)`", out).group(1)
    (tmp_path / "HANDOFF.md").write_text("old generated output")
    regenerated = build_handoff(tmp_path, 200, tmp_path)
    assert f"State fingerprint: `{fingerprint}`" in regenerated


def test_prompt_writes_resume_context(tmp_path: Path, monkeypatch):
    answers = iter(("Goal", "Done", "Next", "None"))
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    output = tmp_path / "out.md"

    assert main(["--project", str(tmp_path), "--output", str(output), "--prompt", "--no-history"]) == 0
    assert "- Goal: Goal\n- Done: Done\n- Next: Next\n- Blocker: None" in output.read_text()


def test_check_reports_current_then_stale(tmp_path: Path, capsys):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    work = tmp_path / "work.txt"
    work.write_text("first\n")
    subprocess.run(["git", "add", "work.txt"], cwd=tmp_path, check=True)
    handoff = tmp_path / "custom-handoff.md"

    assert main(["--project", str(tmp_path), "--output", str(handoff), "--no-history"]) == 0
    assert main(["--check", str(handoff)]) == 0
    assert "CURRENT" in capsys.readouterr().out

    work.write_text("changed\n")
    assert main(["--check", str(handoff)]) == 1
    assert "STALE" in capsys.readouterr().out


def test_four_readers_normalize_messages_tools_and_commands(tmp_path: Path):
    codex_path = tmp_path / "codex.jsonl"
    write_jsonl(codex_path, [
        {"type": "session_meta", "payload": {"id": "c1", "cwd": str(tmp_path)}},
        {"timestamp": "1", "type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "UserMessage", "content": "fix it"}}},
        {"timestamp": "2", "type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "AgentMessage", "content": "working"}}},
        {"timestamp": "3", "type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "CommandExecution", "command": "pytest", "aggregated_output": "passed", "exit_code": 0}}},
    ])
    assert [e["kind"] for e in read_codex(codex_path)["events"]] == ["user_message", "assistant_message", "command"]

    codex_current = tmp_path / "codex-current.jsonl"
    write_jsonl(codex_current, [
        {"type": "session_meta", "payload": {"session_id": "c2", "cwd": str(tmp_path)}},
        {"timestamp": "1", "type": "event_msg", "payload": {"type": "user_message", "message": "fix it"}},
        {"timestamp": "2", "type": "event_msg", "payload": {"type": "agent_message", "message": "done", "phase": "final_answer"}},
        {"timestamp": "3", "type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": "x", "arguments": "pytest"}},
        {"timestamp": "4", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "x", "output": "passed"}},
    ])
    assert [e["kind"] for e in read_codex(codex_current)["events"]] == ["user_message", "assistant_message", "tool_call", "tool_result"]

    claude_path = tmp_path / "claude.jsonl"
    write_jsonl(claude_path, [
        {"sessionId": "a1", "cwd": str(tmp_path), "type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "build"}]}},
        {"sessionId": "a1", "cwd": str(tmp_path), "type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "okay"}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest"}}]}},
        {"sessionId": "a1", "cwd": str(tmp_path), "type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "passed"}]}},
    ])
    assert [e["kind"] for e in read_claude(claude_path)["events"]] == ["user_message", "assistant_message", "tool_call", "tool_result"]

    muse_path = tmp_path / "m1" / "session.jsonl"
    write_jsonl(muse_path, [
        {"recorded_at": "1", "payload": {"cwd": str(tmp_path), "event": {"kind": "started", "prompt": "continue"}}},
        {"recorded_at": "2", "payload": {"event": {"kind": "assistant_message_committed", "message": {"content": "done"}}}},
        {"recorded_at": "3", "payload": {"event": {"kind": "tool_result_batch_committed", "results": [{"tool_call_id": "x", "content": "ok"}]}}},
    ])
    assert [e["kind"] for e in read_muse(muse_path)["events"]] == ["user_message", "assistant_message", "tool_result"]

    cursor_path = tmp_path / "cursor" / "state.vscdb"
    cursor_path.parent.mkdir()
    with sqlite3.connect(cursor_path) as db:
        db.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
        db.execute("INSERT INTO ItemTable VALUES (?, ?)", ("composer.chat", json.dumps({"messages": [{"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}], "command": "pytest", "output": "passed"})))
    cursor_events = read_cursor(cursor_path, tmp_path)["events"]
    assert {event["kind"] for event in cursor_events} == {"user_message", "assistant_message", "command"}

    workspace_db = tmp_path / "User" / "workspaceStorage" / "workspace-1" / "state.vscdb"
    global_db = tmp_path / "User" / "globalStorage" / "state.vscdb"
    workspace_db.parent.mkdir(parents=True)
    global_db.parent.mkdir(parents=True)
    sqlite3.connect(workspace_db).close()
    with sqlite3.connect(global_db) as db:
        db.execute("CREATE TABLE composerHeaders (composerId TEXT, workspaceId TEXT, lastUpdatedAt INTEGER, isSubagent INTEGER)")
        db.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
        db.execute("INSERT INTO composerHeaders VALUES ('chat-1', 'workspace-1', 2, 0)")
        bubbles = [
            ("u1", {"type": 1, "createdAt": "1", "text": "question"}),
            ("a1", {"type": 2, "createdAt": "2", "text": "answer"}),
            ("t1-old", {"type": 2, "createdAt": "3", "toolFormerData": {"toolCallId": "tool-1", "name": "shell", "status": "running"}}),
            ("t1", {"type": 2, "createdAt": "4", "toolFormerData": {"toolCallId": "tool-1", "name": "shell", "status": "done", "result": "passed"}}),
        ]
        db.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)",
                       [(f"bubbleId:chat-1:{key}", json.dumps(value)) for key, value in bubbles])
    modern = read_cursor(workspace_db, tmp_path)
    assert modern["id"] == "chat-1"
    assert [event["kind"] for event in modern["events"]] == ["user_message", "assistant_message", "tool_result"]
    assert modern["events"][-1]["content"] == "passed"


def test_automatic_capture_journal_and_mcp(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr("handoff_sessions.cursor_root", lambda home: home / "cursor")
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / "work.py").write_text("print('work')\n")
    home = tmp_path / "home"
    session_path = home / ".codex" / "sessions" / "2026" / "09" / "21" / "rollout-test.jsonl"
    write_jsonl(session_path, [
        {"type": "session_meta", "payload": {"id": "session-1", "cwd": str(project)}},
        {"timestamp": "2026-09-21T01:00:00Z", "type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "UserMessage", "content": "finish the parser api-key: secret123"}}},
        {"timestamp": "2026-09-21T01:01:00Z", "type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "AgentMessage", "content": "parser is half done"}}},
    ])
    monkeypatch.setattr("handoff.os.path.expanduser", lambda _: str(home))
    db_path = tmp_path / "journal.db"
    output = project / "HANDOFF.md"

    assert main(["--project", str(project), "--db", str(db_path)]) == 0
    printed = capsys.readouterr().out
    assert "captured 2 codex events (2 new)" in printed
    assert "finish the parser" in output.read_text()
    assert "secret123" not in output.read_text()
    assert not (project / ".handoff").exists()
    assert main(["export", "--project", str(project), "--db", str(db_path)]) == 0
    assert "parser is half done" in (project / ".handoff" / "transcript.md").read_text()

    journal = Journal(db_path)
    try:
        assert journal.search(project, "parser")[0]["content"] == "parser is half done"
        assert not journal.search(project, "secret123")
        response = mcp_response({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "resume", "arguments": {"project": str(project)}}}, journal)
        context = response["result"]["structuredContent"]
        assert context["recent_exchanges"][0]["events"][0]["content"].startswith("finish the parser")
        assert context["recent_exchanges"][0]["events"][1]["content"] == "parser is half done"
        assert len(context["recent_exchanges"][0]["events"]) == 2
        assert len(journal.get(context["evidence_ids"])) == 2
    finally:
        journal.close()

    assert main(["--check", str(output)]) == 0


def test_config_merge_and_journal_delete(tmp_path: Path):
    config = tmp_path / ".cursor" / "mcp.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"mcpServers": {"other": {"command": "other"}}, "setting": True}))
    ok, _ = update_json_server(config, {"command": "python", "args": ["-m", "handoff", "--mcp"]})
    assert ok
    saved = json.loads(config.read_text())
    assert saved["setting"] is True
    assert saved["mcpServers"]["other"]["command"] == "other"
    assert saved["mcpServers"]["threadline"]["command"] == "python"
    assert config.with_name("mcp.json.handoff.bak").exists()
    assert update_json_server(config, None)[0]
    assert "threadline" not in json.loads(config.read_text())["mcpServers"]

    project, db = tmp_path / "project", tmp_path / "journal.db"
    project.mkdir()
    captured = {"id": "s", "source": "codex", "project": str(project), "path": "x", "updated": "1",
                "events": [{"id": "e", "session_id": "s", "source": "codex", "ordinal": 1,
                            "timestamp": "1", "kind": "user_message", "actor": "user",
                            "content": "architectural decision sqlite", "metadata": {}}]}
    journal = Journal(db)
    try:
        journal.import_session(captured)
        assert journal.search(project, "architectural sqlite")[0]["id"] == "e"
        assert journal.projects()[0]["events"] == 1
        assert journal.delete(project) == 1
        assert not journal.search(project, "sqlite")
    finally:
        journal.close()


def test_setup_and_uninstall_use_isolated_configs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("handoff.shutil.which", lambda name: str(tmp_path / name) if name in ("cursor-agent", "muse") else None)
    assert configure("setup", tmp_path) == 0
    cursor = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    muse = json.loads((tmp_path / ".config" / "muse" / "settings.json").read_text())
    assert cursor["mcpServers"]["threadline"]["args"] == ["-m", "handoff", "--mcp"]
    assert muse["mcpServers"]["threadline"]["required"] is False
    assert configure("uninstall", tmp_path) == 0
    assert "threadline" not in json.loads((tmp_path / ".cursor" / "mcp.json").read_text())["mcpServers"]
    assert "threadline" not in json.loads((tmp_path / ".config" / "muse" / "settings.json").read_text())["mcpServers"]


def test_claude_records_that_copy_messages_are_not_read_as_messages(tmp_path):
    from handoff_sessions import read_claude
    path = tmp_path / "s1.jsonl"
    rows = [
        {"type": "user", "sessionId": "s1", "cwd": "/p", "timestamp": "2026-11-26T09:00:00Z",
         "message": {"role": "user", "content": "Make the page responsive"}},
        {"type": "assistant", "sessionId": "s1", "cwd": "/p", "timestamp": "2026-11-26T09:05:00Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Done, and published."}]}},
        # Claude Code keeps copies of what it sent the model, with no time of their own.
        {"type": "api-request-blob", "sessionId": "s1", "hash": "x",
         "message": {"role": "user", "content": "Make the page responsive"}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows))
    events = read_claude(path)["events"]
    assert [(e["kind"], e["content"]) for e in events] == [("user_message", "Make the page responsive"),
                                                          ("assistant_message", "Done, and published.")]
    assert all(e["timestamp"] for e in events)
