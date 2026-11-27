"""Check an installed threadline command end to end, in a throwaway home folder.

    python ci/smoke.py path/to/threadline

Writes one Claude Code chat, then runs setup, sync, search, resume and doctor the way a new user
would, and fails loudly if any step errors or the chat cannot be found.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

command = sys.argv[1]
home = Path(tempfile.mkdtemp(prefix="threadline-home-"))
project = home / "project"
project.mkdir()
chat = home / ".claude" / "projects" / "project" / "smoke-session.jsonl"
chat.parent.mkdir(parents=True)
rows = [{"sessionId": "smoke-session", "cwd": str(project), "type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "How do we rotate the zebracorn signing key?"}},
        {"sessionId": "smoke-session", "cwd": str(project), "type": "assistant", "timestamp": "2026-01-01T00:00:05Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Run the rotate script, then restart."}]}}]
chat.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "PYTHONIOENCODING": "utf-8"}


def run(*args):
    done = subprocess.run([command, *args], capture_output=True, text=True, env=env, timeout=600)
    print(f"$ threadline {' '.join(args)}  -> exit {done.returncode}")
    if done.returncode != 0 or "Traceback" in done.stderr:
        print(done.stdout[-3000:], done.stderr[-3000:])
        sys.exit(f"step failed: threadline {' '.join(args)}")
    return done.stdout


run("--help")
run("setup")
run("sync", "--project", str(project))
found = run("search", "--project", str(project), "--query", "zebracorn signing key")
if "smoke-session" not in found or "rotate script" not in found:
    sys.exit("search did not return the chat and its answer:\n" + found[:3000])
run("resume", "--project", str(project))
run("doctor")
print("smoke test passed")
