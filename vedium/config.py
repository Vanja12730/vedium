# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

import json
import os
import re
import ctypes
import ctypes.wintypes as wt
import sys
from . import constants as c
from . import diagnostics
from . import gguf


# ── prompt-format detection ───────────────────────────────────────────────────
# A GGUF ships the chat template it was trained with, so the right prompt format
# can be read off the model instead of being kept in sync by hand. Each entry is
# (marker in the chat template, FORMATS label); the list is ordered so that the
# most distinctive markers win — Zephyr's <|user|> tags in particular would
# otherwise match templates that merely mention the role names.
_TEMPLATE_MARKERS = [
    ("<|im_start|>",            "ChatML  (Qwen / Phi-3 / Yi)"),
    ("<|start_header_id|>",     "Llama 3  (Meta)"),
    ("<start_of_turn>",         "Gemma  (Google)"),
    ("<|START_OF_TURN_TOKEN|>", "Command-R  (Cohere)"),
    ("<|channel|>",             "Harmony  (gpt-oss)"),
    ("### Instruction",         "Alpaca / Vicuna"),
    ("[INST]",                  "Mistral / Mixtral"),
    ("<|assistant|>",           "Zephyr / HF Chat"),
]

# Fallback when a GGUF carries no chat template: the architecture field.
_ARCH_FORMATS = {
    "gemma":  "Gemma  (Google)",
    "gptoss": "Harmony  (gpt-oss)",
    "qwen":   "ChatML  (Qwen / Phi-3 / Yi)",
    "phi":    "ChatML  (Qwen / Phi-3 / Yi)",
    "deepseek": "DeepSeek",
    "command": "Command-R  (Cohere)",
}

# model_path -> detected label. Detection reads the file header, so cache it
# rather than re-reading on every cfg lookup.
_format_cache: dict = {}


def detect_prompt_format(model_path: str):
    """Prompt format implied by a GGUF's own chat template. None if unknown."""
    if not model_path:
        return None
    if model_path in _format_cache:
        return _format_cache[model_path]

    meta = gguf.read_metadata(
        model_path, ["tokenizer.chat_template", "general.architecture"])
    template = str(meta.get("tokenizer.chat_template", ""))

    fmt = None
    for marker, label in _TEMPLATE_MARKERS:
        if marker in template:
            fmt = label
            break
    if fmt is None:
        arch = str(meta.get("general.architecture", "")).lower()
        for key, label in _ARCH_FORMATS.items():
            if key in arch:
                fmt = label
                break

    _format_cache[model_path] = fmt
    return fmt


def resolve_prompt_format(cfg: dict) -> str:
    """The prompt format to actually use.

    An explicit choice in the config always wins. On "Auto" the format is read
    from the model; if that fails (no template, unreadable file, a backend with
    no local GGUF at all) it falls back to ChatML, the previous default.
    """
    chosen = str(cfg.get("prompt_format", c.AUTO_FORMAT)).strip()
    if chosen and chosen != c.AUTO_FORMAT:
        return chosen

    detected = detect_prompt_format(cfg.get("model_path", ""))
    if detected:
        return detected
    return "ChatML  (Qwen / Phi-3 / Yi)"

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


def _parse_generated_style(text: str) -> tuple[str, str]:
    """Return the AI-generated style name and profile, tolerating light Markdown."""
    text = (text or "").strip()
    name_match = re.search(
        r"(?im)^\s*(?:[#*-]+\s*)?(?:\*\*)?STYLE NAME\s*:\s*(?:\*\*)?\s*(.+?)\s*$",
        text,
    )
    profile_match = re.search(
        r"(?im)^\s*(?:[#*-]+\s*)?(?:\*\*)?STYLE PROFILE\s*:\s*(?:\*\*)?\s*",
        text,
    )
    name = re.sub(r"\s+", " ", name_match.group(1).strip(" \t#*_'\".")) if name_match else ""
    if len(name) > 64:
        name = name[:64].rstrip()
    profile = text[profile_match.end():].strip() if profile_match else text
    if name_match and not profile_match:
        profile = (text[:name_match.start()] + text[name_match.end():]).strip()
    return name or "New Writing Style", profile


def _style_mode_prompt(profile: str) -> str:
    """Wrap an extracted profile in the safety rules required by writing modes."""
    return (
        "Rewrite the following text using the writing style described below. "
        "Match its voice, register, sentence rhythm, diction, paragraph structure, rhetorical habits, "
        "imagery, and punctuation where the target text supports them. "
        "Preserve the target text's meaning, facts, names, examples, qualifications, and level of certainty. "
        "Do NOT copy distinctive phrases, metaphors, names, or subject matter from the reference sample. "
        "Do NOT add facts, claims, examples, or conclusions.\n\n"
        f"STYLE PROFILE:\n{profile.strip()}\n\n"
        "Return ONLY the rewritten text. No explanation. No extra lines."
    )


def _add_generated_style_mode(cfg: dict, name: str, profile: str) -> None:
    """Add an extracted style as a custom mode and select it for Improve Writing."""
    name = (name or "").strip()
    profile = (profile or "").strip()
    if not name:
        raise ValueError("Name cannot be empty.")
    if not profile:
        raise ValueError("Style profile cannot be empty.")
    if name in c._BUILTIN_MODES:
        raise ValueError("That name is reserved for a built-in mode.")
    custom = dict(cfg.get("custom_rephrase_modes", {}))
    if name in custom:
        raise ValueError("A writing mode with that name already exists.")
    custom[name] = _style_mode_prompt(profile)
    cfg["custom_rephrase_modes"] = custom
    cfg["rephrase_writing_mode"] = name
    _merge_custom_modes(cfg)


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
