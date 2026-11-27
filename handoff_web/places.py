"""Where each chat belongs, for the page: a project, and inside it the sub-project the chat worked on.

Agents are often opened at a folder that holds many projects. A chat's own folder then says nothing, so the
sub-project is read from the paths the chat's tool calls, commands and file changes mention. A chat clearly
about one sub-project (most of its mentions) goes there; one spread over several goes to "Across several";
one that mentions none stays at the top. Sub-tasks an agent started follow the chat that started them.
Results are cached per chat version, so only new activity is read again.
"""
from __future__ import annotations

import re
from pathlib import Path

PATH_KINDS = ("tool_call", "file_change", "command", "user_message", "assistant_message")  # Not tool output: too long.
CLEAR_SHARE = 0.6
SEVERAL = "*several"
PROJECT_MARKERS = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "setup.py", "requirements.txt", "pom.xml",
                   "build.gradle", "Gemfile", "composer.json", "deno.json")
SKIP_FOLDERS = {"node_modules", "venv", ".venv", "__pycache__", "dist", "build", "site-packages"}
ENDING_CHARS = 160
# Messages a tool wrote at the start of a chat, not the person: they make poor titles.
NOT_A_TITLE = re.compile(r"(?is)^\s*(?:this session is being continued from a previous conversation|caveat:|<|"
                         r"\[request interrupted|tool loaded\.?\s*$)")
# Messages in the person's turn that the person didn't really write: interruptions, the agent's own summary of a
# long chat, notices from tools. They tell the summary nothing about the work.
NOT_SAID = re.compile(r"(?is)^\s*(?:this session is being continued from a previous conversation|caveat:|\[request interrupted|"
                      r"tool loaded\.?\s*$|<(?:task-notification|command-|local-command|system-reminder|bash-|user-prompt-submit-hook))")


def _initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS page_chat_folders (project TEXT, source TEXT, session_id TEXT, version TEXT,
                  folder TEXT, PRIMARY KEY (project, source, session_id))""")


def _version(db, project: str, source: str, session_id: str, updated: str) -> str:
    try:
        row = db.execute("SELECT version FROM chat_versions WHERE project=? AND source=? AND session_id=?",
                         (project, source, session_id)).fetchone()
    except Exception:  # No version table in this journal yet.
        row = None
    return f"{row[0] if row else ''}:{updated or ''}"


def subfolders(project: Path) -> set[str]:
    """Sub-projects can only exist in a folder that holds projects, not in a project's own folders (src, tests...).
    So a folder that is a project itself, or isn't on this computer, has none."""
    try:
        if any((project / marker).exists() for marker in PROJECT_MARKERS):
            return set()
        return {p.name for p in project.iterdir() if p.is_dir() and not p.name.startswith((".", "_")) and p.name not in SKIP_FOLDERS}
    except OSError:
        return set()


def infer_folder(journal, project: Path, chat: dict, known: set[str]) -> str | None:
    """The sub-project a chat worked on, SEVERAL, or None when it names none."""
    key = str(project)
    if not known:
        return None
    workspace = chat.get("workspace") or key
    if workspace != key and workspace.startswith(key) and workspace[len(key)] in "/\\":
        first = re.split(r"[/\\]", workspace[len(key) + 1:])[0]
        if first:
            return first
    name = re.escape(project.name)
    absolute = re.compile(rf"{name}[/\\]+([A-Za-z0-9_.\-]+)")
    relative = re.compile(r"(?<![\w/.\-])(" + "|".join(sorted(map(re.escape, known), key=len, reverse=True)) + r")[/\\]")
    counts: dict[str, int] = {}
    rows = journal.db.execute(
        f"SELECT content, metadata FROM events WHERE project=? AND source=? AND session_id=? AND kind IN ({','.join('?' * len(PATH_KINDS))})",
        (key, chat["source"], chat["id"], *PATH_KINDS))
    for content, metadata in rows:
        text = f"{content or ''}\n{metadata or ''}"
        for found in absolute.findall(text):
            if found in known:
                counts[found] = counts.get(found, 0) + 1
        for found in relative.findall(text):
            counts[found] = counts.get(found, 0) + 1
    if not counts:
        return None
    top, hits = max(counts.items(), key=lambda item: item[1])
    return top if hits >= CLEAR_SHARE * sum(counts.values()) else SEVERAL


def chat_folders(journal, project: Path, chats: list[dict]) -> dict:
    """{(source, id): folder} for every chat, reading only chats that changed since last time."""
    db = journal.db
    _initialize(db)
    key = str(project)
    cached = {(r[0], r[1]): (r[2], r[3]) for r in db.execute(
        "SELECT source, session_id, version, folder FROM page_chat_folders WHERE project=?", (key,))}
    known = subfolders(project)
    result, changed = {}, False
    for chat in chats:
        if chat["relationship"] == "child":
            continue
        ident = (chat["source"], chat["id"])
        version = _version(db, key, chat["source"], chat["id"], chat.get("updated"))
        if ident in cached and cached[ident][0] == version:
            result[ident] = cached[ident][1]
            continue
        result[ident] = infer_folder(journal, project, chat, known)
        db.execute("INSERT OR REPLACE INTO page_chat_folders VALUES (?,?,?,?,?)", (key, *ident, version, result[ident]))
        changed = True
    if changed:
        db.commit()
    by_id = {chat["id"]: chat for chat in chats}
    for chat in chats:  # Sub-tasks follow the chat that started them, however deep.
        if chat["relationship"] == "child":
            parent, seen = chat, set()
            while parent.get("parent_id") in by_id and parent["id"] not in seen:
                seen.add(parent["id"])
                parent = by_id[parent["parent_id"]]
            result[(chat["source"], chat["id"])] = result.get((parent["source"], parent["id"]))
    return result


def _inside(child: str, parent: str) -> bool:
    return child != parent and child.startswith(parent) and child[len(parent)] in "/\\"


class Places:
    """Every chat with its top project and place. Rebuilt only when the journal's chats change."""

    def __init__(self, journal):
        self.journal = journal
        self._stamp = None
        self.chats: list[dict] = []

    def _fingerprint(self):
        return tuple(self.journal.db.execute("SELECT count(*), max(updated) FROM sessions").fetchone())

    def refresh(self):
        stamp = self._fingerprint()
        if stamp == self._stamp:
            return
        journal = self.journal
        homes = sorted({p["project"] for p in journal.projects() if p["project"]}, key=len)
        tops = [h for h in homes if not any(_inside(h, other) for other in homes)]
        chats = []
        for home in homes:
            top = next((t for t in tops if t == home or _inside(home, t)), home)
            conversations = journal.conversations(Path(home))
            activity = journal.activity(Path(home))
            folders = chat_folders(journal, Path(home), conversations) if home == top else {}
            nested = None if home == top else re.split(r"[/\\]", home[len(top) + 1:])[0]
            for chat in conversations:
                place = nested if nested else folders.get((chat["source"], chat["id"]))
                chats.append({**chat, "home": home, "top": top, "place": place or "",
                              "last_activity": activity.get((chat["source"], chat["id"]), "") or chat.get("updated") or ""})
        chats.sort(key=lambda c: c["last_activity"], reverse=True)
        self.chats, self._stamp = chats, stamp

    def tree(self) -> list[dict]:
        self.refresh()
        tops: dict[str, dict] = {}
        for chat in self.chats:
            node = tops.setdefault(chat["top"], {"project": chat["top"], "name": Path(chat["top"]).name or chat["top"],
                                                 "updated": chat["last_activity"], "chats": 0, "children": {}})
            if chat["relationship"] == "child":
                continue
            node["chats"] += 1
            if chat["place"]:
                child = node["children"].setdefault(chat["place"], {"place": chat["place"], "chats": 0, "updated": chat["last_activity"],
                    "name": "Across several" if chat["place"] == SEVERAL else chat["place"]})
                child["chats"] += 1
        result = []
        for node in sorted(tops.values(), key=lambda n: n["updated"], reverse=True):
            children = sorted(node["children"].values(), key=lambda c: (c["place"] == SEVERAL, -(_epoch(c["updated"]))))
            if len(children) == 1 and children[0]["chats"] == node["chats"]:
                children = []  # One sub-project holding every chat is the project itself.
            result.append({**node, "children": children})
        return result

    def select(self, top: str, place: str = "") -> list[dict]:
        """Chats in a place, newest first. An empty place is the whole project."""
        self.refresh()
        return [c for c in self.chats if c["top"] == top and (not place or c["place"] == place)]

    def known_top(self, top: str) -> bool:
        self.refresh()
        return any(c["top"] == top for c in self.chats)

    def chat(self, home: str, source: str, session_id: str) -> dict | None:
        self.refresh()
        return next((c for c in self.chats if c["home"] == home and c["source"] == source and c["id"] == session_id), None)


def _epoch(stamp: str) -> float:
    from handoff_sessions import _parse_time
    moment = _parse_time(stamp)
    return moment.timestamp() if moment else 0.0


def _first_sentences(text: str, limit: int = ENDING_CHARS) -> str:
    clean = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    clean = re.sub(r"[#*_`>|]+", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    if len(clean) <= limit:
        return clean
    cut = clean[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[:end + 1] if end > limit // 3 else cut.rsplit(" ", 1)[0] + "…"


def title(journal, chat: dict) -> str:
    """The chat's title, or the first thing the person really asked when the stored one is tool boilerplate."""
    from handoff_sessions import _SMALL_TALK, visible_text
    current = (chat.get("title") or "").strip()
    if not NOT_A_TITLE.match(current):
        return current
    rows = journal.db.execute("SELECT content FROM events WHERE project=? AND source=? AND session_id=? AND kind='user_message' "
                              "ORDER BY timestamp, ordinal LIMIT 40", (chat["home"], chat["source"], chat["id"]))
    for (content,) in rows:
        text = re.sub(r"\s+", " ", visible_text(content or "")).strip()
        if text and not NOT_A_TITLE.match(text) and not _SMALL_TALK.fullmatch(text):
            return text[:120]
    return "Continued chat"


def last_request(journal, chat: dict) -> str:
    """The last real thing the person asked in a chat."""
    from handoff_sessions import _SMALL_TALK, visible_text
    rows = journal.db.execute("SELECT content FROM events WHERE project=? AND source=? AND session_id=? AND kind='user_message' "
                              "ORDER BY timestamp DESC, ordinal DESC LIMIT 40", (chat["home"], chat["source"], chat["id"]))
    for (content,) in rows:
        text = re.sub(r"\s+", " ", visible_text(content or "")).strip()
        if said(text) and not text.startswith("<") and not _SMALL_TALK.fullmatch(text):
            return text[:120]
    return ""


def ending(journal, chat: dict) -> str:
    """How a chat ended, in a line: the start of the agent's last reply."""
    row = journal.db.execute(
        "SELECT content FROM events WHERE project=? AND source=? AND session_id=? AND kind='assistant_message' "
        "ORDER BY timestamp DESC, ordinal DESC LIMIT 1", (chat["home"], chat["source"], chat["id"])).fetchone()
    return _first_sentences(row[0]) if row else ""


# Attachments appear in the text as markers such as "[Image: source: /tmp/…/1.webp]" or "[Image #1]".
ATTACHMENT = re.compile(r"\[(?:Image|File|Pasted text)(?::[^\]\n]*| #\d+[^\]\n]*)\]")


def said(text: str) -> bool:
    return bool(text and ATTACHMENT.sub("", text).strip()) and not NOT_SAID.match(text)


def last_exchanges(journal, chat: dict, count: int = 1) -> list[dict]:
    """The chat's last real exchanges as {user, agent}: what the person asked and the agent's final reply. Turns with
    nothing the person said (interruptions, tool notices, the agent's own recap of a long chat) are skipped; an
    agent reply with no question of its own joins the question before it."""
    from handoff_sessions import visible_text
    groups = journal.exchanges(Path(chat["home"]), chat["source"], chat["id"], kinds=("user_message", "assistant_message"))
    result, reply, stamp = [], "", ""
    for group in reversed(groups):
        users = [ATTACHMENT.sub("", t).strip() for t in (visible_text(e["content"]) for e in group if e["kind"] == "user_message") if said(t)]
        replies = [e["content"] for e in group if e["kind"] == "assistant_message" and (e["content"] or "").strip()]
        stamp = stamp or max((e.get("timestamp") or "" for e in group), default="")
        reply = reply or (replies[-1] if replies else "")
        if not users:
            continue  # Keep the newest reply for the question that came before it.
        result.append({"source": chat["source"], "user": users[0][:1500], "agent": reply[-2000:], "timestamp": stamp})
        reply, stamp = "", ""
        if len(result) == count:
            break
    if not result and reply:
        result.append({"source": chat["source"], "user": "", "agent": reply[-2000:], "timestamp": stamp})
    return result[::-1]
