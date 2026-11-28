"""Ask Threadline, memex, cass, ccrider and recall-cli the same questions and score where the right chat lands.

Each question names the chat its answer came from (picked at random by sample.py) and a phrase
from the answer. A tool scores "first" when its first chat is the right one, and "top 5" when the
right chat is among its first five different chats. A resumed chat that holds the same answer
message (same text and time) counts as the right chat, for every tool. "Answer shown" means the phrase appears in
the first 24,000 characters the tool returns, the limit agents get from Threadline.

    python run.py [threadline|memex|cass ...]                    the 27 questions used while tuning
    QUESTIONS=questions2.json python run.py [...]                 23 questions not used while tuning
"""
import json
import re
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
LIMIT = 24000
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PYTHON = str(Path.home() / ".handoff" / "venv" / "bin" / "python")


def distinct(ids):
    seen = []
    for value in ids:
        if value and value not in seen:
            seen.append(value)
    return seen


def windows_path(project):
    return "C:\\" + project.lstrip("/").replace("/", "\\")


def threadline(question, query, meaning="1"):
    out = subprocess.run([PYTHON, str(HERE / "mcp_search.py"), question["project"], query],
                         capture_output=True, text=True, cwd=REPO, timeout=300,
                         env={**os.environ, "HANDOFF_MEANING": meaning}).stdout
    data = json.loads(out)
    ids = [r.get("session_id") for r in data.get("results", [])] + [c.get("session_id") for c in data.get("matching_chats", [])]
    return distinct(ids), out


def squash(path):
    return "".join(ch for ch in path.lower() if ch.isalnum())


def memex(question, query, mode):
    # memex names projects three ways (C:\Users\..., /Users/..., Users-me-project), so its
    # --project filter misses chats. Search everything and keep rows from the project and its folders.
    command = ["memex", "search", "--non-interactive", "--no-update-check",
               "--mode", mode, "--limit", "60", "--format", "json", query]
    out = subprocess.run(command, capture_output=True, text=True, timeout=300).stdout
    key = squash(question["project"].split("/Users/", 1)[-1])
    rows = [r for r in json.loads(out or "[]") if key in squash(str(r.get("project", "")))]
    return distinct(r.get("session_id") for r in rows)[:20], json.dumps(rows)


def cass(question, query, mode):
    rows, out = [], ""
    for workspace in (windows_path(question["project"]), question["project"]):
        out = subprocess.run(["cass", "search", query, "--json", "--limit", "20", "--mode", mode, "--workspace", workspace],
                             capture_output=True, text=True, timeout=300).stdout
        try:
            data = json.loads(out)
        except ValueError:
            data = {}
        rows = data.get("hits", data.get("results", [])) if isinstance(data, dict) else data
        if rows:
            break
    # cass numbers chats itself; the chat's own id is the first id in its file name. Long Codex chats
    # are split into files named rollout-<time>-<chat id>_<part id>.jsonl.zst.
    ids = [next(iter(UUID.findall(Path(str(row.get("source_path") or "")).name)), "") for row in rows]
    return distinct(ids), out


def ccrider(question, query):
    # ccrider's search has no project filter, so search everything and keep sessions whose folder is the
    # project or one of its subfolders (the same rule as memex). Output is text: one block per session.
    out = subprocess.run(["ccrider", "search", query, "--limit", "200"], capture_output=True, text=True, timeout=300).stdout
    key = squash(question["project"].split("/Users/", 1)[-1])
    kept, ids = [], []
    for block in out.split("=== Session ")[1:]:
        found = re.search(r"^ID: (\S+)", block, re.M)
        place = re.search(r"^(\S.*?) \| \d+ msgs", block, re.M)
        if found and place and key in squash(place.group(1)):
            ids.append(next(iter(UUID.findall(found.group(1))), found.group(1)))
            kept.append(block)
    return distinct(ids)[:20], "".join(kept)


def recall(question, query, mode="hybrid"):
    # recall filters by a substring of the chat's folder; the last folder name matches both Mac and Windows paths.
    out = subprocess.run(["recall", query, "--mode", mode, "--project", question["project"].rstrip("/").split("/")[-1],
                          "--limit", "20", "--include-exec"], capture_output=True, text=True, timeout=300).stdout
    rows = []
    for line in out.splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            continue
        rows += data if isinstance(data, list) else data.get("results", [data]) if isinstance(data, dict) else []
    return distinct(r.get("session_id") for r in rows if isinstance(r, dict))[:20], out


TOOLS = {
    "threadline": lambda q, query: threadline(q, query),
    "threadline-words": lambda q, query: threadline(q, query, "0"),
    "memex": lambda q, query: memex(q, query, "hybrid"),
    "cass": lambda q, query: cass(q, query, "hybrid"),
    "ccrider": ccrider,
    "recall": recall,
    "recall-words": lambda q, query: recall(q, query, "lexical"),
}


def main():
    questions_file = os.environ.get("QUESTIONS", "questions.json")
    questions = json.load(open(HERE / questions_file))
    names = sys.argv[1:] or list(TOOLS)
    results_file = HERE / os.environ.get("RESULTS", "results.json" if questions_file == "questions.json" else "results2.json")
    results = json.load(open(results_file)) if results_file.exists() else {}
    for name in names:
        results[name] = {}
        for style, field in (("question", "q"), ("keywords", "k")):
            rows = []
            for question in questions:
                began = time.monotonic()
                try:
                    ids, out = TOOLS[name](question, question[field])
                except Exception as exc:  # A failure counts as a miss, and is recorded.
                    ids, out = [], f"error: {exc}"
                ranks = [ids.index(s) + 1 for s in question.get("sessions", [question["session"]]) if s in ids]
                rank = min(ranks) if ranks else None
                rows.append({"n": question["n"], "rank": rank, "answer_shown": question["needle"] in out[:LIMIT],
                             "chars": len(out),
                             "seconds": round(time.monotonic() - began, 2)})
            results[name][style] = rows
            first = sum(r["rank"] == 1 for r in rows)
            top5 = sum(bool(r["rank"]) and r["rank"] <= 5 for r in rows)
            shown = sum(r["answer_shown"] for r in rows)
            seconds = sorted(r["seconds"] for r in rows)[len(rows) // 2]
            chars = sorted(min(r["chars"], LIMIT) for r in rows)[len(rows) // 2]
            print(f"{name:<11} {style:<9} first {first:>2}/{len(rows)}  top5 {top5:>2}/{len(rows)}  "
                  f"answer shown {shown:>2}/{len(rows)}  median {seconds:.2f}s  median {chars} chars", flush=True)
    json.dump(results, open(results_file, "w"), indent=1)


if __name__ == "__main__":
    os.environ.setdefault("HANDOFF_MEANING", "1")
    main()
