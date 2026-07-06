import ast
import pathlib


ROOT = pathlib.Path(__file__).resolve().parent
PACKAGE = ROOT / "spell_daemon"


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def test_no_launcher_uses_old_monolith() -> None:
    run_vbs = (ROOT / "run.vbs").read_text(encoding="utf-8")
    installer = (ROOT / "GrammarAnywhere_v2.iss").read_text(encoding="utf-8")
    assert "spell_daemon.py" not in run_vbs
    assert 'Source: "spell_daemon.py"' not in installer
    assert "-m spell_daemon" in run_vbs


def test_standalone_chat_imports_and_symbols_exist() -> None:
    tree = _parse(PACKAGE / "ui_settings.py")

    imported_from_llm = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "llm" and node.level == 1:
            imported_from_llm.update(alias.name for alias in node.names)

    assert "free_chat_stream" in imported_from_llm
    assert "_stream_cancel" in imported_from_llm
    assert "_streaming_answer" in imported_from_llm
    assert "_strip_reasoning" in imported_from_llm


def test_settings_split_globals_are_wired_to_modules() -> None:
    tree = _parse(PACKAGE / "ui_settings.py")
    imported_from_constants = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "constants" and node.level == 1:
            imported_from_constants.update(alias.name for alias in node.names)

    source = (PACKAGE / "ui_settings.py").read_text(encoding="utf-8")
    assert "_FACTORY_PROMPTS" in imported_from_constants
    assert "_refresh_setup_status" not in source
    assert "llm._server_disable_thinking = bool(nothink_var.get())" in source


def test_settings_uses_lightweight_ttk_theme() -> None:
    source = (PACKAGE / "ui_settings.py").read_text(encoding="utf-8")
    assert "import sv_ttk" not in source
    assert 'style.theme_use("clam")' in source


def test_settings_spell_mode_labels_are_valid_unicode() -> None:
    source = (PACKAGE / "ui_settings.py").read_text(encoding="utf-8")
    assert '"Auto  –  replace silently"' in source
    assert '"Toast  –  always ask first"' in source
    assert '"Off  –  disable (keep rephrase)"' in source


def test_backend_unavailable_message_exists() -> None:
    source = (PACKAGE / "llm.py").read_text(encoding="utf-8")
    assert "def _backend_unavailable_message" in source


def test_cloud_test_connect_updates_live_config() -> None:
    source = (PACKAGE / "ui_settings.py").read_text(encoding="utf-8")
    assert 'cfg["cloud_api_key"] = key' in source
    assert "API keys are not saved to disk" in source


def test_cloud_chat_quota_fallback_and_visible_error() -> None:
    llm_source = (PACKAGE / "llm.py").read_text(encoding="utf-8")
    ui_source = (PACKAGE / "ui_settings.py").read_text(encoding="utf-8")

    assert "def _is_cloud_quota_error" in llm_source
    assert "def _cloud_favorite_fallbacks" in llm_source
    assert "Trying favorite" in llm_source
    assert 'cfg["cloud_model"] = client.model' in llm_source
    assert "All available favorite cloud models hit a rate limit or quota" in llm_source
    assert "AI note:" in ui_source
    assert "str(_exc).strip()" in ui_source


def test_cloud_api_key_secure_storage() -> None:
    config_source = (PACKAGE / "config.py").read_text(encoding="utf-8")
    constants_source = (PACKAGE / "constants.py").read_text(encoding="utf-8")
    ui_source = (PACKAGE / "ui_settings.py").read_text(encoding="utf-8")

    assert '"remember_cloud_api_key": True' in constants_source
    assert "def get_stored_cloud_api_key" in config_source
    assert "def store_cloud_api_key" in config_source
    assert "def delete_stored_cloud_api_key" in config_source
    assert "_inject_stored_secrets(cfg)" in config_source
    assert 'to_write["cloud_api_key"]  = ""' in config_source
    assert "store_cloud_api_key(provider, key)" in ui_source
    assert "Windows Credential Manager" in ui_source


def main() -> int:
    tests = [
        test_no_launcher_uses_old_monolith,
        test_standalone_chat_imports_and_symbols_exist,
        test_settings_split_globals_are_wired_to_modules,
        test_settings_uses_lightweight_ttk_theme,
        test_settings_spell_mode_labels_are_valid_unicode,
        test_backend_unavailable_message_exists,
        test_cloud_test_connect_updates_live_config,
        test_cloud_chat_quota_fallback_and_visible_error,
        test_cloud_api_key_secure_storage,
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
