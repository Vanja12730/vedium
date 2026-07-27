"""Status tab: backend readiness, diagnostics summary, copyable details."""

import tkinter as tk

from .. import llm
from .. import diagnostics
from ..constants import (
    BG, SURFACE, SURF2, TEXT, MUTED, ACCENT, ACCENT2,
    WARN, ERR, SUCCESS, FONT_B, FONT_SIZE_OFFSET,
)


def _build_status_tab(ui):
    """Build the Status tab (live diagnostics view). Self-contained."""
    win, cfg = ui.win, ui.cfg
    frm_status = ui.frm_status
    _active_canvas, _status_canvas = ui.active_canvas, ui.status_canvas

    status_vars = {}
    status_value_labels = {}

    def _status_row(row: int, label: str, key: str):
        tk.Label(frm_status, text=label, font=FONT_B, bg=BG, fg=MUTED,
                 anchor="e", width=20).grid(row=row, column=0, sticky="ne",
                                            padx=(0, 10), pady=7)
        var = tk.StringVar(master=win, value="")
        value = tk.Label(frm_status, textvariable=var, font=FONT_B, bg=BG, fg=TEXT,
                         anchor="w", justify="left", wraplength=360)
        value.grid(row=row, column=1, sticky="ew", pady=7)
        status_vars[key] = var
        status_value_labels[key] = value

    _status_row(0, "Readiness", "readiness")
    _status_row(1, "Next step", "next_step")
    _status_row(2, "App", "enabled")
    _status_row(3, "Writing mode", "spell_check_mode")
    _status_row(4, "Backend", "backend")
    _status_row(5, "Activity", "busy")
    _status_row(6, "Last action", "last_action")
    _status_row(7, "Last error", "last_error")

    action_row = tk.Frame(frm_status, bg=BG)
    action_row.grid(row=8, column=1, sticky="w", pady=(4, 7))
    details_visible = tk.BooleanVar(master=win, value=False)

    def _clear_status_error():
        diagnostics.clear_last_error()
        diagnostics.record("status", "info", "Last error cleared.")
        _refresh_status_tab()

    def _copy_status_diagnostics():
        text = diagnostics.format_snapshot(cfg)
        try:
            win.clipboard_clear()
            win.clipboard_append(text)
            diagnostics.record("status", "info", "Diagnostics copied.")
        except Exception as exc:
            diagnostics.record("clipboard", "error", "Could not copy diagnostics.", str(exc))
        _refresh_status_tab()

    tk.Button(action_row, text="Clear error", font=FONT_B, bg=SURF2, fg=TEXT,
              relief="flat", padx=10, pady=4, cursor="hand2",
              command=_clear_status_error).pack(side="left", padx=(0, 8))
    details_btn = tk.Button(action_row, text="Show details", font=FONT_B,
                            bg=SURF2, fg=TEXT, relief="flat", padx=10, pady=4,
                            cursor="hand2", command=lambda: _toggle_status_details())
    details_btn.pack(side="left")

    details_frame = tk.Frame(frm_status, bg=BG)
    details_frame.grid(row=9, column=0, columnspan=2, sticky="nsew", pady=(12, 7))
    details_frame.columnconfigure(1, weight=1)
    details_frame.grid_remove()

    tk.Label(details_frame, text="Technical details", font=FONT_B, bg=BG, fg=MUTED,
             anchor="ne", width=20).grid(row=0, column=0, sticky="ne",
                                         padx=(0, 10), pady=(0, 7))
    details_head = tk.Frame(details_frame, bg=BG)
    details_head.grid(row=0, column=1, sticky="ew", pady=(0, 7))
    tk.Button(details_head, text="Copy diagnostics", font=FONT_B, bg=ACCENT,
              fg="white", relief="flat", padx=10, pady=4, cursor="hand2",
              command=_copy_status_diagnostics).pack(side="left")

    events_txt = tk.Text(details_frame, font=("Consolas", 8 + FONT_SIZE_OFFSET),
                         bg=SURFACE, fg=TEXT, relief="flat", wrap="word",
                         height=10, padx=9, pady=8)
    events_txt.grid(row=1, column=1, sticky="nsew")
    events_txt.config(state="disabled")
    frm_status.rowconfigure(9, weight=1)
    details_frame.rowconfigure(1, weight=1)

    def _toggle_status_details():
        show = not details_visible.get()
        details_visible.set(show)
        if show:
            details_frame.grid()
            details_btn.config(text="Hide details")
        else:
            details_frame.grid_remove()
            details_btn.config(text="Show details")

    def _readiness_for(state: dict, backend: str) -> tuple[str, str, str]:
        backend_ready = bool(backend and backend != "None")
        mode = state.get("spell_check_mode") or "Unknown"
        backend_mode = state.get("backend_mode") or "unknown"

        if not state.get("enabled"):
            return "Paused", "Enable the app from the tray menu when you want it to watch typing.", WARN
        if state.get("last_error"):
            return "Needs attention", "Check Last error below, then retry the action that failed.", ERR
        if not backend_ready:
            if backend_mode == "cli" and not cfg.get("cli_models_enabled", False):
                return "Needs consent", "Enable AI CLI models in the Model tab, then choose a detected model.", WARN
            if backend_mode == "cli" and not cfg.get("cli_model", "").strip():
                return "Needs setup", "Choose a detected command-line model in the Model tab.", WARN
            if backend_mode == "cloud" and not cfg.get("cloud_api_key", "").strip():
                return "Needs setup", "Add an API key in the Model tab, then click Test & Connect.", WARN
            return "Needs setup", "Choose or test a backend in the Model tab.", WARN
        if mode == "Off":
            return "Ready, spell check off", "Turn Writing mode to Toast or Auto to get sentence corrections. Assistant hotkeys still work.", WARN
        if backend_mode == "cli":
            return "Ready, assistant mode", "Manual Understand, Improve Writing, follow-ups, and Chat are ready. Automatic spell-check is not available on the AI CLI backend.", SUCCESS
        if state.get("busy"):
            return "Working", "Wait for the current correction or rephrase to finish.", ACCENT2
        return "Ready", "Select text to understand it, or use Improve Writing on selected or typed text.", SUCCESS

    # One pending refresh at a time.  This function is also invoked directly from
    # button handlers (Clear error / Copy diagnostics); without cancelling the
    # previous schedule, each direct call would fork another 1s loop that runs
    # forever and compounds, dragging the whole window down the more you use it.
    _status_refresh_id = [None]

    def _refresh_status_tab(_force=False):
        try:
            if not win.winfo_exists():
                return
            # While the Status tab isn't the visible one, skip the heavier snapshot
            # + events-text rebuild and just keep a light heartbeat.  This keeps the
            # window fully responsive while you move/resize it on another tab, while
            # still refreshing within ~1s of switching back.
            if not _force and _active_canvas[0] is not _status_canvas:
                if _status_refresh_id[0] is not None:
                    try:
                        win.after_cancel(_status_refresh_id[0])
                    except Exception:
                        pass
                _status_refresh_id[0] = win.after(1000, _refresh_status_tab)
                return
            snap = diagnostics.snapshot(cfg)
            state = snap["state"]
            backend = llm._backend.label or state.get("backend") or "None"
            if backend and backend != "None":
                diagnostics.set_status(backend=backend)

            enabled = "Enabled" if state.get("enabled") else "Disabled"
            mode = state.get("spell_check_mode") or "Unknown"
            backend_mode = state.get("backend_mode") or "unknown"
            readiness, next_step, readiness_colour = _readiness_for(state, backend)
            status_vars["readiness"].set(readiness)
            status_vars["next_step"].set(next_step)
            status_vars["enabled"].set(enabled)
            status_vars["spell_check_mode"].set(mode)
            status_vars["backend"].set(f"{backend} ({backend_mode})")
            status_vars["busy"].set("Working" if state.get("busy") else "Idle")
            status_vars["last_action"].set(state.get("last_action") or "None yet.")
            status_vars["last_error"].set(state.get("last_error") or "None.")

            status_value_labels["readiness"].config(fg=readiness_colour)
            status_value_labels["next_step"].config(fg=TEXT)
            status_value_labels["enabled"].config(fg=SUCCESS if state.get("enabled") else WARN)
            status_value_labels["backend"].config(fg=SUCCESS if backend and backend != "None" else WARN)
            status_value_labels["last_error"].config(fg=ERR if state.get("last_error") else MUTED)

            lines = []
            for event in snap["events"][:20]:
                detail = f" — {event['detail']}" if event.get("detail") else ""
                lines.append(
                    f"{event['time']}  {event['severity'].upper():7}  "
                    f"{event['component']}: {event['message']}{detail}"
                )
            events_txt.config(state="normal")
            events_txt.delete("1.0", "end")
            events_txt.insert("1.0", "\n".join(lines) if lines else "No events yet.")
            events_txt.config(state="disabled")
            if _status_refresh_id[0] is not None:
                try:
                    win.after_cancel(_status_refresh_id[0])
                except Exception:
                    pass
            _status_refresh_id[0] = win.after(1000, _refresh_status_tab)
        except tk.TclError:
            return

    _refresh_status_tab(_force=True)
