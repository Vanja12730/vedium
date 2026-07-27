import json
import os
import ctypes
import ctypes.wintypes as wt
import sys
from . import constants as c
from . import diagnostics

# Secrets are never stored in the config file. The cloud API key is read from
# the environment so it can't leak if the config (or this whole folder) is
# shared, zipped, or committed. Set VEDIUM_CLOUD_API_KEY before launch.
# The SPELL_DAEMON_* name is honoured as a fallback for setups configured
# before the app was renamed to Vedium.
_API_KEY_ENVS = ("VEDIUM_CLOUD_API_KEY", "SPELL_DAEMON_CLOUD_API_KEY")

# Config keys that hold secrets and must never be written back to disk.
_SECRET_KEYS = ("cloud_api_key", "cloud_api_keys")
_CRED_PREFIX = "Vedium/cloud_api_key/"
# Keys stored before the rename live under the old prefix; reads fall back to
# it and re-store under the new prefix (see get_stored_cloud_api_key).
_LEGACY_CRED_PREFIX = "GrammarAnywhere/cloud_api_key/"

_MODE_RENAMES = {
    "Organize My Thoughts": "Polish My Writing",
    "Rephrase Text": "Polish My Writing",
    "Explain with Evidence": "Explain with Quotes",
    "Devil's Advocate": "Draft a Counterargument",
}


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wt.DWORD), ("dwHighDateTime", wt.DWORD)]


class _CREDENTIAL_ATTRIBUTE(ctypes.Structure):
    _fields_ = [
        ("Keyword", wt.LPWSTR),
        ("Flags", wt.DWORD),
        ("ValueSize", wt.DWORD),
        ("Value", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wt.DWORD),
        ("Type", wt.DWORD),
        ("TargetName", wt.LPWSTR),
        ("Comment", wt.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wt.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wt.DWORD),
        ("AttributeCount", wt.DWORD),
        ("Attributes", ctypes.POINTER(_CREDENTIAL_ATTRIBUTE)),
        ("TargetAlias", wt.LPWSTR),
        ("UserName", wt.LPWSTR),
    ]


_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2


def _credential_target(provider: str, prefix: str | None = None) -> str:
    provider = (provider or "default").strip() or "default"
    return (prefix or _CRED_PREFIX) + provider


def credential_store_available() -> bool:
    """Return True when Windows Credential Manager APIs are available."""
    return sys.platform.startswith("win")


def _read_credential(target: str) -> str:
    """Read one generic credential's secret, or "" if absent."""
    advapi = ctypes.windll.advapi32
    advapi.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    advapi.CredReadW.restype = wt.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    advapi.CredFree.restype = None

    cred_ptr = ctypes.POINTER(_CREDENTIAL)()
    if not advapi.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(cred_ptr)):
        return ""
    try:
        cred = cred_ptr.contents
        if not cred.CredentialBlob or cred.CredentialBlobSize == 0:
            return ""
        data = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return data.decode("utf-16-le", errors="replace").rstrip("\x00")
    finally:
        advapi.CredFree(cred_ptr)


def get_stored_cloud_api_key(provider: str) -> str:
    """Read a provider API key from Windows Credential Manager, if present.

    Keys saved before the app was renamed live under the GrammarAnywhere
    prefix; fall back to it and re-store under the Vedium prefix so the
    fallback retires itself after one read.
    """
    if not credential_store_available():
        return ""
    key = _read_credential(_credential_target(provider))
    if key:
        return key
    legacy = _read_credential(_credential_target(provider, _LEGACY_CRED_PREFIX))
    if legacy:
        store_cloud_api_key(provider, legacy)
    return legacy


def store_cloud_api_key(provider: str, api_key: str) -> bool:
    """Store a provider API key in Windows Credential Manager."""
    api_key = (api_key or "").strip()
    if not api_key or not credential_store_available():
        return False
    advapi = ctypes.windll.advapi32
    advapi.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wt.DWORD]
    advapi.CredWriteW.restype = wt.BOOL

    blob = api_key.encode("utf-16-le")
    blob_buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = _CREDENTIAL()
    cred.Type = _CRED_TYPE_GENERIC
    cred.TargetName = _credential_target(provider)
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(blob_buffer, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = "Vedium"
    ok = bool(advapi.CredWriteW(ctypes.byref(cred), 0))
    if ok:
        diagnostics.record("config", "info", f"Stored cloud API key securely for {provider}.")
    return ok


def delete_stored_cloud_api_key(provider: str) -> bool:
    """Remove a provider API key from Windows Credential Manager."""
    if not credential_store_available():
        return False
    advapi = ctypes.windll.advapi32
    advapi.CredDeleteW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD]
    advapi.CredDeleteW.restype = wt.BOOL
    ok = bool(advapi.CredDeleteW(_credential_target(provider), _CRED_TYPE_GENERIC, 0))
    # Also clear any pre-rename copy so it can't resurrect via the read fallback.
    ok = bool(advapi.CredDeleteW(
        _credential_target(provider, _LEGACY_CRED_PREFIX), _CRED_TYPE_GENERIC, 0)) or ok
    if ok:
        diagnostics.record("config", "info", f"Removed stored cloud API key for {provider}.")
    return ok


def _inject_stored_secrets(cfg: dict):
    """Populate in-memory cloud keys from Windows Credential Manager."""
    if not cfg.get("remember_cloud_api_key", True):
        return
    providers = []
    active = (cfg.get("cloud_provider") or "").strip()
    if active:
        providers.append(active)
    for fav in cfg.get("cloud_favorites", []):
        if isinstance(fav, dict):
            provider = (fav.get("provider") or "").strip()
            if provider and provider not in providers:
                providers.append(provider)

    keys = dict(cfg.get("cloud_api_keys", {}))
    for provider in providers:
        key = get_stored_cloud_api_key(provider)
        if key:
            keys[provider] = key
            if provider == active:
                cfg["cloud_api_key"] = key
    if keys:
        cfg["cloud_api_keys"] = keys


def _inject_env_secrets(cfg: dict):
    """Populate the in-memory cfg with the cloud API key from the environment.

    Mutates `cfg` in place so daemon / UI code that reads cfg["cloud_api_key"]
    keeps working unchanged, but the value only ever lives in memory — it is
    stripped again on save (see `save_cfg`).
    """
    env_key = ""
    for name in _API_KEY_ENVS:
        env_key = os.environ.get(name, "").strip()
        if env_key:
            break
    if not env_key:
        return
    cfg["cloud_api_key"] = env_key
    provider = cfg.get("cloud_provider", "")
    if provider:
        keys = dict(cfg.get("cloud_api_keys", {}))
        keys[provider] = env_key
        cfg["cloud_api_keys"] = keys

def _merge_custom_modes(cfg: dict):
    """
    Rebuild the live REPHRASE_MODES list and _REPHRASE_PROMPTS dict from
    the current config's custom_rephrase_modes dict, and apply any
    builtin_prompt_overrides on top of the factory prompts.
    """
    custom: dict    = cfg.get("custom_rephrase_modes", {})
    overrides: dict = cfg.get("builtin_prompt_overrides", {})

    translation_start = next(
        (i for i, m in enumerate(c._BUILTIN_MODES) if m.startswith("Translate ")),
        len(c._BUILTIN_MODES),
    )
    main_block   = c._BUILTIN_MODES[:translation_start]
    translations = c._BUILTIN_MODES[translation_start:]

    custom_names = [k for k in custom if k not in c._BUILTIN_MODES]
    c.REPHRASE_MODES = c._modes_with_headers(main_block + custom_names + translations)

    for name in c._BUILTIN_MODES:
        c._REPHRASE_PROMPTS[name] = c._FACTORY_PROMPTS[name]
    for name, prompt in overrides.items():
        if name in c._BUILTIN_MODES:
            c._REPHRASE_PROMPTS[name] = prompt

    for name, prompt in custom.items():
        if name not in c._BUILTIN_MODES:
            c._REPHRASE_PROMPTS[name] = prompt
    for name in list(c._REPHRASE_PROMPTS):
        if name not in c._BUILTIN_MODES and name not in custom:
            del c._REPHRASE_PROMPTS[name]


def load_cfg() -> dict:
    """Load config from disk, filling in any missing keys from DEFAULT_CFG."""
    try:
        # One-time upgrade: adopt the pre-rename config file if the Vedium one
        # doesn't exist yet. If the rename fails (file lock, permissions),
        # fall back to reading whichever file is present.
        if not os.path.exists(c.CONFIG_FILE) and os.path.exists(c.LEGACY_CONFIG_FILE):
            try:
                os.rename(c.LEGACY_CONFIG_FILE, c.CONFIG_FILE)
            except OSError:
                pass
        cfg_path = c.CONFIG_FILE if os.path.exists(c.CONFIG_FILE) else c.LEGACY_CONFIG_FILE
        with open(cfg_path, encoding='utf-8') as f:
            cfg = json.load(f)
        if "auto_replace" in cfg and "spell_check_mode" not in cfg:
            cfg["spell_check_mode"] = "Auto" if cfg["auto_replace"] else "Toast"
        cfg.pop("auto_replace", None)
        legacy_hotkey = cfg.get("rephrase_hotkey", "")
        missing_understand_hotkey = "understand_hotkey" not in cfg
        missing_writing_hotkey = "writing_hotkey" not in cfg
        missing_reading_mode = "rephrase_reading_mode" not in cfg
        missing_writing_mode = "rephrase_writing_mode" not in cfg
        for k, v in c.DEFAULT_CFG.items():
            cfg.setdefault(k, v)
        if missing_understand_hotkey and legacy_hotkey:
            cfg["understand_hotkey"] = legacy_hotkey
        cfg.pop("rephrase_hotkey", None)

        # The old single action key becomes Understand; choose a distinct key
        # for Improve Writing without stealing an existing per-mode shortcut.
        if missing_writing_hotkey:
            used_hotkeys = {
                str(v).strip().lower()
                for v in cfg.get("mode_hotkeys", {}).values()
                if str(v).strip()
            }
            used_hotkeys.add(str(cfg.get("understand_hotkey", "")).strip().lower())
            cfg["writing_hotkey"] = next((
                key for key in ("f9", "f8", "f11", "f12", "f5", "f4", "f3", "f2", "f1")
                if key not in used_hotkeys
            ), "f9")
        elif cfg.get("writing_hotkey") == cfg.get("understand_hotkey"):
            cfg["writing_hotkey"] = "f9" if cfg.get("understand_hotkey") != "f9" else "f8"
        if not cfg.get("gpu_acceleration"):
            cfg["gpu_acceleration"] = "Auto"
        if not cfg.get("working_language"):
            cfg["working_language"] = "Auto"
        if cfg.get("spell_check_mode") not in ("Auto", "Toast", "Off"):
            diagnostics.record(
                "config", "warning",
                "Invalid spell-check mode in config; restored Toast mode.",
                str(cfg.get("spell_check_mode")),
            )
            cfg["spell_check_mode"] = "Toast"
        if cfg.get("backend_mode") not in ("local", "server", "cloud", "cli"):
            diagnostics.record(
                "config", "warning",
                "Invalid backend mode in config; restored local mode.",
                str(cfg.get("backend_mode")),
            )
            cfg["backend_mode"] = "local"
        for key in ("rephrase_mode", "rephrase_reading_mode", "rephrase_writing_mode"):
            if cfg.get(key) in _MODE_RENAMES:
                cfg[key] = _MODE_RENAMES[cfg[key]]
        # Split the former shared default into the two contexts it was already
        # serving: a selected passage being read, and text being written.
        legacy_mode = cfg.get("rephrase_mode")
        if missing_reading_mode or not cfg.get("rephrase_reading_mode"):
            cfg["rephrase_reading_mode"] = (
                legacy_mode if legacy_mode in c.ANSWER_MODES else "What's This About?"
            )
        if missing_writing_mode or not cfg.get("rephrase_writing_mode"):
            cfg["rephrase_writing_mode"] = (
                legacy_mode if legacy_mode and legacy_mode not in c.ANSWER_MODES else "Polish My Writing"
            )
        for _d in ("mode_triggers", "mode_hotkeys", "builtin_prompt_overrides"):
            _m = cfg.get(_d, {})
            if not isinstance(_m, dict):
                continue
            for old_name, new_name in _MODE_RENAMES.items():
                if old_name not in _m:
                    continue
                if new_name not in _m:
                    _m[new_name] = _m[old_name]
                del _m[old_name]
    except Exception as exc:
        diagnostics.record("config", "warning", "Config could not be loaded; defaults restored.", str(exc))
        cfg = dict(c.DEFAULT_CFG)
    cfg["cli_models_enabled"] = bool(cfg.get("cli_models_enabled", False))
    # Automatic spell-check was removed for the AI CLI backend (2026-07-18):
    # per-sentence requests are slow and spend subscription usage. Drop the
    # legacy consent key from configs written by older versions.
    cfg.pop("cli_spell_check_enabled", None)
    if cfg.get("cli_reasoning_effort", "") not in (
        "", "low", "medium", "high", "xhigh", "max",
    ):
        diagnostics.record(
            "config", "warning",
            "Invalid CLI reasoning effort; restored the CLI default.",
            str(cfg.get("cli_reasoning_effort")),
        )
        cfg["cli_reasoning_effort"] = ""
    _inject_stored_secrets(cfg)
    _inject_env_secrets(cfg)
    c.apply_scheme(cfg.get("colour_scheme", "Forest"))
    c.apply_font_size(cfg.get("font_size_offset", 0))
    _merge_custom_modes(cfg)
    
    # Apply the persisted thinking preference to the live backend state.
    try:
        from . import llm
        llm._backend.server_disable_thinking = bool(cfg.get("server_disable_thinking", True))
    except (ImportError, AttributeError):
        pass
    return cfg


def save_cfg(cfg: dict) -> bool:
    """Persist `cfg` to disk, stripping secrets so they never touch the file."""
    to_write = {k: v for k, v in cfg.items() if k not in _SECRET_KEYS}
    to_write["cloud_api_key"]  = ""
    to_write["cloud_api_keys"] = {}
    try:
        with open(c.CONFIG_FILE, "w", encoding='utf-8') as f:
            json.dump(to_write, f, indent=2)
        diagnostics.record("config", "info", "Settings saved.")
        return True
    except OSError as exc:
        diagnostics.record("config", "error", "Settings not saved.", exc.strerror or str(exc))
        try:
            from .ui_toasts import _info_toast
            _info_toast(f"⚠ Settings not saved — {exc.strerror or exc}")
        except Exception:
            pass
        return False
