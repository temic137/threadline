"""`threadline open`: the page where Gemini Nano reads your history, with Google's cloud as a fallback.

The page is hosted on Threadline's Firebase project (so App Check can vouch for its cloud requests) and runs in
Chrome, where Gemini Nano lives. Your history never goes to the host: the page asks this local server for it.
The server listens only on 127.0.0.1, answers only Threadline's page, and only with this run's secret token.
Gemini writes the "where you stopped" summary, which is also handed to your next agent.
"""
from __future__ import annotations

import json
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ASSETS = Path(__file__).parent
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".json": "application/json"}
STATIC = ("index.html", "app.html", "style.css", "ai.js", "app.js", "landing.js", "demo.json")
PAGES = {"": "index.html", "app": "app.html"}  # Clean addresses, as Firebase Hosting serves them.
DEFAULT_PORT = 7878
PAGE = "https://threadline-409ee.web.app"
# Only Threadline's hosted page may read the history from another address.
ALLOWED_ORIGINS = (PAGE, "https://threadline-409ee.firebaseapp.com")
REFRESH_SECONDS = 60


THREAD_PAGE = 8
SEARCH_KINDS = ("user_message", "assistant_message")


def thread_items(journal, chats: list[dict], everything: list[dict]) -> list[dict]:
    from handoff_web.places import ending, last_request, title
    children: dict = {}
    for chat in everything:
        if chat["relationship"] == "child" and chat.get("parent_id"):
            children[chat["parent_id"]] = children.get(chat["parent_id"], 0) + 1
    titles = [title(journal, c)[:120] for c in chats]
    for n, chat in enumerate(chats):
        # A chat continued from another keeps its first message, so several rows would read the same. Name those by
        # what was last asked instead.
        if titles.count(titles[n]) > 1 or titles[n] == "Continued chat":
            titles[n] = last_request(journal, chat) or titles[n]
    return [{"home": c["home"], "source": c["source"], "id": c["id"], "place": c.get("place", ""), "title": t,
             "updated": c["last_activity"], "ending": ending(journal, c), "subtasks": children.get(c["id"], 0)}
            for c, t in zip(chats, titles)]


def place_summary(server, top: str, place: str = "") -> dict:
    """Everything the page shows for a project, or for one sub-project inside it."""
    from handoff_sessions import _SMALL_TALK
    from handoff_web.places import SEVERAL, last_exchanges
    journal, chats = server.journal, server.places.select(top, place)
    mains = [c for c in chats if c["relationship"] != "child"]
    # The newest exchanges across the newest chats, newest first. A chat continued in a new chat (after running out
    # of context) repeats its last request there, sometimes with small differences, so repeats are matched on their
    # start and the copy with the agent's reply is kept.
    found: dict = {}
    for chat in mains[:6]:
        for x in last_exchanges(journal, chat, 3):
            key = " ".join(x["user"].lower().split())[:200] or x["timestamp"]
            kept = found.get(key)
            if not kept or (bool(x["agent"]), x["timestamp"]) > (bool(kept["agent"]), kept["timestamp"]):
                found[key] = x
    recent = sorted(found.values(), key=lambda x: x["timestamp"] or "", reverse=True)[:5]
    asked = next((x for x in recent if x["user"] and not _SMALL_TALK.fullmatch(x["user"].strip())), None)
    ids = {(c["source"], c["id"]) for c in mains}
    name = Path(top).name or top
    label = name if not place else f"{name} / {'Across several' if place == SEVERAL else place}"
    return {"project": top, "place": place, "name": label.split(" / ")[-1], "label": label, "chats": len(mains),
            "agents": sorted({c["source"] for c in mains}),
            "latest_request": {"source": asked["source"], "timestamp": asked["timestamp"], "text": asked["user"][:600]} if asked else None,
            "recent_exchanges": recent,
            "thread": thread_items(journal, mains[:THREAD_PAGE], chats), "thread_total": len(mains)}


TIMELINE_CHATS = 12


def timeline(server, top: str, place: str = "") -> list[dict]:
    """The newest exchanges across the newest chats, oldest first, for the page to group into stretches of work."""
    from handoff_web.places import last_exchanges
    mains = [c for c in server.places.select(top, place) if c["relationship"] != "child"][:TIMELINE_CHATS]
    # A chat continued in a new chat repeats its last request there; keep one copy, the one with the reply.
    found: dict = {}
    for chat in mains:
        for x in last_exchanges(server.journal, chat, 12):
            key = " ".join(x["user"].lower().split())[:200] or x["timestamp"]
            kept = found.get(key)
            if x["timestamp"] and (not kept or (bool(x["agent"]), x["timestamp"]) > (bool(kept["agent"]), kept["timestamp"])):
                found[key] = x
    return [{**x, "user": x["user"][:400], "agent": x["agent"][-500:]} for x in sorted(found.values(), key=lambda x: x["timestamp"])][-60:]


DAY_HOURS = 30


def day(server) -> list[dict]:
    """Every project worked on in the last day of activity, with its exchanges from then, newest project first."""
    from handoff_web.places import _epoch
    tops = server.places.tree()
    newest = _epoch(tops[0]["updated"]) if tops else 0
    recent = [node for node in tops if newest - _epoch(node["updated"]) <= DAY_HOURS * 3600]
    for node in recent:
        server.refresh(node["project"])
    result = []
    for node in recent:
        exchanges = [x for x in timeline(server, node["project"]) if newest - _epoch(x["timestamp"]) <= DAY_HOURS * 3600]
        if exchanges:
            result.append({"project": node["project"], "name": node["name"], "exchanges": exchanges})
    return result


def search(server, top: str, query: str, limit: int = 20) -> list[dict]:
    """Word search over what people and agents said (not tool output) in a project and the projects inside it."""
    import re
    from handoff_web.places import title
    journal = server.journal
    terms = [t.lower() for t in re.findall(r"[\w./:-]+", query) if len(t) > 1]
    results = []
    for home in sorted({c["home"] for c in server.places.select(top)}, key=lambda h: h != top):
        for event in journal.search(Path(home), query, limit=80):
            if event["kind"] not in SEARCH_KINDS:
                continue
            chat = server.places.chat(home, event["source"], event["session_id"])
            text = re.sub(r"\s+", " ", event["content"] or "")
            at = min((i for i in (text.lower().find(t) for t in terms) if i >= 0), default=0)
            start = max(0, at - 80)
            results.append({"home": home, "source": event["source"], "session_id": event["session_id"],
                            "title": title(journal, chat)[:120] if chat else "", "timestamp": event.get("timestamp", ""),
                            "who": "you" if event["kind"] == "user_message" else "agent",
                            "snippet": ("…" if start else "") + text[start:start + 240] + ("…" if start + 240 < len(text) else "")})
            if len(results) >= limit:
                return results
    return results


def save_summary(server, top: str, place: str, text, method) -> dict:
    """Keep the summary the page showed, so the next agent's resume starts from where you stopped."""
    from handoff_decisions import save_note
    chats = server.places.select(top, place)
    if place and not chats:
        raise ValueError("Unknown place")
    through = max((c["last_activity"] for c in chats), default="")
    return save_note(server.journal, Path(top), place, text, method, through)


class Server(HTTPServer):
    # Requests are answered one at a time (they share one journal connection). Let many wait rather than refusing
    # them: a browser opens up to six connections at once, and Python's default lets only five wait.
    request_queue_size = 64
    def __init__(self, address, home: Path, db_path: Path, token: str):
        super().__init__(address, Handler)
        self.home, self.db_path, self.token = home, db_path, token
        self.synced: dict = {}
        self._journal = None
        self._places = None

    @property
    def places(self):
        if self._places is None:
            from handoff_web.places import Places
            self._places = Places(self.journal)
        return self._places

    @property
    def journal(self):
        # Opened by the thread that answers requests: SQLite connections stay in the thread that made them.
        if self._journal is None:
            from handoff_sessions import Journal
            self._journal = Journal(self.db_path)
        return self._journal

    def server_close(self):
        super().server_close()
        if self._journal is not None:
            import sqlite3
            try:
                self._journal.close()
            except sqlite3.ProgrammingError:
                pass  # Closed from another thread (tests); the process ending closes it.
            self._journal = None

    def refresh(self, top: str):
        """Save chats written since the last look, at most once a minute per project (and the projects inside it)."""
        from handoff_capture import synchronize
        for home in sorted({c["home"] for c in self.places.select(top)}) or [top]:
            if time.monotonic() - self.synced.get(home, -1e9) < REFRESH_SECONDS:
                continue
            try:
                synchronize(self.journal, Path(home), self.home)
            except Exception:  # The page still shows what is already saved.
                pass
            self.synced[home] = time.monotonic()


class Handler(BaseHTTPRequestHandler):
    server: Server

    def log_message(self, *args):  # Keep the terminal quiet.
        pass

    def _send(self, status: int, body: bytes, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data):
        self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))

    def _host_ok(self) -> bool:
        # Refuse other host names, so a web page that points its own domain at 127.0.0.1 cannot reach the history.
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"localhost:{port}", f"127.0.0.1:{port}")

    def do_OPTIONS(self):
        # Chrome asks first before Threadline's hosted page may talk to this computer.
        origin = self.headers.get("Origin", "")
        if not self._host_ok() or origin not in ALLOWED_ORIGINS:
            return self._send(403, b"", "text/plain")
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "X-Threadline-Token, Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]
        return origin is None or origin in ALLOWED_ORIGINS or origin in (f"http://localhost:{port}", f"http://127.0.0.1:{port}")

    def _authorized(self) -> bool:
        if not self._origin_ok():
            return False
        return secrets.compare_digest(self.headers.get("X-Threadline-Token", ""), self.server.token)

    def _top(self, query) -> str:
        value = (query.get("project") or [""])[0]
        if not value:
            raise ValueError("Choose a project")
        if not self.server.places.known_top(value):
            raise ValueError("Unknown project")
        return value

    def _home(self, value: str) -> str:
        # Only folders already in the history: the page can't point the server anywhere else.
        if not value or value not in {p["project"] for p in self.server.journal.projects() if p["project"]}:
            raise ValueError("Unknown project")
        return value

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"Open Threadline from the address it printed.", "text/plain")
        url = urlparse(self.path)
        name = url.path.strip("/")
        name = PAGES.get(name, name)
        if name in STATIC:
            return self._send(200, (ASSETS / name).read_bytes(), CONTENT_TYPES[Path(name).suffix])
        if not name.startswith("api/"):
            return self._send(404, b"Not found", "text/plain")
        if not self._authorized():
            return self._json(401, {"error": "Open Threadline from the address it printed in the terminal."})
        query, server = parse_qs(url.query), self.server
        arg = lambda key, default="": (query.get(key) or [default])[0]  # noqa: E731
        try:
            if name == "api/projects":
                return self._json(200, {"projects": server.places.tree()})
            if name == "api/project":
                top = self._top(query)
                server.refresh(top)
                return self._json(200, place_summary(server, top, arg("place")))
            if name == "api/thread":
                top = self._top(query)
                chats = server.places.select(top, arg("place"))
                mains = [c for c in chats if c["relationship"] != "child"]
                offset = max(0, int(arg("offset", "0")))
                return self._json(200, {"thread": thread_items(server.journal, mains[offset:offset + 20], chats)})
            if name == "api/chat":
                from handoff_web.places import last_exchanges
                chat = server.places.chat(self._home(arg("home")), arg("source"), arg("id"))
                if not chat:
                    raise ValueError("Unknown chat")
                count = min(6, max(1, int(arg("count", "2"))))
                return self._json(200, {"title": chat["title"], "source": chat["source"], "exchanges": last_exchanges(server.journal, chat, count)})
            if name == "api/day":
                return self._json(200, {"projects": day(server)})
            if name == "api/timeline":
                top = self._top(query)
                return self._json(200, {"exchanges": timeline(server, top, arg("place"))})
            if name == "api/search":
                top, words = self._top(query), arg("q").strip()[:200]
                return self._json(200, {"results": search(server, top, words) if words else []})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception:  # Explain the failure on the page rather than dropping the connection.
            import traceback
            traceback.print_exc()
            return self._json(500, {"error": "Something went wrong reading your history."})
        return self._json(404, {"error": "Unknown request"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, b"Forbidden", "text/plain")
        if not self._authorized():
            return self._json(401, {"error": "Open Threadline from the address it printed in the terminal."})
        url = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1_000_000:
                raise ValueError("Request too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Send a JSON object")
            if url.path == "/api/summary":
                return self._json(200, save_summary(self.server, self._top({"project": [str(body.get("project", ""))]}),
                                                    str(body.get("place", "")), body.get("text"), body.get("method")))
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        return self._json(404, {"error": "Unknown request"})


def make_server(home: Path, db_path: Path, port: int = DEFAULT_PORT) -> Server:
    token = secrets.token_urlsafe(24)
    for candidate in [port] + list(range(port + 1, port + 10)) + [0]:
        try:
            return Server(("127.0.0.1", candidate), home, db_path, token)
        except OSError:
            continue
    raise OSError("No free port for Threadline's page")


def page_address(server: Server, local_page: bool = False) -> str:
    port = server.server_address[1]
    if local_page:  # Served from this computer: works offline, but Google's cloud fallback cannot vouch for it.
        return f"http://localhost:{port}/app#t={server.token}"
    return f"{PAGE}/app#t={server.token}&p={port}"


def open_page(home: Path, db_path: Path, port: int = DEFAULT_PORT, browser: bool = True, local_page: bool = False) -> int:
    server = make_server(home, db_path, port)
    url = page_address(server, local_page)
    print("Threadline is open at:\n  " + url, flush=True)
    print("Your history stays on this computer; the page reads it from here. Use Chrome for Gemini Nano.", flush=True)
    print("Leave this running while you use the page. Press Ctrl+C to close it.", flush=True)
    if browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nClosed.")
    finally:
        server.server_close()
    return 0
