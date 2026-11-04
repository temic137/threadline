"""Rebuildable, conservative decision evidence; no model or hosted service required."""
import hashlib
import json
import re


RULES = (
    ("correction", r"^(?:actually\b|correction\b|instead\b|change\b.+\bto\b|replace\b.+\bwith\b)"),
    ("rejected_approach", r"^(?:reject\b|do not use\b|don't use\b|never use\b|avoid\b|we rejected\b)"),
    ("accepted_decision", r"^(?:use\b|let['’]s use\b|we (?:decided|agreed|chose)\b|i (?:approve|accept|choose)\b|the decision is\b)"),
    ("goal", r"^(?:(?:the |our |my )?goal\s*(?:is\b|:)|i want\b|we need\b|this must\b|it must\b)"),
    ("request", r"^(?:please\b|implement\b|build\b|fix\b|add\b|test\b|make sure\b|finish\b|can you\b)"),
)


def clauses(text):
    """Do not treat code blocks, block quotes, client context or terminal pastes as decisions."""
    from handoff_sessions import visible_text
    text = re.split(r"(?m)(?:^|\s)PS [A-Za-z]:\\", visible_text(text))[0]
    in_code = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code or line.lstrip().startswith(">"):
            continue
        for clause in re.split(r"(?<=[.!?])\s+|;\s*", line):
            quote = clause.strip()
            if quote:
                yield quote


def classify(quote):
    plain = re.sub(r"^(?:[-*]\s+|\d+[.)]\s+)", "", quote).strip()
    if len(plain.split()) < 2:
        return None
    if "?" in plain and not re.match(r"(?i)^can you\b", plain):
        return None
    if re.search(r"(?i)\b(?:maybe|perhaps|might|could|consider|for example|if)\b", plain):
        return None
    for kind, pattern in RULES:
        if re.search(pattern, plain, re.I):
            return kind
    return None


def decision_id(source, session_id, kind, quote, event_id):
    return hashlib.sha256(json.dumps([source, session_id, kind, quote, event_id]).encode()).hexdigest()


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS decision_annotations (
        id TEXT PRIMARY KEY, project TEXT, source TEXT, session_id TEXT, payload TEXT)""")


def track(journal, project, source=None, session_id=None):
    initialize(journal.db)
    items = []
    for conversation in journal.conversations(project):
        if source and conversation["source"] != source or session_id and conversation["id"] != session_id:
            continue
        src, sid = conversation["source"], conversation["id"]
        if conversation["relationship"] == "child" and not session_id:
            continue  # An agent wrote these prompts, so they are not the person's decisions.
        agent_text = []
        for group in journal.exchanges(project, src, sid, kinds=("user_message", "assistant_message")):
            pending = []
            for event in group:
                if event["kind"] == "assistant_message":
                    agent_text.append(event["content"])
                if event["kind"] == "user_message":
                    for quote in clauses(event["content"]):
                        kind = classify(quote)
                        if not kind or any(quote in reply for reply in agent_text):
                            continue  # Repeating the agent's wording is not the person's decision.
                        item = {"id": decision_id(src, sid, kind, quote, event["id"]),
                                "kind": kind, "text": quote, "source": src, "session_id": sid,
                                "status": "completion_unknown" if kind == "request" else "explicit_user_wording",
                                "evidence": [{"event_id": event["id"], "quote": quote}],
                                "method": "wording_rule", "supersedes": [], "conflicts_with": []}
                        items.append(item)
                        if kind == "request":
                            pending.append(item)
                elif event["kind"] == "assistant_message" and re.search(r"(?im)^(?:done\b|implemented\b|fixed\b|completed\b|all .{0,40}tests pass)", event["content"]):
                    # This is an assistant claim, not proof of task completion or test success.
                    for item in pending:
                        item["status"] = "completion_claimed_unverified"
                        item["completion_evidence"] = {"event_id": event["id"], "quote": event["content"],
                                                       "scope": "Same exchange; fulfillment of this request is not established."}
                    pending = []
    for row in journal.db.execute("SELECT payload FROM decision_annotations WHERE project=?", (str(project.resolve()),)):
        item = json.loads(row[0])
        if source and item["source"] != source or session_id and item["session_id"] != session_id:
            continue
        evidence = journal.get([e["event_id"] for e in item["evidence"]])
        current = {e["id"]: e for e in evidence}
        if any(e["event_id"] not in current or e["quote"] not in current[e["event_id"]]["content"] for e in item["evidence"]):
            item["status"] = "stale_evidence"
        items.append(item)
    return {"items": items, "method": "Conservative wording rules plus evidence-validated agent annotations.",
            "limitations": "Not exhaustive semantic extraction. Acceptance is explicit wording, not independent verification. Requests may already be completed. Corrections do not automatically supersede earlier decisions; branch and chat scopes remain separate."}


def annotate(journal, project, source, session_id, kind, text, evidence, supersedes=None, conflicts_with=None):
    if kind not in {k for k, _ in RULES}:
        raise ValueError("Unknown decision kind")
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValueError("Decision text must contain 1–4000 characters")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 20:
        raise ValueError("Provide 1–20 exact supporting quotes")
    events = {e["id"]: e for e in journal.get([e["event_id"] for e in evidence])}
    for entry in evidence:
        event = events.get(entry["event_id"])
        if not event or event["project"] != str(project.resolve()) or event["source"] != source or event["session_id"] != session_id:
            raise ValueError("Evidence must belong to the selected project and conversation")
        if not isinstance(entry.get("quote"), str) or not entry["quote"].strip() or entry["quote"] not in event["content"]:
            raise ValueError("Each quote must match its source message exactly")
    if not any(e["kind"] == "user_message" for e in events.values()):
        raise ValueError("Decision interpretation requires user-message evidence")
    known = {item["id"]: item for item in track(journal, project)["items"]}
    for relation, ids in (("supersedes", supersedes or []), ("conflicts_with", conflicts_with or [])):
        if not isinstance(ids, list) or len(ids) > 20:
            raise ValueError("Relations must be lists of at most 20 decision IDs")
        for target in ids:
            if target not in known or known[target]["status"] == "stale_evidence":
                raise ValueError("Relation target must have current evidence in this project")
            if relation == "supersedes" and (known[target]["source"], known[target]["session_id"]) != (source, session_id):
                raise ValueError("Cross-conversation decisions cannot be silently superseded; record a conflict instead")
    item = {"id": decision_id(source, session_id, kind, text, json.dumps(evidence, sort_keys=True)),
            "kind": kind, "text": text, "source": source, "session_id": session_id,
            "status": "agent_interpretation", "method": "agent_annotation", "evidence": evidence,
            "supersedes": supersedes or [], "conflicts_with": conflicts_with or []}
    from handoff_sessions import sanitize_value
    item = sanitize_value(item)
    journal.db.execute("INSERT OR REPLACE INTO decision_annotations VALUES (?,?,?,?,?)",
                       (item["id"], str(project.resolve()), source, session_id, json.dumps(item)))
    journal.db.commit()
    return item


def page(journal, project, source=None, session_id=None, start=0, limit=20):
    if start < 0 or not 1 <= limit <= 200:
        raise ValueError("start must be nonnegative and limit between 1 and 200")
    ledger = track(journal, project, source, session_id)
    total = len(ledger["items"])
    ledger["items"] = ledger["items"][start:start+limit]
    ledger.update(total_items=total, next_start=start+limit if start+limit < total else None)
    return ledger
