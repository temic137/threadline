"""Readers for agents that keep many chats in one store: OpenCode, Kilo's CLI and VS Code Copilot Chat.

Each reader yields (captured session, archive rows, gaps, metadata) for the chats of one project,
the same shape capture uses for Cursor and Zed. Unfamiliar or damaged records are skipped one at a
time, so one bad record never loses the rest of a chat. Copilot's command-line tool writes one JSONL
file per chat and is read by read_copilot_cli, like Claude Code and Codex.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote, urlparse

from handoff_sessions import make_event, read_jsonl, session, text_of, text_or

SKIPPABLE = (AttributeError, TypeError, KeyError, ValueError)


def _data_home(home: Path) -> Path:
    data = os.environ.get("XDG_DATA_HOME")
    return Path(data) if data and Path(data).is_absolute() else home / ".local" / "share"


def _loads(value):
    """A JSON value from a database column, or None when it is not valid JSON."""
    try:
        return json.loads(value) if isinstance(value, (str, bytes)) else None
    except ValueError:
        return None


def _stamp_text(value) -> str:
    """Times stored as milliseconds since 1970 become ISO text, so chats sort with other agents'."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        import datetime
        try:
            return datetime.datetime.fromtimestamp(value / 1000, datetime.timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    return value if isinstance(value, str) else ""


# OpenCode and Kilo -----------------------------------------------------------------------------

def opencode_roots(home: Path) -> list[Path]:
    """OpenCode's data folders. OPENCODE_DATA_DIR may list several, separated by commas."""
    configured = os.environ.get("OPENCODE_DATA_DIR")
    if configured:
        return [Path(part.strip()) for part in configured.split(",") if part.strip()]
    return [_data_home(home) / "opencode"]


def kilo_db(home: Path) -> Path:
    return _data_home(home) / "kilo" / "kilo.db"


def opencode_candidates(home: Path) -> list[tuple[str, Path, None]]:
    found = []
    for root in opencode_roots(home):
        try:
            found += [("opencode", p, None) for p in sorted(root.iterdir())
                      if p.is_file() and p.name.startswith("opencode") and p.suffix == ".db"]
        except OSError:
            pass
        sessions = root / "storage" / "session"
        if sessions.is_dir():  # Files from OpenCode versions before its database.
            found += [("opencode", p, None) for p in sorted(sessions.rglob("*.json")) if p.is_file()]
    if kilo_db(home).is_file():
        found.append(("kilo", kilo_db(home), None))
    return found


def _part_events(kind, session_id, ordinal, role, part, timestamp, add):
    """Events for one OpenCode message part. Returns how many ordinals it used."""
    if not isinstance(part, dict):
        return 0
    part_type = part.get("type")
    if part_type == "text" and isinstance(part.get("text"), str) and part["text"]:
        if part.get("synthetic") is True or part.get("ignored") is True:
            return 0
        add(ordinal, f"{role}_message", role, part["text"], timestamp)
        return 1
    if part_type == "tool":
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        name = text_or(part.get("tool"), "")
        call_id = text_or(part.get("callID"), text_or(part.get("id"), ""))
        arguments = state.get("input")
        add(ordinal, "tool_call", "assistant",
            arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False) if arguments is not None else "",
            timestamp, {"name": name, "tool_id": call_id})
        failed = state.get("status") == "error"
        output = state.get("output") if state.get("output") is not None else state.get("error")
        if output is not None or failed:
            add(ordinal + 1, "tool_result", "tool", text_of(output) if output is not None else "[tool error]", timestamp,
                {"tool_id": call_id, "name": name, "is_error": failed})
        return 2
    return 0  # Reasoning, step markers, patches and files are not part of the conversation text.


def _v2_events(session_id, rows, add):
    """Events from OpenCode's newer session_message table."""
    for ordinal, (kind, created, data) in enumerate(rows):
        try:
            value = _loads(data)
            if not isinstance(value, dict):
                continue
            timestamp, base = _stamp_text(created), ordinal * 1000
            if kind == "user" and isinstance(value.get("text"), str):
                add(base, "user_message", "user", value["text"], timestamp)
            elif kind == "assistant":
                content = value.get("content") if isinstance(value.get("content"), list) else []
                texts = [item["text"] for item in content if isinstance(item, dict) and item.get("type") == "text"
                         and isinstance(item.get("text"), str)]
                if texts:
                    add(base, "assistant_message", "assistant", "\n".join(texts), timestamp)
                position = 1
                for item in content:
                    if not isinstance(item, dict) or item.get("type") != "tool":
                        continue
                    state = item.get("state") if isinstance(item.get("state"), dict) else {}
                    name, call_id = text_or(item.get("name"), ""), text_or(item.get("id"), "")
                    arguments = state.get("input")
                    add(base + position, "tool_call", "assistant",
                        json.dumps(arguments, ensure_ascii=False) if arguments is not None else "", timestamp,
                        {"name": name, "tool_id": call_id})
                    output = state.get("content")
                    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
                    output = text_of(output) if output else text_or(metadata.get("output"), "")
                    if output:
                        add(base + position + 1, "tool_result", "tool", output, timestamp, {"tool_id": call_id, "name": name})
                    position += 2
        except SKIPPABLE:
            continue


def opencode_conversations(kind, path, project, known, matches):
    """OpenCode or Kilo chats whose folder is this project."""
    if path.suffix == ".json":
        yield from _opencode_file_session(path, project, matches)
        return
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        session_table = "session_v2" if "session_v2" in tables else "session"
        if session_table not in tables:
            return
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({session_table})")}
        if not {"id", "directory"} <= columns:
            return
        title = "title" if "title" in columns else "''"
        parent = "parent_id" if "parent_id" in columns else "NULL"
        for session_id, parent_id, directory, name, updated in db.execute(
                f"SELECT id, {parent}, directory, {title}, {'time_updated' if 'time_updated' in columns else 'NULL'} "
                f"FROM {session_table}").fetchall():
            if not isinstance(session_id, str) or not session_id or not isinstance(directory, str) or not matches(directory):
                continue
            events = []

            def add(ordinal, event_kind, actor, content, timestamp, metadata=None, sid=session_id, sink=events):
                sink.append(make_event(kind, sid, ordinal, event_kind, actor, content, timestamp, metadata))

            rows = [("session", {"id": session_id, "parent_id": parent_id, "directory": directory, "title": name})]
            if "session_message" in tables:
                v2 = db.execute("SELECT type, time_created, data FROM session_message WHERE session_id=? ORDER BY seq",
                                (session_id,)).fetchall()
                _v2_events(session_id, v2, add)
                rows += [(f"v2:{index}", _loads(data) or data) for index, (_, _, data) in enumerate(v2)]
            if {"message", "part"} <= tables and not events:
                messages = db.execute("SELECT id, time_created, data FROM message WHERE session_id=? ORDER BY time_created, id",
                                      (session_id,)).fetchall()
                for index, (message_id, created, data) in enumerate(messages):
                    try:
                        message = _loads(data)
                        role = message.get("role") if isinstance(message, dict) else None
                        role = role if role in ("user", "assistant") else "user"
                        parts = db.execute("SELECT data FROM part WHERE message_id=? ORDER BY id", (message_id,)).fetchall()
                        ordinal = index * 1000
                        for (part_data,) in parts:
                            part = _loads(part_data)
                            ordinal += _part_events(kind, session_id, ordinal, role, part, _stamp_text(created), add)
                            rows.append((f"part:{message_id}:{ordinal}", part if part is not None else part_data))
                        rows.append((f"message:{message_id}", message if message is not None else data))
                    except SKIPPABLE:
                        continue
            captured = session(kind, session_id, str(project), path, events)
            captured["updated"] = _stamp_text(updated) or captured["updated"]
            metadata = {"title": text_or(name, ""), "directory": directory}
            if isinstance(parent_id, str) and parent_id:
                metadata["parent_session_id"] = parent_id
            yield captured, rows, [f"{kind.title()} chat read from its database; attached files are not copied."], metadata


def _opencode_file_session(path, project, matches):
    """A chat from OpenCode's older file layout: session, message and part JSON files."""
    try:
        meta = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return
    if not isinstance(meta, dict):
        return
    session_id, directory = text_or(meta.get("id"), path.stem), meta.get("directory")
    if not isinstance(directory, str) or not matches(directory):
        return
    storage = path.parents[2] if len(path.parents) > 2 else path.parent
    messages = []
    for message_path in sorted((storage / "message" / session_id).glob("*.json")):
        try:
            message = json.loads(message_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if isinstance(message, dict) and isinstance(message.get("id"), str):
            created = message.get("time", {}).get("created") if isinstance(message.get("time"), dict) else None
            messages.append((created if isinstance(created, (int, float)) else 0, message["id"], message))
    messages.sort(key=lambda item: (item[0], item[1]))
    events, rows = [], [("session", meta)]

    def add(ordinal, event_kind, actor, content, timestamp, metadata=None):
        events.append(make_event("opencode", session_id, ordinal, event_kind, actor, content, timestamp, metadata))

    for index, (created, message_id, message) in enumerate(messages):
        role = message.get("role") if message.get("role") in ("user", "assistant") else "user"
        ordinal = index * 1000
        rows.append((f"message:{message_id}", message))
        for part_path in sorted((storage / "part" / message_id).glob("*.json")):
            try:
                part = json.loads(part_path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                continue
            try:
                ordinal += _part_events("opencode", session_id, ordinal, role, part, _stamp_text(created), add)
            except SKIPPABLE:
                continue
            rows.append((f"part:{message_id}:{part_path.stem}", part))
    captured = session("opencode", session_id, str(project), path, events)
    metadata = {"title": text_or(meta.get("title"), ""), "directory": directory}
    if isinstance(meta.get("parentID"), str) and meta["parentID"]:
        metadata["parent_session_id"] = meta["parentID"]
    yield captured, rows, ["OpenCode chat read from its older file layout; attached files are not copied."], metadata


# GitHub Copilot ---------------------------------------------------------------------------------

def copilot_root(home: Path) -> Path:
    configured = os.environ.get("COPILOT_HOME")
    return Path(configured) if configured else home / ".copilot"


def copilot_workspace(path: Path) -> str:
    """The folder a Copilot CLI chat ran in, from the workspace.yaml beside its events."""
    try:
        lines = (path.parent / "workspace.yaml").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    values = {}
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#") or line[:1].isspace() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip().strip("'\"")
        if value:
            values[key.strip()] = value
    return values.get("cwd") or values.get("gitRoot") or values.get("git_root") or ""


def read_copilot_cli(path: Path) -> dict:
    session_id, project, events = path.parent.name, copilot_workspace(path), []
    for ordinal, record in read_jsonl(path):
        try:
            data = record.get("data") if isinstance(record.get("data"), dict) else (
                record.get("payload") if isinstance(record.get("payload"), dict) else {})
            kind, timestamp = record.get("type"), _stamp_text(record.get("timestamp"))
            if kind in ("session.start", "session.resume", "session.context_changed"):
                session_id = text_or(data.get("sessionId"), text_or(data.get("session_id"), session_id))
                context = data.get("context") if isinstance(data.get("context"), dict) else data
                project = text_or(context.get("cwd"), text_or(context.get("gitRoot"), project))
            elif kind == "user.message":
                text = text_of(data.get("content") or data.get("message") or data.get("prompt"))
                # A message with a source is Copilot's own: injected memory or a sub-agent's task.
                if text.strip() and not text_or(data.get("source"), ""):
                    events.append(make_event("copilot", session_id, ordinal, "user_message", "user", text, timestamp))
            elif kind == "assistant.message":
                text = text_of(data.get("content"))
                if text.strip():
                    events.append(make_event("copilot", session_id, ordinal, "assistant_message", "assistant", text, timestamp))
            elif kind in ("tool.execution_start", "tool.user_requested"):
                arguments = data.get("arguments")
                events.append(make_event("copilot", session_id, ordinal, "tool_call", "assistant",
                                         arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False),
                                         timestamp, {"name": text_or(data.get("toolName"), text_or(data.get("name"), "")),
                                                     "tool_id": text_or(data.get("toolCallId"), "")}))
            elif kind == "tool.execution_complete":
                result = data.get("result") if isinstance(data.get("result"), dict) else {}
                output = result.get("detailedContent") or result.get("content") or result.get("contents")
                error = data.get("error")
                if output is None and error is not None:
                    output = error.get("message") if isinstance(error, dict) and error.get("message") else error
                events.append(make_event("copilot", session_id, ordinal, "tool_result", "tool", text_of(output), timestamp,
                                         {"tool_id": text_or(data.get("toolCallId"), ""), "is_error": error is not None}))
        except SKIPPABLE:
            continue
    return session("copilot", session_id, project, path, events)


def vscode_user_dirs(home: Path) -> list[Path]:
    """VS Code's User folders, for the stable and Insiders builds."""
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    elif os.name == "nt":
        base = home / "AppData" / "Roaming"
    else:
        configured = os.environ.get("XDG_CONFIG_HOME")
        base = Path(configured) if configured and Path(configured).is_absolute() else home / ".config"
    return [base / name / "User" for name in ("Code", "Code - Insiders")]


def vscode_folder(workspace: Path) -> Path | None:
    """The folder a VS Code workspace storage directory belongs to, from its workspace.json."""
    try:
        data = json.loads((workspace / "workspace.json").read_text(encoding="utf-8"))
        uri = data.get("folder") if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None
    if not isinstance(uri, str) or not uri.startswith("file:"):
        return None
    path = unquote(urlparse(uri).path)
    if os.name == "nt" and path.startswith("/"):
        path = path[1:]
    return Path(path)


def vscode_candidates(home: Path) -> list[tuple[str, Path, Path | None]]:
    found = []
    for user in vscode_user_dirs(home):
        storage = user / "workspaceStorage"
        try:
            workspaces = [w for w in storage.iterdir() if (w / "chatSessions").is_dir()]
        except OSError:
            continue
        for workspace in sorted(workspaces):
            folder = vscode_folder(workspace)
            try:
                chats = sorted((workspace / "chatSessions").iterdir())
            except OSError:
                continue
            found += [("vscode", chat, folder) for chat in chats if chat.suffix in (".json", ".jsonl") and chat.is_file()]
    return found


def _vscode_response_text(response) -> tuple[str, list[dict]]:
    """The answer's text and its tool calls, from a VS Code chat response list."""
    texts, tools = [], []
    for item in response if isinstance(response, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("kind") in (None, "markdownContent") and isinstance(item.get("value"), str):
            texts.append(item["value"])
        elif item.get("kind") == "markdownContent" and isinstance(item.get("content"), dict):
            texts.append(text_or(item["content"].get("value"), ""))
        elif item.get("kind") in ("toolInvocationSerialized", "toolInvocation"):
            message = item.get("invocationMessage") or item.get("pastTenseMessage")
            tools.append({"name": text_or(item.get("toolId"), ""), "tool_id": text_or(item.get("toolCallId"), ""),
                          "text": text_of(message.get("value") if isinstance(message, dict) else message)})
    return "".join(texts), tools


def vscode_conversations(kind, path, project, known, matches):
    """One Copilot Chat conversation saved by VS Code for this project's folder."""
    if known is None or not matches(str(known)):
        return
    if path.suffix == ".jsonl":
        captured = session("vscode", path.stem, str(project), path, [])
        yield captured, [], ["This chat uses VS Code's newer .jsonl format, which Threadline does not read yet; "
                             "it is listed but its messages are not captured."], {}
        return
    try:
        chat = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        chat = None
    if not isinstance(chat, dict):
        yield session("vscode", path.stem, str(project), path, []), [], ["Chat file could not be read; it may be damaged."], {}
        return
    session_id = text_or(chat.get("sessionId"), path.stem)
    events, rows = [], [("chat", {key: chat.get(key) for key in ("version", "sessionId", "creationDate", "customTitle")})]
    requests = chat.get("requests") if isinstance(chat.get("requests"), list) else []
    for index, request in enumerate(requests):
        try:
            if not isinstance(request, dict):
                continue
            rows.append((f"request:{index}", request))
            timestamp, base = _stamp_text(request.get("timestamp")), index * 1000
            message = request.get("message")
            text = message.get("text") if isinstance(message, dict) else message
            if isinstance(text, str) and text.strip():
                events.append(make_event("vscode", session_id, base, "user_message", "user", text, timestamp))
            answer, tools = _vscode_response_text(request.get("response"))
            if answer.strip():
                events.append(make_event("vscode", session_id, base + 1, "assistant_message", "assistant", answer, timestamp))
            for position, tool in enumerate(tools, start=2):
                events.append(make_event("vscode", session_id, base + position, "tool_call", "assistant", tool["text"], timestamp,
                                         {"name": tool["name"], "tool_id": tool["tool_id"]}))
        except SKIPPABLE:
            continue
    captured = session("vscode", session_id, str(project), path, events)
    captured["updated"] = _stamp_text(chat.get("lastMessageDate")) or captured["updated"]
    yield captured, rows, ["VS Code Copilot Chat read from its saved chat file."], {"title": text_or(chat.get("customTitle"), "")}


# Gemini CLI -------------------------------------------------------------------------------------

def gemini_root(home: Path) -> Path:
    return home / ".gemini"


def _gemini_owner(folder: Path) -> str:
    """The project a Gemini temp folder belongs to, from the .project_root file Gemini writes there."""
    try:
        return (folder / ".project_root").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def gemini_candidates(home: Path) -> list[tuple[str, Path, Path | None]]:
    found = []
    try:
        folders = sorted(p for p in (gemini_root(home) / "tmp").iterdir() if p.is_dir())
    except OSError:
        return found
    for folder in folders:
        owner = _gemini_owner(folder)
        chats = folder / "chats"
        if not chats.is_dir():
            continue
        # Sub-agent chats sit in a folder named after their parent chat.
        for path in sorted(chats.rglob("*")):
            if path.is_file() and path.suffix in (".json", ".jsonl"):
                found.append(("gemini", path, Path(owner) if owner else None))
    return found


def _gemini_messages(path: Path) -> tuple[dict, list]:
    """A Gemini chat's metadata and messages, replaying its log the way Gemini CLI does."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".json":
        try:
            whole = json.loads(text)
        except ValueError:
            whole = None
        if isinstance(whole, dict):  # The older format: the whole chat in one JSON object.
            messages = whole.get("messages") if isinstance(whole.get("messages"), list) else []
            return {k: v for k, v in whole.items() if k != "messages"}, [m for m in messages if isinstance(m, dict)]
    metadata, messages = {}, {}

    def patch(update):
        existing = messages.get(update.get("id")) if isinstance(update, dict) else None
        if existing is None:
            return
        if "content" in update:
            existing["content"] = update["content"]
        if isinstance(update.get("toolCalls"), list) and isinstance(existing.get("toolCalls"), list):
            for change in update["toolCalls"]:
                for call in existing["toolCalls"]:
                    if isinstance(change, dict) and isinstance(call, dict) and call.get("id") == change.get("id") and "result" in change:
                        call["result"] = change["result"]

    for line in text.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(record, dict):
            continue
        try:
            if isinstance(record.get("$rewindTo"), str):
                ids = list(messages)
                cut = ids.index(record["$rewindTo"]) if record["$rewindTo"] in ids else 0
                for message_id in ids[cut:]:
                    del messages[message_id]
            elif isinstance(record.get("$patch"), dict):
                change = record["$patch"]
                if isinstance(change.get("id"), str):
                    patch(change)
                for update in change.get("updates") or []:
                    patch(update)
                for message_id in change.get("removeIds") or []:
                    if isinstance(message_id, str):
                        messages.pop(message_id, None)
                if isinstance(change.get("orderIds"), list):
                    ordered = [i for i in change["orderIds"] if isinstance(i, str) and i in messages]
                    rest = [i for i in messages if i not in ordered]
                    messages = {i: messages[i] for i in rest + ordered}
            elif isinstance(record.get("id"), str):
                messages[record["id"]] = record
            elif isinstance(record.get("$set"), dict):
                update = record["$set"]
                if isinstance(update.get("messages"), list):
                    messages = {m["id"]: m for m in update["messages"] if isinstance(m, dict) and isinstance(m.get("id"), str)}
                metadata.update({k: v for k, v in update.items() if k != "messages"})
            elif isinstance(record.get("sessionId"), str):
                metadata.update(record)
        except SKIPPABLE:
            continue
    return metadata, list(messages.values())


def _gemini_text(content) -> str:
    """Text from Gemini's content, which is a string, a part, or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    parts = []
    for part in content if isinstance(content, list) else []:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str) and not part.get("thought"):
            parts.append(part["text"])
        elif isinstance(part, dict) and isinstance(part.get("functionResponse"), dict):
            response = part["functionResponse"].get("response")
            parts.append(text_of(response.get("output") if isinstance(response, dict) and "output" in response else response))
    return "\n".join(p for p in parts if p)


def gemini_conversations(kind, path, project, known, matches):
    """One Gemini CLI chat, when its folder belongs to this project."""
    import hashlib
    try:
        metadata, messages = _gemini_messages(path)
    except OSError:
        return
    folder = path.parent if path.parent.name == "chats" else path.parent.parent
    folder = folder.parent if folder.name == "chats" else folder
    folders = [d for d in metadata.get("directories") or [] if isinstance(d, str)]
    # Older Gemini versions named the folder with the sha256 of the project path.
    hashed = folder.name in {hashlib.sha256(str(project).encode()).hexdigest(),
                             hashlib.sha256(str(project).lower().encode()).hexdigest()}
    if not ((known is not None and matches(str(known))) or hashed or any(matches(d) for d in folders)):
        return
    session_id = text_or(metadata.get("sessionId"), path.stem)
    events = []
    for index, message in enumerate(messages):
        try:
            timestamp, base, kind_of = text_or(message.get("timestamp"), ""), index * 1000, message.get("type")
            if kind_of == "user":
                text = _gemini_text(message.get("content"))
                if text.strip():
                    events.append(make_event("gemini", session_id, base, "user_message", "user", text, timestamp))
            elif kind_of == "gemini":
                text = _gemini_text(message.get("content"))
                if text.strip():
                    events.append(make_event("gemini", session_id, base, "assistant_message", "assistant", text, timestamp))
                for position, call in enumerate(message.get("toolCalls") or [], start=1):
                    if not isinstance(call, dict):
                        continue
                    name, call_id = text_or(call.get("name"), ""), text_or(call.get("id"), "")
                    events.append(make_event("gemini", session_id, base + position * 2 - 1, "tool_call", "assistant",
                                             json.dumps(call.get("args"), ensure_ascii=False), timestamp,
                                             {"name": name, "tool_id": call_id}))
                    if call.get("result") is not None:
                        events.append(make_event("gemini", session_id, base + position * 2, "tool_result", "tool",
                                                 _gemini_text(call["result"]), timestamp,
                                                 {"name": name, "tool_id": call_id, "is_error": call.get("status") == "error"}))
        except SKIPPABLE:
            continue
    captured = session("gemini", session_id, str(project), path, events)
    captured["updated"] = text_or(metadata.get("lastUpdated"), captured["updated"])
    extra = {"title": text_or(metadata.get("summary"), "")}
    if metadata.get("kind") == "subagent" and path.parent.name != "chats":
        extra["parent_session_id"] = path.parent.name
    rows = [("metadata", metadata)] + [(f"message:{i}", m) for i, m in enumerate(messages)]
    yield captured, rows, ["Gemini CLI chat read from its log; images and attached files are not copied."], extra


# Cline, Roo Code and Kilo Code -------------------------------------------------------------------

# VS Code extension folders of Cline and the agents built from it, which keep tasks the same way.
CLINE_EXTENSIONS = {"cline": "saoudrizwan.claude-dev", "roo": "rooveterinaryinc.roo-cline", "kilocode": "kilocode.kilo-code"}


def cline_roots(home: Path) -> list[tuple[str, Path]]:
    """(source, folder holding tasks/ and state/) for each Cline-family store on this computer."""
    roots = [("cline", Path(os.environ["CLINE_DATA_DIR"]) if os.environ.get("CLINE_DATA_DIR") else
              Path(os.environ["CLINE_DIR"]) / "data" if os.environ.get("CLINE_DIR") else home / ".cline" / "data")]
    for user in vscode_user_dirs(home):
        roots += [(source, user / "globalStorage" / folder) for source, folder in CLINE_EXTENSIONS.items()]
    return [(source, root) for source, root in roots if (root / "tasks").is_dir()]


def _cline_task_folders(root: Path) -> dict:
    """Task id -> folder it ran in, from the store's task list and each task's own record."""
    folders = {}
    try:
        history = json.loads((root / "state" / "taskHistory.json").read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        history = []
    for item in history if isinstance(history, list) else []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            folder = text_or(item.get("cwdOnTaskInitialization"), text_or(item.get("workspace"), ""))
            if folder:
                folders[item["id"]] = folder
    return folders


def cline_candidates(home: Path) -> list[tuple[str, Path, Path | None]]:
    found = []
    for source, root in cline_roots(home):
        folders = _cline_task_folders(root)
        try:
            tasks = sorted(p for p in (root / "tasks").iterdir() if (p / "api_conversation_history.json").is_file())
        except OSError:
            continue
        for task in tasks:
            folder = folders.get(task.name)
            if not folder:  # Roo Code keeps each task's record beside it.
                try:
                    item = json.loads((task / "history_item.json").read_text(encoding="utf-8", errors="replace"))
                    folder = text_or(item.get("workspace"), text_or(item.get("cwdOnTaskInitialization"), "")) if isinstance(item, dict) else ""
                except (OSError, ValueError):
                    folder = ""
            found.append((source, task / "api_conversation_history.json", Path(folder) if folder else None))
    return found


ENVIRONMENT = re.compile(r"<environment_details>.*?</environment_details>\s*", re.S)
WORKING_FOLDER = re.compile(r"# Current Working Directory \((.+?)\)")


def cline_conversations(kind, path, project, known, matches):
    """One Cline-family task, when it ran in this project."""
    try:
        history = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        history = None
    messages = [m for m in history if isinstance(m, dict)] if isinstance(history, list) else []
    if known is None:
        # Tasks the list does not name: Cline writes the folder into the first message's environment.
        found = WORKING_FOLDER.search(json.dumps(messages[:2], ensure_ascii=False).replace("\\n", "\n"))
        known = Path(found.group(1)) if found else None
    if known is None or not matches(str(known)):
        return
    task_id = path.parent.name
    if history is None:
        yield session(kind, task_id, str(project), path, []), [], ["Task file could not be read; it may be damaged."], {}
        return
    events = []
    for index, message in enumerate(messages):
        try:
            role = message.get("role") if message.get("role") in ("user", "assistant") else None
            if role is None:
                continue
            timestamp = _stamp_text(message.get("ts"))
            content = message.get("content")
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            texts = [ENVIRONMENT.sub("", block["text"]).strip() for block in blocks
                     if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)]
            if any(texts):
                events.append(make_event(kind, task_id, index * 1000, f"{role}_message", role, "\n".join(t for t in texts if t), timestamp))
            for position, block in enumerate(blocks, start=1):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    events.append(make_event(kind, task_id, index * 1000 + position, "tool_call", "assistant",
                                             json.dumps(block.get("input"), ensure_ascii=False), timestamp,
                                             {"name": text_or(block.get("name"), ""), "tool_id": text_or(block.get("id"), "")}))
                elif block.get("type") == "tool_result":
                    events.append(make_event(kind, task_id, index * 1000 + position, "tool_result", "tool",
                                             ENVIRONMENT.sub("", text_of(block.get("content"))), timestamp,
                                             {"tool_id": text_or(block.get("tool_use_id"), ""), "is_error": block.get("is_error") is True}))
        except SKIPPABLE:
            continue
    captured = session(kind, task_id, str(project), path, events)
    rows = [(f"message:{i}", m) for i, m in enumerate(messages)]
    yield captured, rows, [f"{CLINE_LABELS[kind]} task read from api_conversation_history.json; images are not copied."], {}


CLINE_LABELS = {"cline": "Cline", "roo": "Roo Code", "kilocode": "Kilo Code"}
