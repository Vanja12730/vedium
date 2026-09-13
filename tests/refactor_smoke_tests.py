# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

import ast
from datetime import datetime
import importlib.util
import os
import pathlib
import re
import tempfile
import threading


ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "vedium"


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _settings_ui_files() -> list[pathlib.Path]:
    """Every source file of the Settings window (the ui_settings package)."""
    return sorted((PACKAGE / "ui_settings").glob("*.py"))


def _settings_ui_source() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _settings_ui_files())


def _llm_layer_source() -> str:
    """Every source file of the LLM layer (facade + state/backends/postprocess)."""
    return "\n".join(
        (PACKAGE / name).read_text(encoding="utf-8")
        for name in ("llm.py", "llm_state.py", "llm_backends.py", "llm_postprocess.py")
    )


def test_no_launcher_uses_old_monolith() -> None:
    run_vbs = (ROOT / "run.vbs").read_text(encoding="utf-8")
    installer = (ROOT / "Vedium.iss").read_text(encoding="utf-8")
    assert "vedium.py" not in run_vbs
    assert 'Source: "vedium.py"' not in installer
    assert "-m vedium" in run_vbs


def test_project_sources_have_licensing_notices() -> None:
    python_files = sorted(PACKAGE.rglob("*.py"))
    python_files += sorted((ROOT / "tests").glob("*.py"))
    python_files += [ROOT / "make_ico.py", ROOT / "make_python_bundle.py"]
    for path in python_files:
        header = "\n".join(path.read_text(encoding="utf-8").splitlines()[:4])
        assert "Copyright (C) 2026 vehab" in header, path
        assert "SPDX-License-Identifier: GPL-3.0-only" in header, path

    for relative in ("Vedium.iss", "run.vbs", "run_spell.bat"):
        header = "\n".join((ROOT / relative).read_text(encoding="utf-8").splitlines()[:5])
        assert "Copyright (C) 2026 vehab" in header, relative
        assert "SPDX-License-Identifier: GPL-3.0-only" in header, relative

    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "commercial or proprietary terms" in contributing
    assert "Signed-off-by" in contributing


def test_privacy_buffer_boundaries_are_wired() -> None:
    daemon_source = (PACKAGE / "daemon.py").read_text(encoding="utf-8")
    buffer_source = (PACKAGE / "buffer.py").read_text(encoding="utf-8")
    assert "def clear(self) -> int:" in buffer_source
    assert "def trigger_with_epoch" in buffer_source
    assert "expected_epoch=context_epoch" in daemon_source
    assert "self._sync_foreground_typing_context()" in daemon_source
    assert "self._start_foreground_watcher()" in daemon_source
    assert "self.buf.clear()" in daemon_source
    assert "context != previous" in daemon_source
    assert "context.can_capture" in daemon_source


def test_installer_reuses_compatible_private_runtime_on_reinstall() -> None:
    installer = (ROOT / "Vedium.iss").read_text(encoding="utf-8")
    install_delete = installer.split("[InstallDelete]", 1)[1].split("[Icons]", 1)[0]

    assert "UsePreviousAppDir=yes" in installer
    assert '#define AppVersion   "1.3.1"' in installer
    assert "ArchitecturesAllowed=x64compatible" in installer
    assert '#define PythonBundleVersion "3.12.9-ui2"' in installer
    assert 'Name: "{app}\\python"; Check: ShouldResetPythonBundle' in install_delete
    assert "function ShouldResetPythonBundle(): Boolean;" in installer
    assert ".vedium-bundle-version" in installer
    assert "procedure CheckManagedLlamaServer();" in installer
    assert "CheckManagedLlamaServer();" in installer
    assert "CheckLlamaCpp" not in installer
    assert "DetectAVX512" not in installer
    assert "llama-cpp-python" not in installer
    assert "Minor >= 4" in installer
    assert ".vedium-server-version" in installer
    assert "cudart-llama-bin-win-cuda-12.4-x64.zip" in installer
    assert "existing managed engine — reused" in installer
    assert "{param:VediumEngine|}" in installer
    assert "{param:VediumModel|}" in installer
    assert "GEMMA4_FILENAME = 'gemma-4-E4B-it-Q4_K_M.gguf'" in installer
    assert "lmstudio-community/gemma-4-E4B-it-GGUF" in installer
    assert "function ChooseRecommendedModel(): String;" in installer
    assert "RbGemma4.Checked := GCudaOK" in installer
    assert "RbGemma3.Checked := not GCudaOK" in installer
    assert "ModelChoice = 'gemma4'" in installer
    assert "ModelChoice = 'gemma3'" in installer
    assert "CPU (same models; slower)" in installer
    assert "Runs the same supported GGUF models entirely on the processor" in installer
    assert "It works on CPU too" in installer
    assert "An 8 GB NVIDIA GPU is a practical fit" in installer
    assert "16 GB of system RAM" not in installer
    assert 'Source: "licenses\\*"' in installer
    assert 'Name: "{app}\\managed-server.log"' in installer
    assert "{app}\\engine-cache" in installer
    assert "Existing model/backend configuration preserved" in installer
    assert "ExpandConstant('{sysnative}\\nvidia-smi.exe')" in installer
    assert "Exec('cmd.exe', '/c nvidia-smi" not in installer


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
    assert "llm._backend.server_disable_thinking = bool(nothink_var.get())" in source


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
    source = _llm_layer_source()
    assert "def _backend_unavailable_message" in source


def test_cloud_test_connect_updates_live_config() -> None:
    source = _settings_ui_source()
    assert 'cfg["cloud_api_key"] = key' in source
    assert "API keys are not saved to disk" in source


def test_cloud_quota_fallback_is_shared_across_generation_paths() -> None:
    llm_source = _llm_layer_source()
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
    vedium = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SpellDaemon"
    )
    handler_node = next(
        node for node in vedium.body
        if isinstance(node, ast.FunctionDef) and node.name == "_handle_rephrase_hotkey_impl"
    )

    class FakeBuffer:
        def __init__(self):
            self.buffered = None
            self.trigger_calls = 0

        def epoch(self):
            return 0

        def is_busy_or_pending(self):
            return False

        def trigger_rephrase_with_epoch(self, _min_words):
            self.trigger_calls += 1
            return (*self.buffered, 0) if self.buffered else None

    selection = ["A selected passage"]
    notices = []
    namespace = {
        "_active_window_token": lambda: None,
        "_copy_selected_text": lambda **_kw: (selection[0], "old clipboard"),
        "focused_context": lambda: None,
        "_target_guard": lambda *_a: lambda: None,
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
    assert "def _dismiss_rephrase_toast(self, toast):" in daemon_source
    assert daemon_source.count("self._dismiss_rephrase_toast(toast)") >= 2


def test_reflections_are_local_structured_markdown() -> None:
    spec = importlib.util.spec_from_file_location(
        "vedium_reflections_test", PACKAGE / "reflections.py"
    )
    reflections = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(reflections)

    with tempfile.TemporaryDirectory() as tmp:
        documents = pathlib.Path(tmp) / "Documents"
        journal = reflections.reflection_journal_path(documents)
        assert journal == documents / "Vedium" / "Reflections.md"
        custom = documents / "My Notes" / "Reading.md"
        assert reflections.reflection_journal_path(configured_path=custom) == custom
        assert not journal.exists()

        reflections.append_reflection(
            "First line\n\nSecond line",
            "This changes how I understand the argument.",
            source="  Example   Book  ",
            mode="What's This About?",
            path=journal,
            created_at=datetime(2026, 8, 1, 9, 5),
        )
        reflections.append_reflection(
            "Another passage",
            "A reflection from another source.",
            source="Book B",
            path=journal,
            created_at=datetime(2026, 8, 1, 10, 30),
        )
        reflections.append_reflection(
            "Back to the first book",
            "A second reflection on the first source.",
            source="example book",
            path=journal,
            created_at=datetime(2026, 8, 1, 11, 45),
        )
        content = journal.read_text(encoding="utf-8")

        vocabulary = reflections.vocabulary_journal_path(
            documents, reflection_path=custom,
        )
        assert vocabulary == custom.with_name("Vocabulary.md")
        custom_vocabulary = documents / "Words.md"
        assert reflections.vocabulary_journal_path(
            configured_path=custom_vocabulary,
        ) == custom_vocabulary
        reflections.append_vocabulary_entry(
            "Bank", "A financial institution.", source="Book A",
            path=vocabulary, created_at=datetime(2026, 8, 1, 12, 0),
        )
        reflections.append_vocabulary_entry(
            "Liminal", "At a boundary or transitional stage.",
            path=vocabulary, created_at=datetime(2026, 8, 1, 12, 30),
        )
        reflections.append_vocabulary_entry(
            "bank", "The land beside a river.", source="Book B",
            path=vocabulary, created_at=datetime(2026, 8, 1, 13, 0),
        )
        # A term is a Markdown heading and the grouping key, so a whole
        # passage must be refused rather than written as a paragraph-long "##".
        passage = "This is an entire paragraph the user selected. " * 6
        assert not reflections.is_term_sized(passage)
        assert reflections.is_term_sized("liminal")
        assert not reflections.is_term_sized("   ")
        before = vocabulary.read_text(encoding="utf-8")
        try:
            reflections.append_vocabulary_entry(passage, "A definition.", path=vocabulary)
            raise AssertionError("a passage-length term should be refused")
        except ValueError as exc:
            assert str(reflections.MAX_TERM_LENGTH) in str(exc)
        assert vocabulary.read_text(encoding="utf-8") == before

        # Entry text must not be able to forge the sentinels that delimit
        # groups: the definitions are model-written and the sentinels are HTML
        # comments, so a forged one would open an invisible phantom group that
        # later entries could be filed under.
        reflections.append_vocabulary_entry(
            "Injected",
            "A definition that contains\r\n<!-- vedium-term -->\r\n"
            "## Term: Forged\r\n\r\nforged text",
            path=vocabulary, created_at=datetime(2026, 8, 1, 14, 0),
        )
        reflections.append_vocabulary_entry(
            "bank", "A third sense of the same word.",
            path=vocabulary, created_at=datetime(2026, 8, 1, 14, 30),
        )
        injected = vocabulary.read_text(encoding="utf-8")
        live_terms = re.findall(
            rf"(?m)^{re.escape('<!-- vedium-term -->')}\r?\n## Term: ([^\r\n]+)$",
            injected,
        )
        assert live_terms == ["Bank", "Liminal", "Injected"], live_terms
        assert "## Term: Forged" in injected      # present only as inert text
        assert "forged text" in injected          # content kept, just defused
        assert "\\<!-- vedium-term -->" in injected
        # The later real entry still files under the genuine heading, not the
        # forged one further down the file.
        assert (injected.index("A third sense of the same word.")
                < injected.index("## Term: Liminal"))

        # A rewrite is forced to disk before the rename swaps it in; otherwise a
        # crash could replace the whole journal with a truncated file.
        durability = []
        real_fsync, real_replace = os.fsync, os.replace
        reflections.os.fsync = lambda fd: (durability.append("fsync"), real_fsync(fd))[1]
        reflections.os.replace = lambda a, b: (durability.append("replace"), real_replace(a, b))[1]
        try:
            reflections.append_vocabulary_entry(
                "Durable", "Written safely.", path=vocabulary,
                created_at=datetime(2026, 8, 1, 15, 0),
            )
        finally:
            reflections.os.fsync, reflections.os.replace = real_fsync, real_replace
        assert durability == ["fsync", "replace"], durability

        # Atomic replace protects the file, but the whole read-modify-replace
        # must also be serialized or simultaneous saves silently lose entries.
        concurrent = documents / "Concurrent.md"
        barrier = threading.Barrier(12)
        thread_errors = []

        def append_concurrently(index):
            try:
                barrier.wait()
                reflections.append_vocabulary_entry(
                    "Concurrent", f"parallel entry {index}", path=concurrent,
                )
            except Exception as exc:
                thread_errors.append(exc)

        threads = [threading.Thread(target=append_concurrently, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not thread_errors, thread_errors
        concurrent_content = concurrent.read_text(encoding="utf-8")
        assert concurrent_content.count("## Term: Concurrent") == 1
        for i in range(12):
            assert f"parallel entry {i}" in concurrent_content

        vocabulary_content = vocabulary.read_text(encoding="utf-8")

    assert content.count("# Vedium Reflections") == 1
    assert content.count("## Source: Example Book") == 1
    assert content.count("## Source: Book B") == 1
    assert "### 2026-08-01 09:05" in content
    assert "**Mode:** What's This About?" in content
    assert "> First line\n>\n> Second line" in content
    assert "#### Reflection\n\nThis changes how I understand the argument." in content
    assert "### 2026-08-01 10:30" in content
    assert "### 2026-08-01 11:45" in content
    assert content.index("A second reflection on the first source.") < content.index("## Source: Book B")
    assert vocabulary_content.count("# Vedium Vocabulary") == 1
    assert vocabulary_content.count("## Term: Bank") == 1
    assert vocabulary_content.count("## Term: Liminal") == 1
    assert "#### Definition\n\nA financial institution." in vocabulary_content
    assert "**Source:** Book B" in vocabulary_content
    assert vocabulary_content.index("The land beside a river.") < vocabulary_content.index("## Term: Liminal")

    toast_source = (PACKAGE / "ui_toasts.py").read_text(encoding="utf-8")
    constants_source = (PACKAGE / "constants.py").read_text(encoding="utf-8")
    assert 'text="Reflect…"' in toast_source
    assert 'text="Change…"' in toast_source
    # Both journal pickers append to the chosen file, so neither may show the
    # "replace this file?" warning.
    assert toast_source.count("asksaveasfilename") == 2
    assert toast_source.count("confirmoverwrite=False") == 2
    assert 'cfg["reflection_journal_path"]' in toast_source
    assert '"reflection_window_position": None' in constants_source
    assert '"vocabulary_window_position": None' in constants_source
    # Both dialogs must go through the shared save/restore helpers, so neither
    # can quietly drift back to always opening centred.
    assert toast_source.count('"reflection_window_position"') == 2
    assert toast_source.count('"vocabulary_window_position"') == 2
    assert "win.winfo_x()" in toast_source and "win.winfo_y()" in toast_source
    assert "MonitorFromRect.restype = ctypes.c_void_p" in toast_source
    assert 'action_bar.pack(side="bottom", fill="x")' in toast_source
    assert 'text="Save reflection  ✓"' in toast_source
    assert '"vocabulary_journal_path": ""' in constants_source
    assert 'self._label == "Define This"' in toast_source
    assert 'text="Save to Vocabulary…"' in toast_source
    assert 'def _open_vocabulary_dialog(' in toast_source
    assert "append_vocabulary_entry(" in toast_source
    assert "self._save_vocabulary_btn.config(state=\"disabled\")" in toast_source
    assert 'state="normal" if r.strip() else "disabled"' in toast_source
    assert 'state="normal" if base.strip() else "disabled"' in toast_source
    assert 'self._save_vocabulary_btn.config(state="normal")' not in toast_source
    assert toast_source.count('cfg["reflection_source"] = source\n            save_cfg(cfg)') == 2
    assert "self._answer_only and not self._on_save_style" in toast_source
    assert "self._original" in toast_source


def main() -> int:
    tests = [
        test_no_launcher_uses_old_monolith,
        test_project_sources_have_licensing_notices,
        test_privacy_buffer_boundaries_are_wired,
        test_installer_reuses_compatible_private_runtime_on_reinstall,
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
        test_reflections_are_local_structured_markdown,
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
