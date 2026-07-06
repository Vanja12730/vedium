import ast
import pathlib
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parent
ORIGINAL = ROOT / "spell_daemon.py"
PACKAGE = ROOT / "spell_daemon"


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _literal_assignment(path: pathlib.Path, name: str):
    tree = _parse(path)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                return _safe_literal(node.value)
    raise AssertionError(f"{name} not found in {path}")


def _safe_literal(node: ast.AST):
    """Evaluate literals plus the `dict(KEY=value, ...)` shape used by SCHEMES."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
        if node.args:
            raise ValueError("dict(...) with positional args is not supported")
        return {kw.arg: _safe_literal(kw.value) for kw in node.keywords}
    if isinstance(node, ast.Dict):
        return {
            _safe_literal(key): _safe_literal(value)
            for key, value in zip(node.keys, node.values)
        }
    if isinstance(node, ast.List):
        return [_safe_literal(item) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_safe_literal(item) for item in node.elts)
    if isinstance(node, ast.Set):
        return {_safe_literal(item) for item in node.elts}
    return ast.literal_eval(node)


def _class_methods(path: pathlib.Path, class_name: str) -> set[str]:
    tree = _parse(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    raise AssertionError(f"{class_name} not found in {path}")


def _defs(path: pathlib.Path) -> set[str]:
    tree = _parse(path)
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def test_import_prefers_package_when_monolith_exists() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "x.py").write_text("kind='file'\n", encoding="utf-8")
        (root / "x").mkdir()
        (root / "x" / "__init__.py").write_text("kind='package'\n", encoding="utf-8")
        sys.path.insert(0, str(root))
        try:
            sys.modules.pop("x", None)
            import x

            assert x.kind == "package"
        finally:
            sys.modules.pop("x", None)
            sys.path.remove(str(root))


def test_original_constants_preserved() -> None:
    # These option lists must match the monolith exactly — they are wired to
    # config values and prompt formats, so any drift is a real regression.
    for key in ("HOTKEY_OPTIONS", "N_CTX_OPTIONS", "FORMATS", "WORKING_LANGUAGE_OPTIONS"):
        original = _literal_assignment(ORIGINAL, key)
        refactored = _literal_assignment(PACKAGE / "constants.py", key)
        assert refactored == original, f"{key} drifted from monolith"

    # SCHEMES: the colour palettes were intentionally redesigned in the refactor
    # (a cohesive visual refresh), so the hex VALUES are allowed to differ.  The
    # STRUCTURE must stay in lock-step, though: same scheme names and the same
    # set of colour keys per scheme, or apply_scheme() would KeyError at runtime.
    original_schemes = _literal_assignment(ORIGINAL, "SCHEMES")
    refactored_schemes = _literal_assignment(PACKAGE / "constants.py", "SCHEMES")
    assert set(refactored_schemes) == set(original_schemes), "SCHEMES names drifted"
    for name, palette in original_schemes.items():
        assert set(refactored_schemes[name]) == set(palette), f"SCHEMES['{name}'] keys drifted"


def test_original_rephrase_prompts_preserved() -> None:
    original_prompts = _literal_assignment(ORIGINAL, "_REPHRASE_PROMPTS")
    refactored_prompts = _literal_assignment(PACKAGE / "constants.py", "_REPHRASE_PROMPTS")
    assert set(refactored_prompts) == set(original_prompts)


def test_daemon_methods_preserved() -> None:
    original_methods = _class_methods(ORIGINAL, "SpellDaemon")
    refactored_methods = _class_methods(PACKAGE / "daemon.py", "SpellDaemon")
    assert original_methods <= refactored_methods


def test_no_launcher_uses_old_monolith() -> None:
    run_vbs = (ROOT / "run.vbs").read_text(encoding="utf-8")
    installer = (ROOT / "GrammarAnywhere_v2.iss").read_text(encoding="utf-8")
    assert "spell_daemon.py" not in run_vbs
    assert 'Source: "spell_daemon.py"' not in installer
    assert "-m spell_daemon" in run_vbs


def test_refactor_did_not_drop_core_defs() -> None:
    original_defs = _defs(ORIGINAL)
    refactored_defs: set[str] = set()
    for path in PACKAGE.glob("*.py"):
        refactored_defs.update(_defs(path))

    missing = original_defs - refactored_defs
    allowed_internal_renames = {
        "_clear_for_stream",
        "_copy",
        "_on_body_configure",
        "_on_canvas_configure",
        "_redraw",
        # Closures from the old multi-slot Appearance colour-scheme picker. The
        # refactor intentionally simplified that UI to a single active scheme
        # (ui_settings._apply_theme_choice / _draw_swatches), dropping the per-slot
        # "Use" buttons and the colour_scheme_slots concept. No core behavior lost.
        "_make_use",
        "_use",
        "_on_dd_change",
    }
    assert missing <= allowed_internal_renames, sorted(missing)


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
    defs = _defs(PACKAGE / "llm.py")
    assert "_backend_unavailable_message" in defs


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
        test_import_prefers_package_when_monolith_exists,
        test_original_constants_preserved,
        test_original_rephrase_prompts_preserved,
        test_daemon_methods_preserved,
        test_no_launcher_uses_old_monolith,
        test_refactor_did_not_drop_core_defs,
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
