"""Isolated behavioral checks for CLI backend wiring.

The real ``llm.py`` is loaded with tiny dependency fakes, so these tests need
no desktop packages and can never execute an installed CLI.
Run with: python cli_backend_wiring_tests.py
"""

import importlib.util
import re
import sys
import threading
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT / "spell_daemon"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_isolated_llm():
    package = types.ModuleType("spell_daemon")
    package.__path__ = [str(PACKAGE)]
    sys.modules["spell_daemon"] = package

    constants = types.ModuleType("spell_daemon.constants")
    constants._SLAVIC_LANGS = frozenset({"BHS"})
    constants._REPHRASE_PROMPTS = {
        "Diacritics Only": "Restore diacritics only.",
        "Polish My Writing": "Polish the following writing.",
        "What's This About?": "Explain the selected text.",
    }
    constants._CJK_RE = re.compile(r"[\u3400-\u9fff]")
    constants._word_count = lambda text: len(str(text).split())
    constants._is_answer_mode = lambda mode: mode == "What's This About?"
    sys.modules[constants.__name__] = constants

    config = types.ModuleType("spell_daemon.config")
    config.load_cfg = lambda: {}
    sys.modules[config.__name__] = config

    prompt_builder = types.ModuleType("spell_daemon.prompt_builder")
    prompt_builder._build_prompt = lambda fmt, system, user: (
        f"FORMATTED::{fmt}::{system}::{user}", []
    )
    prompt_builder._build_chat_prompt = lambda fmt, system, history: (
        f"CHAT::{fmt}::{system}::{history}", []
    )
    sys.modules[prompt_builder.__name__] = prompt_builder

    events = []
    diagnostics = types.ModuleType("spell_daemon.diagnostics")
    diagnostics.record = lambda *args, **kwargs: events.append((args, kwargs))
    diagnostics.record_throttled = lambda *args, **kwargs: events.append((args, kwargs))
    diagnostics.set_status = lambda **kwargs: None
    sys.modules[diagnostics.__name__] = diagnostics
    package.diagnostics = diagnostics

    _load("spell_daemon.cli_models", PACKAGE / "cli_models.py")
    llm = _load("spell_daemon.llm", PACKAGE / "llm.py")
    return llm, events


def _client(llm, reply="fixed", model="claude-haiku-4-5"):
    client = llm.CLIClient("claude-haiku", "low", enabled=True)
    calls = []

    def complete(messages, *, enabled=None):
        calls.append((messages, enabled))
        return reply, {"runner": "Claude Code", "model": model, "effort": "low"}

    client.complete = complete
    return client, calls


def test_connect_cli_is_detection_only_and_opt_in():
    llm, _events = _load_isolated_llm()
    llm.selectable_models = lambda enabled: [{
        "id": "claude-haiku", "label": "Claude Code - Haiku", "efforts": ["low"]
    }] if enabled else []
    llm._backend.client = object()
    try:
        llm.connect_cli("claude-haiku", False)
    except llm.CLIModelError as exc:
        assert "switched off" in str(exc)
    else:
        raise AssertionError("disabled CLI selection should fail")
    assert llm._backend.client is None

    label = llm.connect_cli("claude-haiku", True, "low")
    assert label == "AI CLI (Claude Code - Haiku)"
    assert isinstance(llm._backend.client, llm.CLIClient)
    assert llm._backend.client.model == "claude-haiku"


def test_shared_helpers_require_and_forward_semantic_messages():
    llm, _events = _load_isolated_llm()
    client, calls = _client(llm)
    llm._backend.client = client
    semantic = [
        {"role": "system", "content": "System"},
        {"role": "user", "content": "User"},
    ]
    cfg = {"cli_models_enabled": True}

    out = llm._complete_prompt(
        "FORMATTED TOKENS", max_tokens=10, temperature=0.0,
        cfg=cfg, messages=semantic,
    )
    assert out["choices"][0]["text"] == "fixed"
    assert calls == [(semantic, True)]

    calls.clear()
    llm._stream_cancel.clear()
    chunks = list(llm._stream_prompt(
        "OTHER FORMATTED TOKENS", max_tokens=10, temperature=0.0,
        cfg=cfg, messages=semantic,
    ))
    assert [chunk["choices"][0]["text"] for chunk in chunks] == ["fixed"]
    assert calls == [(semantic, True)]

    try:
        llm._complete_prompt("FORMATTED", max_tokens=1, temperature=0.0, cfg=cfg)
    except llm.CLIModelError as exc:
        assert "semantic messages" in str(exc)
    else:
        raise AssertionError("CLI raw-prompt fallback must be forbidden")


def test_automatic_cli_spell_check_has_a_separate_default_off_gate():
    llm, events = _load_isolated_llm()
    client, calls = _client(llm, reply="Fixed sentence")
    llm._backend.client = client

    original = "bad sentence"
    disabled_cfg = {"cli_models_enabled": True, "cli_spell_check_enabled": False}
    assert llm.spell_check_sentence(original, "Plain", cfg=disabled_cfg) == original
    assert calls == []
    assert any("Automatic CLI spell-check is off" in args[2] for args, _ in events)

    enabled_cfg = {"cli_models_enabled": True, "cli_spell_check_enabled": True}
    corrected = llm.spell_check_sentence(original, "Plain", cfg=enabled_cfg)
    assert corrected == "Fixed sentence"
    assert len(calls) == 1
    messages, request_enabled = calls[0]
    assert request_enabled is True
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "bad sentence" in messages[1]["content"]


def test_manual_rephrase_yields_once_without_footer():
    llm, _events = _load_isolated_llm()
    client, calls = _client(llm, reply="Polished text")
    llm._backend.client = client
    llm._stream_cancel.clear()

    chunks = list(llm.rephrase_sentence_stream(
        "rough text", "Plain", "Polish My Writing",
        cfg={"cli_models_enabled": True},
    ))
    assert chunks == ["Polished text"]
    assert "via Claude" not in chunks[0]
    assert calls[0][0][0]["content"].startswith("Polish the following writing.")
    assert calls[0][0][1]["content"] == "Rewrite:\nrough text"


def test_standalone_chat_replays_history_and_adds_safe_footer():
    llm, _events = _load_isolated_llm()
    client, calls = _client(llm, reply="Your name is Bo")
    llm._backend.client = client
    llm._stream_cancel.clear()
    history = [
        {"role": "user", "content": "My name is Bo"},
        {"role": "assistant", "content": "Nice to meet you"},
        {"role": "user", "content": "What is my name?"},
    ]

    chunks = list(llm.free_chat_stream(
        history, "Plain", {"cli_models_enabled": True}
    ))
    assert chunks == [
        "Your name is Bo\n\n*(via Claude Code · claude-haiku-4-5 · effort low)*"
    ]
    sent = calls[0][0]
    assert sent[0]["role"] == "system"
    assert sent[1:] == history


def test_existing_backend_still_receives_exact_raw_prompt():
    llm, _events = _load_isolated_llm()

    class ExistingClient:
        def __init__(self):
            self.calls = []

        def __call__(self, prompt, **kwargs):
            self.calls.append((prompt, kwargs))
            return {"choices": [{"text": "existing"}]}

    client = ExistingClient()
    llm._backend.client = client
    semantic = [{"role": "user", "content": "must be ignored"}]
    out = llm._complete_prompt(
        "EXACT OLD PROMPT", max_tokens=7, temperature=0.2,
        repeat_penalty=1.3, stop=["STOP"], echo=False,
        messages=semantic,
    )
    assert out == {"choices": [{"text": "existing"}]}
    assert client.calls == [("EXACT OLD PROMPT", {
        "max_tokens": 7,
        "temperature": 0.2,
        "repeat_penalty": 1.3,
        "stop": ["STOP"],
        "echo": False,
    })]


def test_config_and_startup_source_guards_exist():
    constants = (PACKAGE / "constants.py").read_text(encoding="utf-8")
    config = (PACKAGE / "config.py").read_text(encoding="utf-8")
    daemon = (PACKAGE / "daemon.py").read_text(encoding="utf-8")
    assert '"cli_models_enabled":     False' in constants
    assert '"cli_spell_check_enabled": False' in constants
    assert '("local", "server", "cloud", "cli")' in config
    assert 'cfg.get("cli_spell_check_enabled", False) and cfg["cli_models_enabled"]' in config
    assert 'if mode == "cli":' in daemon
    assert "backend = connect_cli(" in daemon
    assert "The selected CLI never falls back to another backend." in daemon


def main() -> int:
    tests = [
        test_connect_cli_is_detection_only_and_opt_in,
        test_shared_helpers_require_and_forward_semantic_messages,
        test_automatic_cli_spell_check_has_a_separate_default_off_gate,
        test_manual_rephrase_yields_once_without_footer,
        test_standalone_chat_replays_history_and_adds_safe_footer,
        test_existing_backend_still_receives_exact_raw_prompt,
        test_config_and_startup_source_guards_exist,
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
