"""Bottom bar: status label and the Save & Connect flow."""

import threading
import tkinter as tk

from .. import constants as c
from .. import llm
from .. import diagnostics
from ..llm import connect_server, connect_cloud, connect_cli
from ..config import save_cfg, _merge_custom_modes
from ..constants import (
    BG, SURF2, ACCENT, ACCENT2, WARN, ERR, SUCCESS,
    FONT_B, FONT_SIZE_OFFSET,
)
from ..ui_thread import _ui_call


def _build_bottom_bar(ui):
    """Build the always-visible bottom bar: status label and the
    Save & Connect button with the full save_and_load flow."""
    win, cfg = ui.win, ui.cfg
    on_cfg_saved, on_model_loaded = ui.on_cfg_saved, ui.on_model_loaded
    bottom_bar = ui.bottom_bar
    backend_var, model_var = ui.backend_var, ui.model_var
    server_url_var, autodetect_var = ui.server_url_var, ui.autodetect_var
    nothink_var = ui.nothink_var
    cloud_provider_var, cloud_url_var = ui.cloud_provider_var, ui.cloud_url_var
    cloud_key_var, cloud_model_var = ui.cloud_key_var, ui.cloud_model_var
    remember_key_var = ui.remember_key_var
    _cloud_keys, _favorites = ui.cloud_keys, ui.favorites
    _remember_provider_key = ui.remember_provider_key
    cli_enabled_var, cli_model_var = ui.cli_enabled_var, ui.cli_model_var
    cli_effort_var, cli_spell_var = ui.cli_effort_var, ui.cli_spell_var
    _cli_effort_value = ui.cli_effort_value
    fmt_var, gpu_var, ctx_var = ui.fmt_var, ui.gpu_var, ui.ctx_var
    lang_var, sc_mode_var, diacr_var = ui.lang_var, ui.sc_mode_var, ui.diacr_var
    _trig_string = ui.trig_string
    min_var, idle_var = ui.min_var, ui.idle_var
    understand_hotkey_var = ui.understand_hotkey_var
    writing_hotkey_var = ui.writing_hotkey_var
    reading_rephrase_var, writing_rephrase_var = ui.reading_rephrase_var, ui.writing_rephrase_var
    creativity_var = ui.creativity_var
    _custom, _builtin_overrides = ui.custom, ui.builtin_overrides
    _mode_triggers, _mode_hotkeys = ui.mode_triggers, ui.mode_hotkeys
    active_scheme_var, font_size_var = ui.active_scheme_var, ui.font_size_var
    FONT_SIZE_MAP = ui.FONT_SIZE_MAP

    # Show the live backend if one is already connected ("None" = not connected).
    _backend_now = llm._backend.label or ""
    status = tk.Label(bottom_bar,
                      text=f"Ready ✓ ({_backend_now})" if _backend_now and _backend_now != "None" else "",
                      font=FONT_B, bg=BG, fg=SUCCESS)
    divider = tk.Frame(bottom_bar, bg=SURF2, height=1)
    divider.pack(fill="x", padx=130, pady=(2, 8))
    tk.Label(bottom_bar, text="\u223c", font=("Georgia", 12 + FONT_SIZE_OFFSET),
             bg=BG, fg=SURF2).pack(pady=(0, 2))
    status.pack(pady=(0, 6))
    win.status_lbl = status

    def save_and_load():
        # Persist the chosen backend. Clear the inactive local/server values so the
        # startup check doesn't pick up a stale one; cloud_* are always kept so the
        # key isn't lost when switching backends (they're only USED in cloud mode).
        mode = backend_var.get()
        
        # Check if the model or backend settings actually changed
        old_mode = cfg.get("backend_mode")
        old_local_path = cfg.get("model_path")
        old_server_url = cfg.get("server_url")
        old_cloud_provider = cfg.get("cloud_provider")
        old_cloud_base_url = cfg.get("cloud_base_url")
        old_cloud_api_key = cfg.get("cloud_api_key")
        old_cloud_model = cfg.get("cloud_model")
        old_cli_enabled = cfg.get("cli_models_enabled", False)
        old_cli_model = cfg.get("cli_model", "")
        old_cli_effort = cfg.get("cli_reasoning_effort", "")
        old_gpu = cfg.get("gpu_acceleration")
        old_n_ctx = cfg.get("n_ctx")

        model_changed = (
            old_mode != mode or
            (mode == "local" and old_local_path != model_var.get()) or
            (mode == "server" and old_server_url != server_url_var.get().strip()) or
            (mode == "cloud" and (
                old_cloud_provider != cloud_provider_var.get() or
                old_cloud_base_url != cloud_url_var.get().strip() or
                old_cloud_api_key != cloud_key_var.get().strip() or
                old_cloud_model != cloud_model_var.get().strip()
            )) or
            (mode == "cli" and (
                old_cli_enabled != bool(cli_enabled_var.get()) or
                old_cli_model != cli_model_var.get().strip() or
                old_cli_effort != _cli_effort_value()
            )) or
            old_gpu != gpu_var.get() or
            old_n_ctx != ctx_var.get()
        )

        cfg["backend_mode"]           = mode
        cfg["model_path"]             = model_var.get() if mode == "local" else ""
        cfg["server_url"]             = server_url_var.get().strip() if mode == "server" else ""
        cfg["cloud_provider"]         = cloud_provider_var.get()
        cfg["cloud_base_url"]         = cloud_url_var.get().strip()
        cfg["cloud_api_key"]          = cloud_key_var.get().strip()
        cfg["cloud_model"]            = cloud_model_var.get().strip()
        cfg["cli_models_enabled"]     = bool(cli_enabled_var.get())
        cfg["cli_model"]              = cli_model_var.get().strip()
        cfg["cli_reasoning_effort"]   = _cli_effort_value()
        cfg["cli_spell_check_enabled"] = bool(
            cli_enabled_var.get() and cli_spell_var.get()
        )
        cfg["remember_cloud_api_key"] = bool(remember_key_var.get())
        # Keep provider keys in memory for this run; save_cfg strips secrets from disk.
        _remember_provider_key(cloud_provider_var.get(), cloud_key_var.get().strip())
        cfg["cloud_api_keys"]         = dict(_cloud_keys)
        cfg["cloud_favorites"]        = [dict(f) for f in _favorites]
        use_server = mode == "server"
        cfg["auto_detect_server"]     = autodetect_var.get()
        cfg["server_disable_thinking"] = nothink_var.get()
        # Apply live so the change takes effect without reconnecting the server.
        llm._backend.server_disable_thinking = bool(nothink_var.get())
        cfg["prompt_format"]          = fmt_var.get()
        cfg["gpu_acceleration"]       = gpu_var.get()
        cfg["working_language"]       = lang_var.get()
        cfg["trigger_chars"]          = _trig_string()
        cfg["min_word_count"]         = min_var.get()
        cfg["idle_reset_sec"]         = idle_var.get()
        cfg["spell_check_mode"]       = sc_mode_var.get()
        cfg["diacritics_only_auto"]   = diacr_var.get()
        cfg["understand_hotkey"]      = understand_hotkey_var.get()
        cfg["writing_hotkey"]         = writing_hotkey_var.get()
        cfg["rephrase_reading_mode"]  = reading_rephrase_var.get()
        cfg["rephrase_writing_mode"]  = writing_rephrase_var.get()
        cfg["rephrase_creativity"]    = creativity_var.get() / 100
        cfg["n_ctx"]                  = ctx_var.get()
        cfg["colour_scheme"]          = active_scheme_var.get()
        cfg.pop("colour_scheme_slots", None)

        old_offset = cfg.get("font_size_offset", 0)
        new_offset = FONT_SIZE_MAP.get(font_size_var.get(), 0)
        cfg["font_size_offset"]       = new_offset

        cfg["custom_rephrase_modes"]    = dict(_custom)
        cfg["builtin_prompt_overrides"] = dict(_builtin_overrides)
        cfg["mode_triggers"]            = dict(_mode_triggers)
        cfg["mode_hotkeys"]             = dict(_mode_hotkeys)
        _merge_custom_modes(cfg)
        diagnostics.set_status(
            enabled=cfg.get("enabled", True),
            spell_check_mode=cfg.get("spell_check_mode", "Auto"),
            backend_mode=cfg.get("backend_mode", "local"),
        )
        diagnostics.record("config", "info", "Settings save requested.")
        save_cfg(cfg)
        on_cfg_saved(cfg)

        def _load():
            try:
                if mode == "cli":
                    backend = connect_cli(
                        cfg["cli_model"], cfg["cli_models_enabled"],
                        cfg["cli_reasoning_effort"],
                    )
                elif mode == "cloud":
                    backend = connect_cloud(
                        cfg["cloud_base_url"], cfg["cloud_api_key"], cfg["cloud_model"],
                        cfg_n_ctx=max(cfg.get("n_ctx", 4096), 8192),
                    )
                elif use_server:
                    backend = connect_server(cfg["server_url"], cfg_n_ctx=cfg.get("n_ctx", 4096))
                else:
                    backend = on_model_loaded(model_var.get())
                def _refresh(b=backend):
                    active_win = ui.current_win()
                    if not active_win or not active_win.winfo_exists():
                        return
                    if hasattr(active_win, "status_lbl") and active_win.status_lbl.winfo_exists():
                        active_win.status_lbl.config(text=f"Ready ✓ ({b})", fg=SUCCESS)
                    if hasattr(active_win, "backend_lbl") and active_win.backend_lbl.winfo_exists():
                        if "GPU" in b and "fallback" not in b and "failed" not in b:
                            active_win.backend_lbl.config(fg=SUCCESS)
                        else:
                            active_win.backend_lbl.config(fg=WARN)
                    if hasattr(active_win, "backend_label_var"):
                        active_win.backend_label_var.set(f"  Active: {b}")
                _ui_call(_refresh, wait=False)
            except Exception as e:
                err_msg = str(e)
                diagnostics.record("backend", "error", "Backend load/connect failed.", err_msg)
                def _refresh_err(m=err_msg):
                    active_win = ui.current_win()
                    if active_win and active_win.winfo_exists() and hasattr(active_win, "status_lbl") and active_win.status_lbl.winfo_exists():
                        active_win.status_lbl.config(text=f"Error: {m}", fg=ERR)
                _ui_call(_refresh_err, wait=False)

        if new_offset != old_offset:
            global FONT_SIZE_OFFSET
            FONT_SIZE_OFFSET = new_offset
            c.apply_font_size(new_offset)
            ui.reopen()
            if model_changed:
                threading.Thread(target=_load, daemon=True).start()
            return

        if model_changed:
            status.config(
                text={"cli": "Saved. Selecting command-line model…",
                      "cloud": "Saved. Connecting to cloud API…",
                      "server": "Saved. Connecting to server…"}.get(mode, "Saved. Loading model…"),
                fg=WARN,
            )
            threading.Thread(target=_load, daemon=True).start()
        else:
            status.config(text="Saved.", fg=SUCCESS)

    tk.Button(bottom_bar, text="\u221e  Save & Connect",
              font=("Georgia", 12 + FONT_SIZE_OFFSET, "bold"),
              bg=ACCENT, fg="white", activebackground=ACCENT2, activeforeground="white",
              relief="flat", padx=44, pady=9, cursor="hand2",
              command=save_and_load).pack()
