import io
import json
import os
import subprocess
import sys
from pathlib import Path

import handoff_sessions
from handoff_sessions import Journal, make_event, serve_mcp


def _seed(db, project, text):
    journal = Journal(db)
    try:
        event = make_event("codex", "A", 0, "user_message", "user", text, "2026-01-01T00:00:00Z", {})
        journal.import_session({"id": "A", "source": "codex", "project": str(project), "path": str(project / "A"),
                                "updated": "2026-01-01", "events": [event]})
    finally:
        journal.close()


def _serve(db, project, tool, arguments):
    request = {"id": 1, "method": "tools/call", "params": {"name": tool, "arguments": {"project": str(project), **arguments}}}
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    script = Path(handoff_sessions.__file__).with_name("handoff.py")
    process = subprocess.run([sys.executable, str(script), "--mcp", "--db", str(db)], env=env, capture_output=True,
                             input=json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
    assert process.returncode == 0, process.stderr.decode("utf-8", "replace")
    return json.loads(process.stdout)["result"]["structuredContent"]


def test_characters_outside_the_codepage_survive_the_transport(tmp_path):
    text = "Handoff’s step → café \U0001f600"
    _seed(tmp_path / "j.db", tmp_path, text)
    data = _serve(tmp_path / "j.db", tmp_path, "read_conversation", {"source": "codex", "session_id": "A"})
    assert data["exchanges"][0]["events"][0]["content"] == text


def test_non_ascii_arguments_arrive_as_utf8(tmp_path):
    _seed(tmp_path / "j.db", tmp_path, "café menu")
    data = _serve(tmp_path / "j.db", tmp_path, "search_context", {"query": "café"})
    assert "café menu" in json.dumps(data["results"], ensure_ascii=False)


def test_unexpected_tool_failure_keeps_the_server_alive(tmp_path, monkeypatch, capsys):
    def explode(request, journal):
        if request["id"] == 1:
            raise RuntimeError("boom")
        return {"jsonrpc": "2.0", "id": request["id"], "result": {}}

    monkeypatch.setattr(handoff_sessions, "mcp_response", explode)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"id": 1}\n{"id": 2}\n'))
    serve_mcp(tmp_path / "j.db")
    first, second = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert first["id"] == 1 and "boom" in first["error"]["message"]
    assert second == {"jsonrpc": "2.0", "id": 2, "result": {}}
