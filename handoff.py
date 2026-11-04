"""Capture local coding-agent history and Git state for another harness."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from handoff_sessions import (
    Journal,
    discover,
    mcp_response,
    overview,
    redact,
    render_transcript,
    sanitize_session,
    serve_mcp,
)


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def backup(path: Path):
    target = path.with_name(path.name + ".handoff.bak")
    if path.is_file() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def update_json_server(path: Path, entry: dict | None, defaults: dict | None = None) -> tuple[bool, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else dict(defaults or {})
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"invalid config: {exc}"
    if not isinstance(data, dict):
        return False, "config is not an object"
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return False, "mcpServers is not an object"
    backup(path)
    if entry is None:
        servers.pop("handoff", None)
    else:
        servers["handoff"] = entry
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return True, str(path)


def run_setup_command(command: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    detail = (result.stdout or result.stderr).strip().splitlines()
    return result.returncode == 0, detail[-1] if detail else "done"


def configure(action: str, home: Path) -> int:
    python = sys.executable
    entry = {"command": python, "args": ["-m", "handoff", "--mcp"]}
    results = []

    codex = shutil.which("codex")
    if codex:
        backup(home / ".codex" / "config.toml")
        if action == "setup":
            subprocess.run([codex, "mcp", "remove", "handoff"], capture_output=True, text=True)
            ok, detail = run_setup_command([codex, "mcp", "add", "handoff", "--", python, "-m", "handoff", "--mcp"])
        else:
            ok, detail = run_setup_command([codex, "mcp", "remove", "handoff"])
        results.append(("Codex", ok, detail))
    else:
        results.append(("Codex", False, "not installed"))

    claude = shutil.which("claude")
    if claude:
        backup(home / ".claude.json")
        if action == "setup":
            subprocess.run([claude, "mcp", "remove", "--scope", "user", "handoff"], capture_output=True, text=True)
            ok, detail = run_setup_command([claude, "mcp", "add", "--transport", "stdio", "--scope", "user", "handoff", "--", python, "-m", "handoff", "--mcp"])
        else:
            ok, detail = run_setup_command([claude, "mcp", "remove", "--scope", "user", "handoff"])
        results.append(("Claude Code", ok, detail))
    else:
        results.append(("Claude Code", False, "not installed"))

    cursor_installed = bool(shutil.which("cursor-agent") or (Path(os.environ.get("APPDATA", "")) / "Cursor").exists())
    if cursor_installed:
        ok, detail = update_json_server(home / ".cursor" / "mcp.json", entry if action == "setup" else None)
        results.append(("Cursor", ok, detail))
    else:
        results.append(("Cursor", False, "not installed"))

    muse = shutil.which("muse")
    if muse:
        muse_entry = {"type": "stdio", **entry, "enabled": True, "required": False}
        ok, detail = update_json_server(home / ".config" / "muse" / "settings.json",
                                        muse_entry if action == "setup" else None,
                                        {"schema_version": 1})
        results.append(("Muse Code", ok, detail))
    else:
        results.append(("Muse Code", False, "not installed"))

    for name, ok, detail in results:
        state = "configured" if action == "setup" and ok else "removed" if ok else "skipped"
        print(f"{name:<12} {state:<10} {detail}")
    if action == "setup":
        print("\nOpen the same project in another configured agent and type: Continue")
    return 0 if any(ok for _, ok, _ in results) else 1


def doctor(home: Path, db_path: Path) -> int:
    checks = [("Python", True, sys.version.split()[0]), ("Git", bool(shutil.which("git")), shutil.which("git") or "not found")]
    try:
        journal = Journal(db_path)
        checks.append(("Journal", True, str(db_path)))
        checks.append(("FTS5", journal.fts, "available" if journal.fts else "LIKE fallback active"))
        response = mcp_response({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, journal)
        names = {tool["name"] for tool in response["result"]["tools"]}
        required = {"resume", "capture", "coverage", "conversations", "read_conversation", "search_context", "search", "evidence", "decisions", "record_decision"}
        checks.append(("MCP", required <= names, ", ".join(sorted(names))))
        journal.close()
    except (OSError, sqlite3.Error) as exc:
        checks.append(("Journal", False, str(exc)))
    from handoff_sessions import candidate_paths
    for source in ("codex", "claude", "cursor", "muse"):
        found = len(candidate_paths(home, source))
        if source == "codex":
            from handoff_capture import codex_databases
            found += sum(path is not None for path in codex_databases(home))
        checks.append((source.title(), found > 0, f"{found} local session source(s)" if found else "not found"))
    for label, executable, command in (
        ("Codex MCP", shutil.which("codex"), ["mcp", "get", "handoff"]),
        ("Claude MCP", shutil.which("claude"), ["mcp", "get", "handoff"]),
    ):
        if executable:
            ok, detail = run_setup_command([executable, *command])
            checks.append((label, ok, detail if ok else "not configured"))
    for label, path in (("Cursor MCP", home / ".cursor" / "mcp.json"),
                        ("Muse MCP", home / ".config" / "muse" / "settings.json")):
        try:
            configured = "handoff" in json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {})
        except (OSError, json.JSONDecodeError, TypeError):
            configured = False
        checks.append((label, configured, "configured" if configured else "not configured"))
    for name, ok, detail in checks:
        print(f"{name:<12} {'OK' if ok else '--':<4} {detail}")
    return 0 if all(ok for name, ok, _ in checks if name in ("Python", "Git", "Journal", "MCP")) else 1


def run_git(args: list[str], cwd: Path) -> str:
    command = ["git"]
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            command += ["-c", f"safe.directory={candidate.resolve().as_posix()}"]
            break
    try:
        out = subprocess.run(
            command + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        return f"(git unavailable: {exc})"
    if out.returncode != 0:
        err = (out.stderr or out.stdout or "unknown error").strip()
        return f"(git {' '.join(args)} failed: {err})"
    return out.stdout.strip() or "(empty)"


def local_notes(project: Path, max_chars: int = 3000) -> str:
    parts = []
    for name in ("AGENTS.md", "MEMORY.md"):
        p = project / name
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")[:max_chars]
            except OSError as exc:
                text = f"(unreadable: {exc})"
            parts.append(f"### {name}\n```\n{redact(text)}\n```")
    return "\n\n".join(parts) if parts else "(no AGENTS.md / MEMORY.md found)"


def existing_summary(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return {
        key.lower(): value.strip()
        for key, value in re.findall(
            r"^- (Goal|Done|Next|Blocker):[ \t]*(.*)$", text, re.MULTILINE
        )
    }


def git_snapshot(project: Path, output: Path | None = None) -> dict[str, str]:
    pathspec = [".", ":(exclude).handoff"]
    try:
        relative_output = (output or project / "HANDOFF.md").resolve().relative_to(
            project.resolve()
        )
        pathspec.append(f":(exclude){relative_output.as_posix()}")
    except ValueError:
        pass

    repo_check = run_git(["rev-parse", "--is-inside-work-tree"], project)
    if repo_check == "true":
        branch = run_git(["branch", "--show-current"], project)
        branch = "(detached HEAD)" if branch == "(empty)" else branch
        commit = run_git(["rev-parse", "--verify", "HEAD"], project)
        status = run_git(
            ["status", "--short", "--branch", "--untracked-files=all", "--"]
            + pathspec,
            project,
        )
        if commit.startswith("(git "):
            commit = log = "(no commits yet)"
        else:
            log = run_git(["log", "--oneline", "-8"], project)
        unstaged = run_git(["diff", "--"] + pathspec, project)
        staged = run_git(["diff", "--cached", "--"] + pathspec, project)
    else:
        branch = commit = "(not a Git worktree)"
        status = log = staged = unstaged = repo_check

    fingerprint = hashlib.sha256(
        "\n".join((commit, status, staged, unstaged)).encode()
    ).hexdigest()[:12]
    return {
        "branch": branch,
        "commit": commit,
        "status": status,
        "log": log,
        "staged": staged,
        "unstaged": unstaged,
        "fingerprint": fingerprint,
    }


def check_handoff(path: Path, project: Path | None = None) -> int:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"error: cannot read handoff: {exc}")
        return 2
    project_match = re.search(r"^Project: `([^`]+)`$", text, re.MULTILINE)
    fingerprint_match = re.search(r"^State fingerprint: `([^`]+)`$", text, re.MULTILINE)
    if not project_match or not fingerprint_match:
        print("error: invalid handoff: missing project or state fingerprint")
        return 2

    project = (project or Path(project_match.group(1))).resolve()
    if not project.is_dir():
        print(f"error: project dir not found: {project}")
        return 2
    expected = fingerprint_match.group(1)
    current = git_snapshot(project, path)["fingerprint"]
    if current == expected:
        print(f"CURRENT {current}")
        return 0
    print(f"STALE expected={expected} current={current}")
    return 1


def build_handoff(
    project: Path,
    max_diff_lines: int,
    home: Path,
    summary: dict[str, str] | None = None,
    output: Path | None = None,
    captured: dict | None = None,
) -> str:
    summary = summary or {}
    now = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()
    state = git_snapshot(project, output)
    branch = state["branch"]
    commit = state["commit"]
    status = state["status"]
    log = state["log"]
    staged = state["staged"]
    unstaged = state["unstaged"]
    diff_lines = ("# Staged\n" + staged + "\n# Unstaged\n" + unstaged).splitlines()
    truncated = len(diff_lines) > max_diff_lines
    diff = "\n".join(diff_lines[:max_diff_lines])
    fingerprint = state["fingerprint"]
    manual = ""
    if any(summary.values()):
        manual = f"""## Manual notes
- Goal: {summary.get("goal", "")}
- Done: {summary.get("done", "")}
- Next: {summary.get("next", "")}
- Blocker: {summary.get("blocker", "")}

"""
    captured_context = overview(captured, redact) if captured else "No matching Codex, Claude Code, Cursor, or Muse Code session was found."

    return f"""# Handoff — {project.resolve().name}

Generated: {now} (UTC)
Project: `{project.resolve()}`
Branch: {branch}
Commit: {commit}
State fingerprint: `{fingerprint}`

## Captured agent context
{captured_context}

{manual}## Git
### status
```
{redact(status)}
```
### recent log
```
{redact(log)}
```
### staged and unstaged diff (first {max_diff_lines} lines{", truncated" if truncated else ""})
```
{redact(diff)}
```

## Local notes
{local_notes(project)}

## Resume prompt (paste into next harness)
```
Call the handoff MCP `resume` tool for this project, verify the repository state, and continue the latest unfinished request. If MCP is unavailable, use this file as the fallback context.
```
"""


def choose_conversation(journal, project, source=None, session=None, choose=False):
    """Interactive selection is explicit; scripts never guess between chats."""
    if session and not source:
        raise ValueError("--session requires --source")
    choices = [c for c in journal.conversations(project)
               if (not source or c["source"] == source) and (not session or c["id"] == session)]
    saved = journal.selected_conversation(project)
    if not choose and not session and saved and saved in choices:
        selected = saved
    elif len(choices) == 1:
        selected = choices[0]
    else:
        if not choices:
            raise ValueError("No matching captured conversations. Run python -m handoff sync first.")
        if not sys.stdin.isatty():
            raise ValueError("Choose a conversation in an interactive terminal with python -m handoff extract --choose, or supply --source and --session.")
        choices.sort(key=lambda c: c["relationship"] == "child")
        print("Choose the conversation to continue (child chats are agent subtasks):", file=sys.stderr)
        for number, chat in enumerate(choices, 1):
            title = " ".join(str(chat["title"]).split())
            print(f"  {number}. [{chat['source']} / {chat['relationship']}] {title} ({chat['id'][:8]})", file=sys.stderr)
        while True:
            print("Conversation number (q to cancel): ", end="", file=sys.stderr, flush=True)
            answer = input().strip()
            if answer.lower() == "q":
                raise ValueError("Selection cancelled; saved choice unchanged.")
            if answer.isdecimal() and 1 <= int(answer) <= len(choices):
                selected = choices[int(answer)-1]
                break
            print("Enter one of the numbers above, or q.", file=sys.stderr)
    journal.selected_conversation(project, selected)
    print(f"Using [{selected['source']} / {selected['relationship']}] {' '.join(str(selected['title']).split())}. Use --choose to change.", file=sys.stderr)
    return selected["source"], selected["id"]


def configure_destination(destination, home, db_path):
    # Use the installed module's absolute script path so destination cwd is irrelevant.
    args = [str(Path(__file__).resolve()), "--mcp", "--db", str(db_path.resolve())]
    entry = {"command": sys.executable, "args": args}
    if destination == "cursor":
        return update_json_server(home / ".cursor" / "mcp.json", entry)
    if destination == "muse":
        return update_json_server(home / ".config" / "muse" / "settings.json",
                                  {"type": "stdio", **entry, "enabled": True, "required": False}, {"schema_version": 1})
    executable = shutil.which(destination)
    if not executable:
        return False, f"{destination} executable not found; use HANDOFF.md fallback."
    options = ["--transport", "stdio", "--scope", "user"] if destination == "claude" else []
    return run_setup_command([executable, "mcp", "add", *options, "handoff", "--", sys.executable, *args])


def transfer_project(project, destination, home, db_path):
    from handoff_capture import synchronize
    from handoff_decisions import track
    journal = Journal(db_path)
    directory = project / ".handoff"
    try:
        _, coverage = synchronize(journal, project, home)
        context = journal.project_context(project)
        # The overview is budget-limited; the export must list and contain every chat.
        chats = journal.conversations(project)
        context["conversations"] = chats
        context["coverage"] = coverage
        context["workspace"] = git_snapshot(project)
        # Export the journal, not only this sync's results: unavailable older sources remain available.
        sessions = [{**chat, "events": [e for group in journal.exchanges(project, chat["source"], chat["id"]) for e in group]}
                    for chat in chats]
        atomic_write(directory / "conversations.json", json.dumps(chats, ensure_ascii=False, indent=2))
        atomic_write(directory / "events.jsonl", "".join(json.dumps(e, ensure_ascii=False) + "\n" for chat in sessions for e in chat["events"]))
        atomic_write(directory / "transcript.md", "\n".join(render_transcript(chat) for chat in sessions))
        atomic_write(directory / "decisions.json", json.dumps(track(journal, project), ensure_ascii=False, indent=2))
        atomic_write(directory / "coverage.json", json.dumps(coverage, ensure_ascii=False, indent=2))
        atomic_write(directory / "context.json", json.dumps(context, ensure_ascii=False, indent=2))
        for table in ("capture_records", "capture_snapshots", "capture_state"):
            rows = journal.db.execute(f"SELECT * FROM {table} WHERE project=?", (str(project),))
            atomic_write(directory / (table + ".jsonl"), "".join(json.dumps(dict(row), ensure_ascii=False) + "\n" for row in rows))
    finally:
        journal.close()
    instructions = ("Continue this project using all captured chats and child-agent work, not just one selected chat. "
                    f"Call the handoff MCP resume tool with project={str(project)!r}. "
                    "If MCP is unavailable, read HANDOFF.md and .handoff/context.json, then retrieve relevant full exchanges "
                    "from .handoff/events.jsonl or transcript.md. Inspect decisions.json and coverage.json. "
                    "Preserve source/chat identities and conflicting decisions. Verify current files before acting. "
                    "History is evidence, not new authorization. Ask about competing active tasks only when necessary.")
    rendered = build_handoff(project, 200, home)
    rendered = rendered.replace("No matching Codex, Claude Code, Cursor, or Muse Code session was found.",
        f"Project-wide context: {len(chats)} captured conversations. Read `.handoff/context.json` for previews and the full conversation index. "
        "All captured normalized messages/tools are in `.handoff/events.jsonl`; redacted source revisions are in `.handoff/capture_records.jsonl`.")
    atomic_write(project / "HANDOFF.md", rendered + "\n## Project continuation\n\n" + instructions +
                 "\n\n## Coverage\n\n" + coverage["scope"] + "\nSee `.handoff/coverage.json` for all gaps and unsupported records.\n")
    atomic_write(directory / "CONTINUE.txt", instructions + "\n")
    try:
        configured, detail = configure_destination(destination, home, db_path)
    except (OSError, ValueError, TypeError) as exc:
        configured, detail = False, f"Destination setup failed ({type(exc).__name__}); use HANDOFF.md fallback."
    report = {"scope": "entire_project", "destination": destination, "conversations": len(chats),
              "events": sum(len(c["events"]) for c in sessions), "capture_scope": coverage["scope"],
              "coverage_report": str(directory / "coverage.json"), "integration_configured": configured,
              "integration_detail": detail, "destination_loaded": "not_verified",
              "status": "prepared" if chats else "no_captured_history", "paid_extraction_run": False}
    atomic_write(directory / "transfer.json", json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Prepared project-wide context: {report['conversations']} conversations, {report['events']} events.")
    print("Capture scope: " + coverage["scope"])
    print(f"Destination integration: {'configured' if configured else 'not configured'}; {detail}")
    print("Destination loading is not yet verified. Open this project in " + destination + " and ask: Continue using handoff.")
    print("If it does not call handoff resume, paste .handoff/CONTINUE.txt. See .handoff/transfer.json and coverage.json for status and gaps.")
    return 0 if chats and configured else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    home = Path(os.path.expanduser("~"))
    default_db = home / ".handoff" / "handoff.db"
    if not argv or argv[0] == "transfer":
        command = argparse.ArgumentParser(prog="handoff transfer", description="Transfer all accessible project context to another coding agent.")
        command.add_argument("--project", default=".")
        command.add_argument("--to", choices=("cursor", "claude", "codex", "muse"))
        command.add_argument("--db")
        options = command.parse_args(argv[1:] if argv else [])
        project = Path(options.project).resolve()
        if not project.is_dir():
            print("Project directory does not exist.")
            return 2
        destination = options.to
        if not destination:
            if not sys.stdin.isatty():
                print("Choose a destination: python -m handoff transfer --to cursor (or claude, codex, muse)")
                return 2
            try:
                while destination not in ("cursor", "claude", "codex", "muse"):
                    destination = input("Move this project to which agent? cursor / claude / codex / muse (q to cancel): ").strip().lower()
                    if destination == "q":
                        return 1
            except (EOFError, KeyboardInterrupt):
                return 1
        try:
            return transfer_project(project, destination, home, Path(options.db).resolve() if options.db else default_db)
        except (OSError, ValueError, sqlite3.Error) as exc:
            print(f"Transfer failed: {type(exc).__name__}. No destination loading has been verified.")
            return 1
    if argv and argv[0] == "configure":
        from handoff_typellm import configure as configure_typellm
        command = argparse.ArgumentParser(prog="handoff configure")
        command.add_argument("provider", choices=["typellm"])
        modes = command.add_mutually_exclusive_group()
        for mode in ("status", "check", "remove"):
            modes.add_argument("--" + mode, action="store_true")
        options = command.parse_args(argv[1:])
        try:
            return configure_typellm(next((m for m in ("status", "check", "remove") if getattr(options, m)), "setup"))
        except (ValueError, EOFError, KeyboardInterrupt) as exc:
            print(str(exc) if isinstance(exc, ValueError) else "Setup cancelled; saved key was not changed.")
            return 1
    if argv and argv[0] == "extract":
        from handoff_typellm import extract, extract_project
        command = argparse.ArgumentParser(prog="handoff extract", description="Send selected redacted conversation exchanges to TypeLLM for decision extraction.")
        command.add_argument("--project", default=".")
        command.add_argument("--db")
        command.add_argument("--source")
        command.add_argument("--session")
        command.add_argument("--choose", action="store_true", help="Choose a different conversation by number")
        command.add_argument("--refresh", action="store_true", help="Refresh capture before extraction")
        command.add_argument("--max-calls", type=int, default=3)
        options = command.parse_args(argv[1:])
        journal = Journal(Path(options.db).resolve() if options.db else default_db)
        try:
            project = Path(options.project).resolve()
            if not project.is_dir():
                raise ValueError("Project directory does not exist")
            if not 1 <= options.max_calls <= 50:
                raise ValueError("max-calls must be between 1 and 50")
            if options.refresh or not options.session:
                from handoff_capture import synchronize
                synchronize(journal, project, home)
            if options.session or options.choose:
                source, session = choose_conversation(journal, project, options.source, options.session, options.choose)
                result = extract(journal, project, source, session, options.max_calls)
            else:
                result = extract_project(journal, project, options.max_calls, options.source)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except (ValueError, EOFError, KeyboardInterrupt) as exc:
            print(f"Extraction stopped: {exc}")
            return 1
        finally:
            journal.close()
    if argv and argv[0] in ("conversations", "resume", "read", "search", "decisions"):
        from handoff_capture import synchronize
        command = argparse.ArgumentParser(prog=f"handoff {argv[0]}")
        command.add_argument("--project", default=".")
        command.add_argument("--db")
        command.add_argument("--source")
        command.add_argument("--session")
        command.add_argument("--choose", action="store_true")
        command.add_argument("--refresh", action="store_true")
        command.add_argument("--start", type=int, default=0)
        command.add_argument("--limit", type=int, default=5)
        command.add_argument("--token-budget", type=int, default=4000)
        command.add_argument("--query", default="")
        options = command.parse_args(argv[1:])
        project = Path(options.project).resolve()
        journal = Journal(Path(options.db).resolve() if options.db else default_db)
        try:
            if options.refresh:
                synchronize(journal, project, home)
            if argv[0] in ("resume", "read", "decisions") and options.choose:
                options.source, options.session = choose_conversation(journal, project, options.source, options.session, options.choose)
            if argv[0] == "conversations":
                data = {"conversations": journal.conversations(project)}
            elif argv[0] == "decisions":
                from handoff_decisions import page
                data = page(journal, project, options.source, options.session, options.start, options.limit)
            elif argv[0] == "resume":
                data = (journal.context(project, options.token_budget, options.source, options.session)
                        if options.source or options.session else journal.project_context(project, options.token_budget))
            elif argv[0] == "read":
                if not options.source or not options.session:
                    command.error("read requires --source and --session")
                data = journal.read_conversation(project, options.source, options.session, options.start, options.limit)
            else:
                data = journal.search_context(project, options.query, options.limit)
            print(json.dumps(data, ensure_ascii=False, indent=2))
        except ValueError as exc:
            print(f"error: {exc}")
            return 2
        finally:
            journal.close()
        return 0
    if argv and argv[0] in ("sync", "coverage"):
        from handoff_capture import synchronize, stored_report
        command = argparse.ArgumentParser(prog=f"handoff {argv[0]}")
        command.add_argument("--project", default=".")
        command.add_argument("--db")
        command.add_argument("--source", choices=("auto", "codex", "claude", "cursor", "muse"), default="auto")
        options = command.parse_args(argv[1:])
        project = Path(options.project).resolve()
        if not project.is_dir():
            print(f"error: project dir not found: {project}")
            return 2
        journal = Journal(Path(options.db).resolve() if options.db else default_db)
        try:
            if argv[0] == "sync":
                _, report = synchronize(journal, project, home, options.source)
            else:
                report = stored_report(journal, project) or {"status": "No capture report; run handoff sync first."}
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            journal.close()
        return 0
    if argv and argv[0] in ("setup", "uninstall"):
        command = argparse.ArgumentParser(
            prog=f"handoff {argv[0]}",
            description="Configure detected coding agents" if argv[0] == "setup" else "Remove handoff MCP entries without deleting history",
        )
        command.parse_args(argv[1:])
        return configure(argv[0], home)
    if argv and argv[0] == "doctor":
        command = argparse.ArgumentParser(prog="handoff doctor")
        command.add_argument("--db")
        options = command.parse_args(argv[1:])
        return doctor(home, Path(options.db).resolve() if options.db else default_db)
    if argv and argv[0] == "history":
        command = argparse.ArgumentParser(prog="handoff history")
        command.add_argument("--db")
        options = command.parse_args(argv[1:])
        journal = Journal(Path(options.db).resolve() if options.db else default_db)
        try:
            for item in journal.projects():
                print(f"{item['events']:>6}  {item['updated'] or '-':<26}  {item['project'] or '(unknown project)'}")
        finally:
            journal.close()
        return 0
    if argv and argv[0] == "delete":
        command = argparse.ArgumentParser(prog="handoff delete")
        command.add_argument("--project", required=True)
        command.add_argument("--session")
        command.add_argument("--db")
        command.add_argument("--yes", action="store_true", required=True, help="confirm permanent journal deletion")
        options = command.parse_args(argv[1:])
        journal = Journal(Path(options.db).resolve() if options.db else default_db)
        try:
            removed = journal.delete(Path(options.project), options.session)
        finally:
            journal.close()
        print(f"removed {removed} event(s) from the journal; source session files were not changed")
        return 0
    if argv and argv[0] == "export":
        argv = argv[1:] + ["--export-details"]

    ap = argparse.ArgumentParser(
        description="Capture or retrieve cross-agent project context",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""commands:
  setup       configure detected coding agents
  transfer    prepare all project history and configure one destination (--to cursor)
  configure   securely configure TypeLLM (configure typellm)
  extract     extract decisions across all project chats using TypeLLM (optional)
  sync        capture all matching local conversations, without writing HANDOFF.md
  coverage    show the last capture report
  doctor      check histories, journal, search, and MCP
  uninstall   remove handoff MCP entries
  export      write detailed Markdown and JSONL alongside HANDOFF.md
  history     list journaled projects
  delete      delete journal history for a project
""",
    )
    ap.add_argument("--project", help="project directory (default: cwd or handoff value)")
    ap.add_argument("--output", default=None, help="output file (default: <project>/HANDOFF.md)")
    ap.add_argument("--check", metavar="HANDOFF", help="report whether a handoff is current")
    ap.add_argument("--max-diff-lines", type=int, default=200, help="cap for embedded git diff")
    ap.add_argument("--source", choices=("auto", "codex", "claude", "cursor", "muse"), default="auto")
    ap.add_argument("--session", help="session id or path fragment override")
    ap.add_argument("--db", help="journal path (default: ~/.handoff/handoff.db)")
    ap.add_argument("--mcp", action="store_true", help="serve the journal over MCP stdio")
    ap.add_argument("--no-history", action="store_true", help="generate Git-only handoff")
    ap.add_argument("--export-details", action="store_true", help="also write .handoff transcript and JSONL")
    ap.add_argument("--goal", help="current objective")
    ap.add_argument("--done", help="completed work")
    ap.add_argument("--next", help="exact next action")
    ap.add_argument("--blocker", help="current blocker")
    ap.add_argument("--prompt", action="store_true", help="prompt for resume context")
    args = ap.parse_args(argv)

    db_path = Path(args.db).resolve() if args.db else default_db
    if args.mcp:
        serve_mcp(db_path)
        return 0

    if args.check:
        override = Path(args.project).resolve() if args.project else None
        return check_handoff(Path(args.check).resolve(), override)

    project = Path(args.project or ".").resolve()
    if not project.is_dir():
        print(f"error: project dir not found: {project}")
        return 2
    output = Path(args.output).resolve() if args.output else project / "HANDOFF.md"
    previous = existing_summary(output)
    summary = {
        key: value if value is not None else previous.get(key, "")
        for key, value in {
            "goal": args.goal,
            "done": args.done,
            "next": args.next,
            "blocker": args.blocker,
        }.items()
    }
    if args.prompt:
        for key, current in summary.items():
            entered = input(f"{key.title()} [{current}]: ").strip()
            if entered:
                summary[key] = entered

    captured, captures, coverage, decisions, project_context = None, [], None, None, None
    if not args.no_history:
        from handoff_capture import synchronize
        journal = Journal(db_path)
        try:
            captures, coverage = synchronize(journal, project, home, args.source, args.session, reread=True)
            from handoff_decisions import track
            captured = captures[0] if len(captures) == 1 else None
            project_context = journal.project_context(project)
            decisions = track(journal, project)
        finally:
            journal.close()
        print(f"captured {coverage['conversations_captured']} conversation(s), {coverage['new_records']} new source records")
        for item in coverage["conversations"]:
            print(f"  {item['source']} {item['session_id']}: {item['status']}, {item['records']} records")
        print("Coverage scope: " + coverage["scope"])
        print(f"Coverage notes: {len(coverage['gaps'])} scan gap(s); see HANDOFF.md for per-conversation limitations.")
    if captures:
        if args.export_details:
            artifact_dir = output.parent / ".handoff"
            atomic_write(artifact_dir / "transcript.md", "\n".join(render_transcript(item, redact) for item in captures))
            atomic_write(artifact_dir / "events.jsonl", "".join(
                redact(json.dumps(event, ensure_ascii=False, default=str)) + "\n"
                for item in captures for event in item["events"]
            ))
            atomic_write(artifact_dir / "coverage.json", json.dumps(coverage, ensure_ascii=False, indent=2))
            if decisions:
                atomic_write(artifact_dir / "decisions.json", json.dumps(decisions, ensure_ascii=False, indent=2))
        if captured:
            print(f"captured {len(captured['events'])} {captured['source']} events ({coverage['new_events']} new)")

    rendered = build_handoff(project, max(1, args.max_diff_lines), home, summary, output, captured)
    if decisions:
        rendered += "\n## Decision evidence\n\n" + decisions["limitations"] + "\n\n```json\n" + json.dumps(decisions, ensure_ascii=False, indent=2) + "\n```\n"
    if len(captures) > 1:
        rendered = rendered.replace("No matching Codex, Claude Code, Cursor, or Muse Code session was found.",
            "Project-wide history is available. Run `handoff resume` or call MCP resume without a session filter; no chat selection is required.")
    if project_context:
        rendered += "\n## Project context\n\n```json\n" + json.dumps(project_context, ensure_ascii=False, indent=2) + "\n```\n"

    atomic_write(
        output,
        rendered + ("\n## Capture coverage\n\nThe journal contains the conversations listed below.\n\n```json\n"
             + json.dumps(coverage, ensure_ascii=False, indent=2) + "\n```\n" if coverage else ""),
    )
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
