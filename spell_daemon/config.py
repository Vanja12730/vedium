import json
import os
import ctypes
import ctypes.wintypes as wt
import sys
from . import constants as c
from . import diagnostics

# Secrets are never stored in the config file. The cloud API key is read from
# the environment so it can't leak if the config (or this whole folder) is
# shared, zipped, or committed. Set SPELL_DAEMON_CLOUD_API_KEY before launch.
_API_KEY_ENV = "SPELL_DAEMON_CLOUD_API_KEY"

# Config keys that hold secrets and must never be written back to disk.
_SECRET_KEYS = ("cloud_api_key", "cloud_api_keys")
_CRED_PREFIX = "GrammarAnywhere/cloud_api_key/"


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


def _credential_target(provider: str) -> str:
    provider = (provider or "default").strip() or "default"
    return _CRED_PREFIX + provider


def credential_store_available() -> bool:
    """Return True when Windows Credential Manager APIs are available."""
    return sys.platform.startswith("win")


def get_stored_cloud_api_key(provider: str) -> str:
    """Read a provider API key from Windows Credential Manager, if present."""
    if not credential_store_available():
        return ""
    advapi = ctypes.windll.advapi32
    advapi.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    advapi.CredReadW.restype = wt.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    advapi.CredFree.restype = None

    cred_ptr = ctypes.POINTER(_CREDENTIAL)()
    if not advapi.CredReadW(_credential_target(provider), _CRED_TYPE_GENERIC, 0, ctypes.byref(cred_ptr)):
        return ""
    try:
        cred = cred_ptr.contents
        if not cred.CredentialBlob or cred.CredentialBlobSize == 0:
            return ""
        data = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return data.decode("utf-16-le", errors="replace").rstrip("\x00")
    finally:
        advapi.CredFree(cred_ptr)


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
    cred.UserName = "GrammarAnywhere"
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
    env_key = os.environ.get(_API_KEY_ENV, "").strip()
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
        with open(c.CONFIG_FILE, encoding='utf-8') as f:
            cfg = json.load(f)
        if "auto_replace" in cfg and "spell_check_mode" not in cfg:
            cfg["spell_check_mode"] = "Auto" if cfg["auto_replace"] else "Toast"
        cfg.pop("auto_replace", None)
        for k, v in c.DEFAULT_CFG.items():
            cfg.setdefault(k, v)
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
        if cfg.get("backend_mode") not in ("local", "server", "cloud"):
            diagnostics.record(
                "config", "warning",
                "Invalid backend mode in config; restored local mode.",
                str(cfg.get("backend_mode")),
            )
            cfg["backend_mode"] = "local"
        if cfg.get("rephrase_mode") == "Organize My Thoughts":
            cfg["rephrase_mode"] = "Rephrase Text"
        for _d in ("mode_triggers", "mode_hotkeys"):
            _m = cfg.get(_d, {})
            if isinstance(_m, dict) and "Organize My Thoughts" in _m:
                _m["Rephrase Text"] = _m.pop("Organize My Thoughts")
    except Exception as exc:
        diagnostics.record("config", "warning", "Config could not be loaded; defaults restored.", str(exc))
        cfg = dict(c.DEFAULT_CFG)
    _inject_stored_secrets(cfg)
    _inject_env_secrets(cfg)
    c.apply_scheme(cfg.get("colour_scheme", "Forest"))
    c.apply_font_size(cfg.get("font_size_offset", 0))
    _merge_custom_modes(cfg)
    
    # We set _server_disable_thinking on the llm module namespace dynamically
    try:
        from . import llm
        llm._server_disable_thinking = bool(cfg.get("server_disable_thinking", True))
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
