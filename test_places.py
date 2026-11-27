"""The page's project tree: chats opened at a folder of projects are placed in the sub-project they worked on."""
import json

from handoff_capture import synchronize
from handoff_sessions import Journal
from handoff_web.places import SEVERAL, Places, _first_sentences


def write_chat(folder, cwd, chat_id, user, agent, hour):
    lines = [json.dumps({"sessionId": chat_id, "cwd": str(cwd), "type": "user", "timestamp": f"2026-01-01T{hour:02d}:00:00Z",
                         "message": {"role": "user", "content": user}}),
             json.dumps({"sessionId": chat_id, "cwd": str(cwd), "type": "assistant", "timestamp": f"2026-01-01T{hour:02d}:00:05Z",
                         "message": {"role": "assistant", "content": [{"type": "text", "text": agent}]}})]
    (folder / f"{chat_id}.jsonl").write_text("\n".join(lines) + "\n")


def build(tmp_path, monkeypatch, container_is_a_project=False):
    home, root = tmp_path / "home", tmp_path / "work"
    for name in ("billing", "bot", "docs"):
        (root / name).mkdir(parents=True)
    if container_is_a_project:
        (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.setattr("handoff_sessions.cursor_root", lambda _: home / "cursor")
    monkeypatch.setattr("handoff_sessions.zed_threads_db", lambda _: home / "zed" / "threads.db")
    folder = home / ".claude" / "projects" / "work"
    folder.mkdir(parents=True)
    write_chat(folder, root, "c1", "Fix the refund matcher", "Edited billing/matcher.py and billing/tests/test_refund.py. Tests pass.", 1)
    write_chat(folder, root, "c2", "Make the bot friendlier", f"Changed {root}/bot/flow.ts. The greeting is shorter now.", 2)
    write_chat(folder, root, "c3", "Compare both", "Read billing/api.py, bot/flow.ts, docs/guide.md and bot/menu.ts.", 3)
    write_chat(folder, root, "c4", "What's the plan?", "Let's decide after lunch.", 4)
    sub = home / ".claude" / "projects" / "work-billing"
    sub.mkdir(parents=True)
    write_chat(sub, root / "billing", "c5", "Opened inside billing", "Looked at the ledger.", 5)
    db = home / ".handoff" / "handoff.db"
    db.parent.mkdir(parents=True)
    journal = Journal(db)
    synchronize(journal, root, home)
    synchronize(journal, root / "billing", home)
    return journal, root.resolve()


def test_chats_at_a_folder_of_projects_go_to_the_sub_project_they_worked_on(tmp_path, monkeypatch):
    journal, root = build(tmp_path, monkeypatch)
    places = Places(journal)
    [top] = places.tree()
    assert top["project"] == str(root) and top["chats"] == 5
    children = {c["place"]: c["chats"] for c in top["children"]}
    assert children == {"billing": 2, "bot": 1, SEVERAL: 1}, "c3 touched three folders; c4 named none and stays at the top"
    assert {c["id"] for c in places.select(str(root), "billing")} == {"c1", "c5"}, "a project opened inside joins its folder"
    assert places.tree() == [top], "unchanged history is answered from the cache"


def test_a_project_folder_is_not_split_into_its_own_folders(tmp_path, monkeypatch):
    journal, root = build(tmp_path, monkeypatch, container_is_a_project=True)
    [top] = Places(journal).tree()
    assert [c["place"] for c in top["children"]] == ["billing"], "only the project opened inside it, not src-style folders"


def test_how_a_chat_ended_is_one_clean_line():
    assert _first_sentences("**Done.** Tests pass.\n```py\nx = 1\n```\nNext I would refactor.") == "Done. Tests pass. Next I would refactor."
    long = "The matcher now handles partial refunds. " * 10
    assert _first_sentences(long).endswith(".") and len(_first_sentences(long)) <= 160


def test_a_continued_chat_is_titled_by_what_the_person_asked(tmp_path, monkeypatch):
    from handoff_web.places import title
    journal, root = build(tmp_path, monkeypatch)
    chat = {"home": str(root), "source": "claude", "id": "c1", "title": "This session is being continued from a previous conversation..."}
    assert title(journal, chat) == "Fix the refund matcher"
    assert title(journal, {**chat, "title": "Ship the release"}) == "Ship the release"


def test_the_summary_reads_real_exchanges_not_interruptions_or_tool_notices(tmp_path, monkeypatch):
    from handoff_web.places import last_exchanges
    home, root = tmp_path / "home", tmp_path / "work"
    root.mkdir()
    monkeypatch.setattr("handoff_sessions.cursor_root", lambda _: home / "cursor")
    monkeypatch.setattr("handoff_sessions.zed_threads_db", lambda _: home / "zed" / "threads.db")
    folder = home / ".claude" / "projects" / "work"
    folder.mkdir(parents=True)
    turns = [("user", "Add retries to the uploader"), ("assistant", "Added three retries with backoff."),
             ("user", "Now log each failed attempt"), ("assistant", "Each failed attempt is logged with its error."),
             ("user", "[Request interrupted by user]"),
             ("user", "This session is being continued from a previous conversation that ran out of context."),
             ("user", "<task-notification><task-id>abc</task-id></task-notification>"),
             ("assistant", "The background test run finished: all green.")]
    lines = [json.dumps({"sessionId": "c1", "cwd": str(root), "type": kind, "timestamp": f"2026-01-01T00:00:{n:02d}Z",
                         "message": {"role": kind, "content": text if kind == "user" else [{"type": "text", "text": text}]}})
             for n, (kind, text) in enumerate(turns)]
    (folder / "c1.jsonl").write_text("\n".join(lines) + "\n")
    db = home / ".handoff" / "handoff.db"
    db.parent.mkdir(parents=True)
    journal = Journal(db)
    synchronize(journal, root, home)
    exchanges = last_exchanges(journal, {"home": str(root.resolve()), "source": "claude", "id": "c1"}, 2)
    assert [x["user"] for x in exchanges] == ["Add retries to the uploader", "Now log each failed attempt"]
    assert exchanges[-1]["agent"] == "The background test run finished: all green.", "the newest reply joins the last real question"


def test_an_attachment_alone_is_not_a_request():
    from handoff_web.places import said
    assert not said("[Image: source: /private/tmp/x/images/1.webp]")
    assert not said("[Image #1]  ")
    assert said("[Image #1] the page is broken on my phone")
