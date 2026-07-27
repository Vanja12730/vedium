"""Bottom bar: status label and the Connect flow.

Behaviour, Assistant, and Appearance settings apply and persist the moment
they are changed in their own tabs. This bar only handles the connection
settings (Model tab), which need an explicit action: loading a model or
connecting to a server/cloud/CLI backend is slow and can fail."""

import threading
import tkinter as tk

from .. import llm
from .. import diagnostics
from ..llm import connect_server, connect_cloud, connect_cli
from ..config import save_cfg
from ..constants import (
    BG, SURF2, ACCENT, ACCENT2, WARN, ERR, SUCCESS,
    FONT_B, FONT_SIZE_OFFSET,
)
from ..ui_thread import _ui_call


def _build_bottom_bar(ui):
    """Build the always-visible bottom bar: status label and the
    Connect button with the connection save_and_load flow."""
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
    cli_effort_var = ui.cli_effort_var
    _cli_effort_value = ui.cli_effort_value
    fmt_var, gpu_var, ctx_var = ui.fmt_var, ui.gpu_var, ui.ctx_var

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

    def _pending_connection_cfg() -> dict:
        """The connection values as currently shown in the Model tab — exactly
        what pressing Connect would write into cfg. Shared by the save flow
        and the unsaved-changes indicator so the two can never disagree."""
        mode = backend_var.get()
        return {
            "backend_mode": mode,
            "model_path": model_var.get() if mode == "local" else "",
            "server_url": server_url_var.get().strip() if mode == "server" else "",
            "cloud_provider": cloud_provider_var.get(),
            "cloud_base_url": cloud_url_var.get().strip(),
            "cloud_api_key": cloud_key_var.get().strip(),
            "cloud_model": cloud_model_var.get().strip(),
            "cli_models_enabled": bool(cli_enabled_var.get()),
            "cli_model": cli_model_var.get().strip(),
            "cli_reasoning_effort": _cli_effort_value(),
            "remember_cloud_api_key": bool(remember_key_var.get()),
            "auto_detect_server": autodetect_var.get(),
            "server_disable_thinking": nothink_var.get(),
            "prompt_format": fmt_var.get(),
            "gpu_acceleration": gpu_var.get(),
            "n_ctx": ctx_var.get(),
        }

    def _connection_dirty() -> bool:
        try:
            pending = _pending_connection_cfg()
        except tk.TclError:
            # A numeric entry is mid-edit (e.g. blank n_ctx) — that's unsaved.
            return True
        return any(cfg.get(key) != value for key, value in pending.items())

    _dirty_shown = [False]

    def _refresh_dirty_indicator(*_args):
        if _connection_dirty():
            _dirty_shown[0] = True
            status.config(
                text="Connection changes not applied yet — click Connect.",
                fg=WARN,
            )
        elif _dirty_shown[0]:
            # Values match the saved config again — clear our own warning,
            # without clobbering texts other flows put there.
            _dirty_shown[0] = False
            label = llm._backend.label or ""
            status.config(
                text=f"Ready ✓ ({label})" if label and label != "None" else "",
                fg=SUCCESS,
            )

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

        cfg.update(_pending_connection_cfg())
        # Keep provider keys in memory for this run; save_cfg strips secrets from disk.
        _remember_provider_key(cloud_provider_var.get(), cloud_key_var.get().strip())
        cfg["cloud_api_keys"]         = dict(_cloud_keys)
        cfg["cloud_favorites"]        = [dict(f) for f in _favorites]
        use_server = mode == "server"
        # Apply live so the change takes effect without reconnecting the server.
        llm._backend.server_disable_thinking = bool(nothink_var.get())
        _dirty_shown[0] = False
        diagnostics.set_status(
            enabled=cfg.get("enabled", True),
            backend_mode=cfg.get("backend_mode", "local"),
        )
        diagnostics.record("config", "info", "Connection settings save requested.")
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

        if model_changed:
            status.config(
                text={"cli": "Selecting command-line model…",
                      "cloud": "Connecting to cloud API…",
                      "server": "Connecting to server…"}.get(mode, "Loading model…"),
                fg=WARN,
            )
            threading.Thread(target=_load, daemon=True).start()
        else:
            status.config(text="Connection settings unchanged.", fg=SUCCESS)

    tk.Button(bottom_bar, text="\u221e  Connect",
              font=("Georgia", 12 + FONT_SIZE_OFFSET, "bold"),
              bg=ACCENT, fg="white", activebackground=ACCENT2, activeforeground="white",
              relief="flat", padx=44, pady=9, cursor="hand2",
              command=save_and_load).pack()

    # Live unsaved-changes indicator: any edit to a connection setting flips
    # the status line to a warning until Connect is pressed (or the value is
    # reverted). Behaviour/Assistant/Appearance settings save themselves, so
    # the Model tab is the only place changes can sit unapplied.
    for _var in (backend_var, model_var, server_url_var, autodetect_var,
                 nothink_var, cloud_provider_var, cloud_url_var, cloud_key_var,
                 cloud_model_var, remember_key_var, cli_enabled_var,
                 cli_model_var, cli_effort_var, fmt_var, gpu_var, ctx_var):
        _var.trace_add("write", _refresh_dirty_indicator)
    _refresh_dirty_indicator()
