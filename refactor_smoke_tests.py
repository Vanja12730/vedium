import ast
import pathlib


ROOT = pathlib.Path(__file__).resolve().parent
PACKAGE = ROOT / "spell_daemon"


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _settings_ui_files() -> list[pathlib.Path]:
    """Every source file of the Settings window (the ui_settings package)."""
    return sorted((PACKAGE / "ui_settings").glob("*.py"))


def _settings_ui_source() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _settings_ui_files())


def test_no_launcher_uses_old_monolith() -> None:
    run_vbs = (ROOT / "run.vbs").read_text(encoding="utf-8")
    installer = (ROOT / "Vedium.iss").read_text(encoding="utf-8")
    assert "spell_daemon.py" not in run_vbs
    assert 'Source: "spell_daemon.py"' not in installer
    assert "-m spell_daemon" in run_vbs


def test_standalone_chat_imports_and_symbols_exist() -> None:
    tree = _parse(PACKAGE / "ui_chat.py")

    imported_from_llm = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "llm" and node.level == 1:
            imported_from_llm.update(alias.name for alias in node.names)

    assert "free_chat_stream" in imported_from_llm
    assert "_stream_cancel" in imported_from_llm
    assert "_streaming_answer" in imported_from_llm
    assert "_strip_reasoning" in imported_from_llm


def test_settings_split_globals_are_wired_to_modules() -> None:
    imported_from_constants = set()
    for path in _settings_ui_files():
        for node in _parse(path).body:
            if isinstance(node, ast.ImportFrom) and node.module == "constants" and node.level in (1, 2):
                imported_from_constants.update(alias.name for alias in node.names)

    source = _settings_ui_source()
    assert "_FACTORY_PROMPTS" in imported_from_constants
    assert "_refresh_setup_status" not in source
    assert "llm._server_disable_thinking = bool(nothink_var.get())" in source


def test_settings_uses_lightweight_ttk_theme() -> None:
    settings_source = _settings_ui_source()
    thread_source = (PACKAGE / "ui_thread.py").read_text(encoding="utf-8")
    assert "import sv_ttk" not in settings_source
    assert "import sv_ttk" not in thread_source
    assert 'style.theme_use("clam")' in thread_source


def test_settings_spell_mode_labels_are_valid_unicode() -> None:
    source = _settings_ui_source()
    assert '"Auto  –  replace silently"' in source
    assert '"Toast  –  always ask first"' in source
    assert '"Off  –  disable (keep rephrase)"' in source


def test_backend_unavailable_message_exists() -> None:
    source = (PACKAGE / "llm.py").read_text(encoding="utf-8")
    assert "def _backend_unavailable_message" in source


def test_cloud_test_connect_updates_live_config() -> None:
    source = _settings_ui_source()
    assert 'cfg["cloud_api_key"] = key' in source
    assert "API keys are not saved to disk" in source


def test_cloud_quota_fallback_is_shared_across_generation_paths() -> None:
    llm_source = (PACKAGE / "llm.py").read_text(encoding="utf-8")
    daemon_source = (PACKAGE / "daemon.py").read_text(encoding="utf-8")
    toasts_source = (PACKAGE / "ui_toasts.py").read_text(encoding="utf-8")

    assert "def _is_cloud_quota_error" in llm_source
    assert "def _is_cloud_server_error" in llm_source
    assert "def _is_cloud_transient_error" in llm_source
    assert "def _cloud_favorite_fallbacks" in llm_source
    assert "def _cloud_stream_with_fallback" in llm_source
    assert "def _cloud_complete_with_fallback" in llm_source
    assert "received_reply or not _is_cloud_transient_error" in llm_source
    assert 'cfg["cloud_model"] = client.model' in llm_source
    assert "All available favorite cloud models hit a rate limit, quota, or provider error" in llm_source
    assert "cfg=self.cfg" in daemon_source
    assert "on_fallback=toast.show_model_switch" in daemon_source
    assert "cfg=self._cfg" in toasts_source
    assert "on_fallback=self.show_model_switch" in toasts_source
    # Every path that can switch models must tell the user which model answered:
    # streaming toasts (show_model_switch), the non-streaming spell toast
    # (switches collected during spell_check_sentence, shown once the toast is
    # up), and the chat transcript (inline notice line).
    assert "using {next_model} instead" in toasts_source
    assert "cloud_switches.append((failed, nxt))" in daemon_source
    assert "toast.show_model_switch(cloud_switches[0][0], cloud_switches[-1][1])" in daemon_source
    assert "using {nxt} instead" in llm_source


def test_cloud_api_key_secure_storage() -> None:
    config_source = (PACKAGE / "config.py").read_text(encoding="utf-8")
    constants_source = (PACKAGE / "constants.py").read_text(encoding="utf-8")
    ui_source = _settings_ui_source()

    assert '"remember_cloud_api_key": True' in constants_source
    assert "def get_stored_cloud_api_key" in config_source
    assert "def store_cloud_api_key" in config_source
    assert "def delete_stored_cloud_api_key" in config_source
    assert "_inject_stored_secrets(cfg)" in config_source
    assert 'to_write["cloud_api_key"]  = ""' in config_source
    assert "store_cloud_api_key(provider, key)" in ui_source
    assert "Windows Credential Manager" in ui_source


def test_tightened_assistant_modes_are_registered() -> None:
    constants_source = (PACKAGE / "constants.py").read_text(encoding="utf-8")

    modes = constants_source[
        constants_source.index("REPHRASE_MODES = ["):
        constants_source.index("REPHRASE_MODES = _modes_with_headers")
    ]
    answers = constants_source[
        constants_source.index("ANSWER_MODES = frozenset"):
        constants_source.index("def _is_answer_mode")
    ]
    prompt = constants_source[
        constants_source.index('"Explain with Quotes":'):
        constants_source.index('"More Formal (Academic)":')
    ]

    assert '"Explain with Quotes"' in modes
    assert '"Explain with Quotes"' in answers
    assert "grounded only in the selected passage" in prompt
    assert "short EXACT quote" in prompt
    assert "at most two bullet lines" in prompt
    assert '"Polish My Writing"' in modes
    assert '"Draft a Counterargument"' in modes
    assert "Make only changes that improve clarity" in constants_source
    assert "Do NOT invent facts, evidence" in constants_source
    assert "Do NOT invent evidence" in constants_source


def test_assistant_hotkeys_route_by_explicit_action() -> None:
    import threading
    import types

    # Execute the real routing method with dependency-free fakes. Importing the
    # daemon itself would require pynput, which this zero-install smoke harness
    # intentionally does not require.
    tree = _parse(PACKAGE / "daemon.py")
    spell_daemon = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SpellDaemon"
    )
    handler_node = next(
        node for node in spell_daemon.body
        if isinstance(node, ast.FunctionDef) and node.name == "_handle_rephrase_hotkey_impl"
    )

    class FakeBuffer:
        def __init__(self):
            self.buffered = None
            self.trigger_calls = 0

        def is_busy_or_pending(self):
            return False

        def trigger_rephrase(self, _min_words):
            self.trigger_calls += 1
            return self.buffered

    selection = ["A selected passage"]
    notices = []
    namespace = {
        "_active_window_token": lambda: None,
        "_copy_selected_text": lambda: (selection[0], "old clipboard"),
        "_info_toast": notices.append,
        "_is_answer_mode": lambda mode: mode in {
            "What's This About?", "Define This", "Key Takeaways",
            "Explain It", "Explain It (Simply)", "Explain with Quotes",
        },
        "_resolve_trigger_mode": lambda text, _triggers: (None, text),
        "_word_count": lambda text: len(text.split()),
        "diagnostics": types.SimpleNamespace(record=lambda *_args, **_kwargs: None),
        "time": __import__("time"),
    }
    method_module = ast.Module(body=[handler_node], type_ignores=[])
    ast.fix_missing_locations(method_module)
    exec(compile(method_module, str(PACKAGE / "daemon.py"), "exec"), namespace)
    handle = namespace["_handle_rephrase_hotkey_impl"]

    assistant = types.SimpleNamespace()
    assistant.cfg = {
        "rephrase_reading_mode": "What's This About?",
        "rephrase_writing_mode": "Polish My Writing",
        "mode_triggers": {},
        "min_word_count": 2,
    }
    assistant.buf = FakeBuffer()
    assistant._active_rephrase_lock = threading.Lock()
    assistant._active_rephrase_toast = None
    calls = []
    assistant._selected_rephrase_pipeline = lambda text, _clipboard, **kwargs: calls.append(
        ("selected", text, kwargs["mode_override"]))
    assistant._rephrase_pipeline = lambda text, _raw_len, **kwargs: calls.append(
        ("buffer", text, kwargs["mode_override"]))

    handle(assistant, "understand")
    assert calls == [("selected", "A selected passage", "What's This About?")]
    assert assistant.buf.trigger_calls == 0

    calls.clear()
    handle(assistant, "write")
    assert calls == [("selected", "A selected passage", "Polish My Writing")]

    calls.clear()
    selection[0] = ""
    assistant.buf.buffered = ("Recently typed draft", 20)
    handle(assistant, "write")
    assert calls == [("buffer", "Recently typed draft", "Polish My Writing")]

    calls.clear()
    assistant.buf.trigger_calls = 0
    handle(assistant, "understand")
    assert calls == []
    assert assistant.buf.trigger_calls == 0
    assert notices and "Select some text first" in notices[-1]

    calls.clear()
    selection[0] = "Selected again"
    handle(assistant, "understand", mode_override="Polish My Writing")
    assert calls == [("selected", "Selected again", "What's This About?")]


def test_legacy_hotkey_and_mode_migration_exists() -> None:
    constants_source = (PACKAGE / "constants.py").read_text(encoding="utf-8")
    config_source = (PACKAGE / "config.py").read_text(encoding="utf-8")
    daemon_source = (PACKAGE / "daemon.py").read_text(encoding="utf-8")

    assert '"understand_hotkey":' in constants_source
    assert '"writing_hotkey":' in constants_source
    assert 'self.cfg.get("understand_hotkey", "f8")' in daemon_source
    assert 'self.cfg.get("writing_hotkey", "f9")' in daemon_source
    assert 'kwargs={"action": "understand"}' in daemon_source
    assert 'kwargs={"action": "write"}' in daemon_source
    assert 'legacy_hotkey = cfg.get("rephrase_hotkey", "")' in config_source
    assert 'cfg.pop("rephrase_hotkey", None)' in config_source
    assert '"Rephrase Text": "Polish My Writing"' in config_source


def main() -> int:
    tests = [
        test_no_launcher_uses_old_monolith,
        test_standalone_chat_imports_and_symbols_exist,
        test_settings_split_globals_are_wired_to_modules,
        test_settings_uses_lightweight_ttk_theme,
        test_settings_spell_mode_labels_are_valid_unicode,
        test_backend_unavailable_message_exists,
        test_cloud_test_connect_updates_live_config,
        test_cloud_quota_fallback_is_shared_across_generation_paths,
        test_cloud_api_key_secure_storage,
        test_tightened_assistant_modes_are_registered,
        test_assistant_hotkeys_route_by_explicit_action,
        test_legacy_hotkey_and_mode_migration_exists,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
