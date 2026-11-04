import io
import json
import os
import urllib.error
import uuid

import pytest

import handoff_typellm as provider
from handoff import main
from handoff_sessions import Journal
from handoff_decisions import track
from test_decisions import seed
from handoff import choose_conversation


def test_picker_remembers_changes_and_clears_selection(tmp_path, monkeypatch):
    j = Journal(tmp_path / "picker.db")
    try:
        seed(j, tmp_path, "A")
        seed(j, tmp_path, "B")
        monkeypatch.setattr(provider.sys.stdin, "isatty", lambda: True)
        answers = iter(["bad", "99", "2"])
        monkeypatch.setattr("builtins.input", lambda: next(answers))
        assert choose_conversation(j, tmp_path) == ("codex", "B")
        monkeypatch.setattr("builtins.input", lambda: pytest.fail("Remembered choice should not prompt"))
        assert choose_conversation(j, tmp_path) == ("codex", "B")
        assert j.context(tmp_path)["session_id"] == "B"
        monkeypatch.setattr("builtins.input", lambda: "1")
        assert choose_conversation(j, tmp_path, choose=True) == ("codex", "A")
        j.delete(tmp_path, "A")
        assert j.selected_conversation(tmp_path) is None
    finally:
        j.close()


def test_picker_scripts_do_not_guess_or_overwrite_on_cancel(tmp_path, monkeypatch):
    j = Journal(tmp_path / "picker.db")
    try:
        seed(j, tmp_path, "A")
        seed(j, tmp_path, "B")
        monkeypatch.setattr(provider.sys.stdin, "isatty", lambda: False)
        with pytest.raises(ValueError, match="interactive terminal"):
            choose_conversation(j, tmp_path)
        choose_conversation(j, tmp_path, "codex", "B")
        with pytest.raises(ValueError, match="No matching"):
            choose_conversation(j, tmp_path, "codex", "missing")
        monkeypatch.setattr(provider.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda: "q")
        with pytest.raises(ValueError, match="cancelled"):
            choose_conversation(j, tmp_path, choose=True)
        assert j.selected_conversation(tmp_path)["id"] == "B"
        assert j.selected_conversation(tmp_path / "another-project") is None
    finally:
        j.close()


@pytest.mark.parametrize("text", ["abc " * 20000, "\u0001" * 20000, "Unicode \U0001f600 " * 10000], ids=["plain", "escaped", "unicode"])
def test_split_windows_cover_every_character_and_bound_json(text):
    group = [{"id": "U", "actor": "user", "kind": "user_message", "content": text},
             {"id": "A", "actor": "assistant", "kind": "assistant_message", "content": "reply" * 20000}]
    windows = list(provider.extraction_windows([group]))
    assert len(windows) > 1
    for event in group:
        restored = [None] * len(event["content"])
        for _, _, messages, users, split in windows:
            assert split and users == ["U"]
            assert len(json.dumps(messages, ensure_ascii=False)) <= 60000
            for message in messages:
                if message["id"] == event["id"]:
                    restored[message["start"]:message["end"]] = message["text"]
        assert "".join(restored) == event["content"]


def test_child_labels_and_main_first(tmp_path, monkeypatch, capsys):
    j = Journal(tmp_path / "labels.db")
    try:
        seed(j, tmp_path, "A-child")
        seed(j, tmp_path, "B-main")
        j.conversations(tmp_path)
        metadata = {"source": {"subagent": {"spawn": {"parent_thread_id": "B-main"}}}}
        j.db.execute("INSERT INTO capture_state VALUES (?,?,?,?,?,?)",
                     (str(tmp_path), "codex", "A-child", "file", "digest", json.dumps({"metadata": metadata})))
        j.db.commit()
        child = next(c for c in j.conversations(tmp_path) if c["id"] == "A-child")
        assert child["relationship"] == "child" and child["parent_id"] == "B-main"
        monkeypatch.setattr(provider.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda: "1")
        assert choose_conversation(j, tmp_path) == ("codex", "B-main")
        assert "2. [codex / child]" in capsys.readouterr().err
    finally:
        j.close()


def test_extract_cli_without_ids_refreshes_entire_project(tmp_path, monkeypatch, capsys):
    import handoff_capture
    db = tmp_path / "cli.db"
    j = Journal(db)
    seed(j, tmp_path, "A")
    seed(j, tmp_path, "B")
    j.close()
    refreshes = []
    monkeypatch.setattr(handoff_capture, "synchronize", lambda *args: refreshes.append(True))
    monkeypatch.setattr(provider.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: pytest.fail("Project extraction must not prompt for chats"))
    monkeypatch.setattr(provider, "credential", lambda _: "dummy")
    monkeypatch.setattr(provider, "request", lambda *args: {"result": {f"d{i}_kind": "none" for i in range(3)}})
    command = ["extract", "--project", str(tmp_path), "--db", str(db), "--max-calls", "1"]
    assert main(command) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["calls"] == 1 and result["scope"] == "entire_project"
    assert {c["session_id"] for c in result["conversations"]} == {"A", "B"}
    monkeypatch.setattr("builtins.input", lambda: pytest.fail("Should remember"))
    assert main(command) == 0
    assert json.loads(capsys.readouterr().out)["cached"] == 1
    assert len(refreshes) == 2


def test_split_rejects_quote_from_unsent_part(tmp_path, monkeypatch):
    j = Journal(tmp_path / "pieces.db")
    try:
        event = seed(j, tmp_path, texts=[("user", "x" * 65000 + "Secret distant decision")])[0]
        monkeypatch.setattr(provider, "credential", lambda _: "dummy")
        def generate(*args):
            assert "Secret distant decision" not in args[2]["context"]
            return {"result": {"d0_kind": "goal", "d0_user_id": event["id"],
                               "d0_text": "Distant decision", "d0_quote": "Secret distant decision",
                               "d1_kind": "none", "d2_kind": "none"}}
        monkeypatch.setattr(provider, "request", generate)
        result = provider.extract(j, tmp_path, "codex", "A", 1)
        assert result["saved"] == 0 and result["gaps"]
        assert not j.db.execute("SELECT * FROM typellm_cache").fetchall()
    finally:
        j.close()


def test_setup_validates_before_saving_and_never_echoes_key(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(provider.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(provider.getpass, "getpass", lambda _: "test-secret")
    monkeypatch.setattr(provider, "validate", lambda key: calls.append(("validate", key)))
    monkeypatch.setattr(provider, "credential", lambda action, key=None: calls.append((action, key)))
    assert main(["configure", "typellm"]) == 0
    assert calls == [("validate", "test-secret"), ("write", "test-secret")]
    assert "test-secret" not in capsys.readouterr().out


def test_failed_setup_preserves_old_key(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(provider.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(provider.getpass, "getpass", lambda _: "bad-secret")
    monkeypatch.setattr(provider, "credential", lambda *args: calls.append(args))
    def reject(key):
        raise ValueError("API key rejected")
    monkeypatch.setattr(provider, "validate", reject)
    assert main(["configure", "typellm"]) == 1
    assert not calls
    assert "bad-secret" not in capsys.readouterr().out


def test_setup_requires_hidden_interactive_input(monkeypatch):
    monkeypatch.setattr(provider.sys.stdin, "isatty", lambda: False)
    assert main(["configure", "typellm"]) == 1
    monkeypatch.setattr(provider.sys.stdin, "isatty", lambda: True)
    def no_echo(prompt):
        import warnings
        warnings.warn("echo unavailable", provider.getpass.GetPassWarning)
    monkeypatch.setattr(provider.getpass, "getpass", no_echo)
    assert main(["configure", "typellm"]) == 1


def test_status_check_remove(monkeypatch, capsys):
    store = {"key": "dummy"}
    def vault(action, value=None):
        if action == "read":
            return store.get("key")
        if action == "delete":
            store.clear()
    monkeypatch.setattr(provider, "credential", vault)
    monkeypatch.setattr(provider, "validate", lambda _: None)
    assert main(["configure", "typellm", "--status"]) == 0
    assert main(["configure", "typellm", "--check"]) == 0
    assert main(["configure", "typellm", "--remove"]) == 0
    assert main(["configure", "typellm", "--status"]) == 1
    assert "dummy" not in capsys.readouterr().out


@pytest.mark.parametrize("status", [401, 402, 429, 500, 503])
def test_http_errors_do_not_echo_server_body_or_key(monkeypatch, status):
    class Client:
        def open(self, req, **kwargs):
            raise urllib.error.HTTPError(req.full_url, status, "echo-secret", {}, io.BytesIO(b"echo-secret"))
    monkeypatch.setattr(provider.urllib.request, "build_opener", lambda *_: Client())
    with pytest.raises(ValueError) as error:
        provider.request("echo-secret", "models")
    assert "echo-secret" not in str(error.value)


def test_http_contract_and_redirect_protection(monkeypatch):
    class Client:
        def open(self, req, **kwargs):
            assert req.full_url == "https://api.typellm.ai/v1/models"
            assert req.get_header("Authorization") == "Bearer dummy"
            return io.BytesIO(b'{"models":["typellm-latest"]}')
    monkeypatch.setattr(provider.urllib.request, "build_opener", lambda *_: Client())
    provider.validate("dummy")
    with pytest.raises(ValueError):
        provider.NoRedirect().redirect_request(None, None, 302, "", {}, "https://unrelated.example/")


@pytest.mark.parametrize("key", ["\x16", "\x16fake-key", "fake-key\x16", "fake\x00key", "fake\x7fkey"])
def test_keyboard_controls_rejected_before_network(monkeypatch, key):
    def unexpected(*args):
        pytest.fail("Malformed key must never reach the network")
    monkeypatch.setattr(provider.urllib.request, "build_opener", unexpected)
    with pytest.raises(ValueError, match="keyboard control character") as error:
        provider.request(key, "models")
    assert "fake-key" not in str(error.value)


def test_extraction_validates_evidence_and_caches_without_repeat_calls(tmp_path, monkeypatch):
    j = Journal(tmp_path / "j.db")
    try:
        events = seed(j, tmp_path, texts=[("assistant", "Shall we use SQLite?"), ("user", "Yes, use that.")])
        monkeypatch.setattr(provider, "credential", lambda _: "dummy")
        calls = []
        def generate(key, endpoint, payload):
            calls.append(payload)
            assert "Shall we use SQLite?" in payload["context"]
            assert all(q["type"] in ("string", "boolean", "integer", "number") for q in payload["questions"].values())
            return {"result": {"d0_kind": "accepted_decision", "d0_text": "Use SQLite", "d0_user_id": events[1]["id"],
                "d0_quote": "Yes, use that.", "d0_context_id": events[0]["id"], "d0_context_quote": "Shall we use SQLite?",
                "d1_kind": "none", "d2_kind": "none"}}
        monkeypatch.setattr(provider, "request", generate)
        assert provider.extract(j, tmp_path, "codex", "A")["saved"] == 1
        assert provider.extract(j, tmp_path, "codex", "A")["cached"] == 1
        assert len(calls) == 1
        assert any(i["text"] == "Use SQLite" and len(i["evidence"]) == 2 for i in track(j, tmp_path)["items"])
        j.delete(tmp_path)
        assert j.db.execute("SELECT count(*) FROM typellm_cache").fetchone()[0] == 0
    finally:
        j.close()


def test_bad_model_evidence_not_saved_or_cached(tmp_path, monkeypatch):
    j = Journal(tmp_path / "j.db")
    try:
        events = seed(j, tmp_path, texts=[("user", "Hello")])
        monkeypatch.setattr(provider, "credential", lambda _: "dummy")
        monkeypatch.setattr(provider, "request", lambda *args: {"result": {"d0_kind": "goal", "d0_text": "invented",
            "d0_user_id": events[0]["id"], "d0_quote": "Not present", "d1_kind": "none", "d2_kind": "none"}})
        result = provider.extract(j, tmp_path, "codex", "A")
        assert result["gaps"] and result["saved"] == 0
        assert not j.db.execute("SELECT * FROM typellm_cache").fetchall()
    finally:
        j.close()


def test_call_limits_redaction_and_oversized_windows(tmp_path, monkeypatch):
    j = Journal(tmp_path / "j.db")
    try:
        seed(j, tmp_path, texts=[("user", "api_key=private-value"), ("user", "second request"), ("user", "x" * 61000)])
        monkeypatch.setattr(provider, "credential", lambda _: "dummy")
        calls = []
        def generate(key, endpoint, payload):
            assert "private-value" not in payload["context"]
            calls.append(payload)
            return {"result": {f"d{i}_kind": "none" for i in range(3)}}
        monkeypatch.setattr(provider, "request", generate)
        result = provider.extract(j, tmp_path, "codex", "A", 1)
        assert result["calls"] == 1 and result["remaining"] > 1 and not result["gaps"]
        assert result["split_windows"] > 1
        result = provider.extract(j, tmp_path, "codex", "A", 1)
        assert result["cached"] == 1 and result["calls"] == 1
        assert len(calls) == 2
        result = provider.extract(j, tmp_path, "codex", "A", 50)
        assert result["remaining"] == 0 and not result["gaps"]
        assert all(len(call["context"]) <= 60000 for call in calls)
    finally:
        j.close()


def test_network_failure_and_invalid_json_are_safe(monkeypatch):
    class Client:
        def open(self, *args, **kwargs):
            raise urllib.error.URLError("private transport details")
    monkeypatch.setattr(provider.urllib.request, "build_opener", lambda *_: Client())
    with pytest.raises(ValueError, match="Could not reach"):
        provider.request("dummy", "models")
    monkeypatch.setattr(Client, "open", lambda *args, **kwargs: io.BytesIO(b"not-json"))
    with pytest.raises(ValueError, match="unreadable JSON"):
        provider.request("dummy", "models")


@pytest.mark.skipif(os.environ.get("HANDOFF_TEST_CREDENTIALS") != "1", reason="Opt-in native vault check uses a disposable dummy credential")
def test_native_windows_vault_roundtrip():
    target = "handoff/test/" + str(uuid.uuid4())
    try:
        assert provider.credential("read", target=target) is None
        provider.credential("write", "dummy-not-an-api-key", target)
        assert provider.credential("read", target=target) == "dummy-not-an-api-key"
        provider.credential("write", "replacement-dummy", target)
        assert provider.credential("read", target=target) == "replacement-dummy"
    finally:
        provider.credential("delete", target=target)
    assert provider.credential("read", target=target) is None
