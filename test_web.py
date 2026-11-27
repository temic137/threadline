"""`threadline open`: the local page's server only answers this run's page."""
import http.client
import json
import threading

import pytest

from handoff_capture import synchronize
from handoff_sessions import Journal
from handoff_web import ALLOWED_ORIGINS, PAGE, make_server, page_address


@pytest.fixture
def page(tmp_path, monkeypatch):
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("handoff_sessions.cursor_root", lambda _: home / "cursor")
    monkeypatch.setattr("handoff_sessions.zed_threads_db", lambda _: home / "zed" / "threads.db")
    folder = home / ".claude" / "projects" / "project"
    folder.mkdir(parents=True)
    rows = [("user", "Should I split the parser?", None),
            ("assistant", None, "I can split it into parser.py."),
            ("user", "Go with Postgres for the queue. Skip Redis for now.", None)]
    lines = []
    for n, (kind, user, agent) in enumerate(rows):
        content = user if kind == "user" else [{"type": "text", "text": agent}]
        lines.append(json.dumps({"sessionId": "chat-1", "cwd": str(project), "type": kind,
                                 "timestamp": f"2026-01-01T00:00:0{n}Z", "message": {"role": kind, "content": content}}))
    (folder / "chat-1.jsonl").write_text("\n".join(lines) + "\n")
    db_path = home / ".handoff" / "handoff.db"
    db_path.parent.mkdir(parents=True)
    journal = Journal(db_path)
    synchronize(journal, project, home)
    journal.close()
    server = make_server(home, db_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, project.resolve(), db_path
    server.shutdown()
    server.server_close()


def call(server, method, path, body=None, token=True, host=None, origin=None, headers_out=None):
    port = server.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host or f"localhost:{port}"}
    if origin:
        headers["Origin"] = origin
    if token:
        headers["X-Threadline-Token"] = server.token if token is True else token
    data = json.dumps(body).encode() if body is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body=data, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    if headers_out is not None:
        headers_out.update({k.lower(): v for k, v in response.getheaders()})
    connection.close()
    try:
        return response.status, json.loads(raw)
    except ValueError:
        return response.status, raw.decode()


def test_page_is_served_but_history_needs_the_token(page):
    server, project, _ = page
    status, text = call(server, "GET", "/", token=False)
    assert status == 200 and "Threadline" in text
    for path in ("/app", "/app.js", "/ai.js", "/style.css", "/landing.js", "/demo.json"):
        assert call(server, "GET", path, token=False)[0] == 200, path
    assert call(server, "GET", "/api/projects", token=False)[0] == 401
    assert call(server, "GET", "/api/projects", token="guess")[0] == 401
    assert call(server, "GET", "/api/projects", host="evil.example:80")[0] == 403, "another site pointed at 127.0.0.1 is refused"
    assert call(server, "GET", "/../handoff.py", token=False)[0] == 404
    assert call(server, "POST", "/api/summary", {"project": str(project), "text": "x", "method": "gemini_nano"}, token=False)[0] == 401


def test_projects_thread_messages_and_summary(page):
    server, project, _ = page
    status, data = call(server, "GET", "/api/projects")
    assert status == 200 and [p["project"] for p in data["projects"]] == [str(project)] and data["projects"][0]["chats"] == 1
    status, data = call(server, "GET", f"/api/project?project={project}")
    assert status == 200 and data["chats"] == 1 and "Postgres" in data["latest_request"]["text"]
    assert data["thread"][0]["ending"] == "I can split it into parser.py." and data["thread_total"] == 1
    assert data["recent_exchanges"][0]["user"].startswith("Go with Postgres") or data["recent_exchanges"][-1]["user"].startswith("Go with")
    chat = data["thread"][0]
    status, detail = call(server, "GET", f"/api/chat?home={chat['home']}&source={chat['source']}&id={chat['id']}")
    assert status == 200 and detail["exchanges"][-1]["user"].startswith("Go with Postgres")
    assert call(server, "GET", "/api/project?project=/etc")[0] == 400, "only projects already in the history"
    assert call(server, "GET", "/api/chat?home=/etc&source=claude&id=chat-1")[0] == 400


def test_cards_and_timeline_get_what_they_need(page):
    """Chat cards read a chat's last few exchanges; the timeline gets exchanges across chats, oldest first."""
    server, project, _ = page
    chat = call(server, "GET", f"/api/project?project={project}")[1]["thread"][0]
    status, detail = call(server, "GET", f"/api/chat?home={chat['home']}&source={chat['source']}&id={chat['id']}&count=4")
    assert status == 200 and [x["user"][:6] for x in detail["exchanges"]] == ["Should", "Go wit"]
    status, data = call(server, "GET", f"/api/timeline?project={project}")
    stamps = [x["timestamp"] for x in data["exchanges"]]
    assert status == 200 and stamps == sorted(stamps) and data["exchanges"][-1]["user"].startswith("Go with Postgres")
    assert call(server, "GET", "/api/timeline?project=/etc")[0] == 400


def test_your_day_lists_the_projects_worked_on(page):
    server, project, _ = page
    status, data = call(server, "GET", "/api/day")
    assert status == 200 and [p["project"] for p in data["projects"]] == [str(project)]
    assert data["projects"][0]["exchanges"][-1]["user"].startswith("Go with Postgres")
    assert call(server, "GET", "/api/day", token=False)[0] == 401


def test_search_finds_what_was_said(page):
    server, project, _ = page
    status, data = call(server, "GET", f"/api/search?project={project}&q=Redis")
    assert status == 200 and data["results"][0]["who"] == "you" and "Skip Redis" in data["results"][0]["snippet"]
    assert call(server, "GET", f"/api/search?project={project}&q=")[1]["results"] == []


def test_the_pages_summary_is_what_the_next_agent_reads(page):
    """The "where you stopped" summary the page showed reaches the next agent's resume, without secrets."""
    server, project, db_path = page
    summary = {"project": str(project), "place": "", "method": "gemini_nano",
               "text": "You chose Postgres for the queue. [[Wire the queue into the parser]] is next. api_key=sk-live-abcdef123456"}
    assert call(server, "POST", "/api/summary", summary) == (200, {"saved": True})
    assert call(server, "POST", "/api/summary", {**summary, "method": "someone"})[0] == 400
    assert call(server, "POST", "/api/summary", {**summary, "project": "/etc"})[0] == 400
    journal = Journal(db_path)
    try:
        context = journal.project_context(project)
    finally:
        journal.close()
    note = context["where_you_stopped"]
    assert note["text"].startswith("You chose Postgres") and "[[" not in note["text"] and "sk-live" not in note["text"]
    assert note["written_by"] == "Gemini Nano on this computer" and note["current"] is True


def test_only_threadlines_hosted_page_may_read_from_another_address(page):
    server, _, _ = page
    seen = {}
    assert call(server, "OPTIONS", "/api/projects", token=False, origin=PAGE, headers_out=seen)[0] == 204
    assert seen["access-control-allow-origin"] == PAGE and "X-Threadline-Token" in seen["access-control-allow-headers"]
    assert seen["access-control-allow-private-network"] == "true"
    assert call(server, "OPTIONS", "/api/projects", token=False, origin="https://evil.example")[0] == 403
    seen.clear()
    assert call(server, "GET", "/api/projects", origin=ALLOWED_ORIGINS[1], headers_out=seen)[0] == 200
    assert seen["access-control-allow-origin"] == ALLOWED_ORIGINS[1]
    seen.clear()
    status, _ = call(server, "GET", "/api/projects", origin="https://evil.example", headers_out=seen)
    assert status == 401 and "access-control-allow-origin" not in seen, "even with the token, another site is refused"


def test_open_points_at_the_hosted_page_with_token_and_port(page):
    server, _, _ = page
    port = server.server_address[1]
    assert page_address(server) == f"{PAGE}/app#t={server.token}&p={port}"
    assert page_address(server, local_page=True) == f"http://localhost:{port}/app#t={server.token}"


def test_sample_projects_are_complete():
    from handoff_web import ASSETS
    projects = json.loads((ASSETS / "demo.json").read_text())["projects"]
    assert len(projects) >= 2 and any(c["place"] for p in projects for c in p["chats"]), "the samples show sub-projects"
    for p in projects:
        assert p["chats"] and {"project", "chats"} <= set(p)
        for chat in p["chats"]:
            assert {"home", "source", "id", "place", "updated", "title", "exchanges"} <= set(chat)
            for exchange in chat["exchanges"]:
                assert {"user", "agent", "timestamp"} <= set(exchange)


def test_the_site_serves_the_same_installers_as_the_repository():
    from pathlib import Path
    from handoff_web import ASSETS
    for name in ("install.sh", "install.ps1"):
        assert (ASSETS / name).read_bytes() == (Path(__file__).parent / name).read_bytes(), f"copy {name} into handoff_web/"


def test_many_requests_at_once_wait_their_turn_instead_of_being_dropped(page):
    """A browser opens up to six connections at once; the server answers one at a time, so the rest must wait."""
    server, project, _ = page
    results = []
    workers = [threading.Thread(target=lambda: results.append(call(server, "GET", f"/api/project?project={project}")[0]))
               for _ in range(24)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert results == [200] * 24
