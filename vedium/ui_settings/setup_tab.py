"""Setup tab: first-run checklist cards with live status."""

import os
import tkinter as tk

from .. import llm
from .. import diagnostics
from ..constants import (
    BG, SURFACE, SURF2, TEXT, MUTED, ACCENT, ACCENT2,
    WARN, ERR, SUCCESS, FONT_SIZE_OFFSET,
)


def _build_setup_tab(ui):
    """Build the Setup tab: first-run checklist cards with live status."""
    win, cfg = ui.win, ui.cfg
    frm_setup = ui.frm_setup
    notebook = ui.notebook
    _active_canvas, _setup_canvas = ui.active_canvas, ui.setup_canvas
    backend_var, model_var = ui.backend_var, ui.model_var
    server_url_var, autodetect_var = ui.server_url_var, ui.autodetect_var
    cloud_key_var, cloud_model_var = ui.cloud_key_var, ui.cloud_model_var
    cli_enabled_var, cli_model_var = ui.cli_enabled_var, ui.cli_model_var
    cli_model_ids = ui.cli_model_ids
    sc_mode_var, lang_var = ui.sc_mode_var, ui.lang_var

    setup_vars = {}
    setup_labels = {}

    frm_setup.columnconfigure(0, weight=1)

    def _draw_setup_quill(canvas):
        canvas.delete("all")
        canvas.create_line(38, 70, 75, 27, fill=MUTED, width=3, capstyle="round")
        canvas.create_line(32, 76, 42, 69, fill=TEXT, width=5, capstyle="round")
        for i, length in enumerate((24, 28, 31, 27, 20)):
            y = 63 - i * 9
            x = 43 + i * 7
            canvas.create_line(x, y, x + length, y - 18, fill=ACCENT, width=2, capstyle="round")
            canvas.create_line(x, y, x + length - 5, y + 7, fill=ACCENT2, width=2, capstyle="round")
        canvas.create_line(23, 82, 82, 82, fill=SURF2, width=1)
        canvas.create_line(52, 72, 90, 78, fill=SURF2, width=1, smooth=True)

    hero = tk.Frame(frm_setup, bg=BG)
    hero.grid(row=0, column=0, sticky="ew", pady=(4, 20))
    hero.columnconfigure(1, weight=1)
    quill = tk.Canvas(hero, width=105, height=95, bg=BG, highlightthickness=0, bd=0)
    quill.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(14, 24))
    _draw_setup_quill(quill)
    tk.Label(
        hero,
        text="Get ready in four steps.",
        font=("Georgia", 25 + FONT_SIZE_OFFSET, "bold"),
        bg=BG,
        fg=TEXT,
        anchor="w",
    ).grid(row=0, column=1, sticky="ew", pady=(6, 6))
    tk.Label(
        hero,
        text="Use this page when setting up the app for the first time or when something feels unfinished.",
        font=("Georgia", 11 + FONT_SIZE_OFFSET),
        bg=BG,
        fg=MUTED,
        justify="left",
        wraplength=650,
    ).grid(row=1, column=1, sticky="ew")

    def _setup_row(row: int, title: str, key: str, button_text: str, icon_text: str, command):
        card = tk.Frame(
            frm_setup,
            bg=SURFACE,
            highlightbackground=SURF2,
            highlightcolor=SURF2,
            highlightthickness=1,
            bd=0,
        )
        card.grid(row=row, column=0, sticky="ew", padx=(120, 44), pady=5)
        card.columnconfigure(1, weight=1)

        bubble = tk.Canvas(card, width=58, height=58, bg=SURFACE, highlightthickness=0, bd=0)
        bubble.grid(row=0, column=0, padx=(16, 18), pady=12)
        bubble.create_oval(4, 4, 54, 54, fill=BG, outline=SURF2, width=1)
        bubble.create_text(29, 29, text=icon_text, fill=ACCENT2,
                           font=("Segoe UI Symbol", 20 + FONT_SIZE_OFFSET))

        copy = tk.Frame(card, bg=SURFACE)
        copy.grid(row=0, column=1, sticky="ew", pady=12)
        var = tk.StringVar(master=win, value="")
        tk.Label(copy, text=title, font=("Georgia", 12 + FONT_SIZE_OFFSET, "bold"),
                 bg=SURFACE, fg=TEXT, anchor="w").pack(anchor="w")
        lbl = tk.Label(copy, textvariable=var, font=("Georgia", 10 + FONT_SIZE_OFFSET),
                       bg=SURFACE, fg=TEXT, anchor="w", justify="left", wraplength=560)
        lbl.pack(anchor="w", pady=(4, 0))

        tk.Button(card, text=button_text, font=("Georgia", 10 + FONT_SIZE_OFFSET),
                  bg=BG, fg=TEXT, activebackground=SURF2, activeforeground=TEXT,
                  relief="solid", borderwidth=1, padx=14, pady=6, cursor="hand2",
                  command=command).grid(row=0, column=2, sticky="e",
                                        padx=(18, 14), pady=12)
        setup_vars[key] = var
        setup_labels[key] = lbl

    def _go_model():
        notebook.select(2)

    def _go_behaviour():
        notebook.select(3)

    def _use_review_mode():
        sc_mode_var.set("Toast")
        diagnostics.record("setup", "info", "Writing mode set to review suggestions.")
        _refresh_setup_tab()

    def _use_auto_language():
        lang_var.set("Auto")
        diagnostics.record("setup", "info", "Writing language set to Auto.")
        _refresh_setup_tab()

    def _copy_test_sentence():
        sample = "Ths sentence has a small typo"
        try:
            win.clipboard_clear()
            win.clipboard_append(sample)
            diagnostics.record("setup", "info", "Test sentence copied.")
        except Exception as exc:
            diagnostics.record("clipboard", "error", "Could not copy test sentence.", str(exc))

    _setup_row(1, "AI connection", "backend", "\u2726  Choose AI", "\u2601", _go_model)
    _setup_row(2, "Writing", "writing", "\u25a4  Review first", "\u270d", _use_review_mode)
    _setup_row(3, "Language", "language", "\u2691  Use Auto", "\u25ce", _use_auto_language)
    _setup_row(4, "Test", "test", "\u2398  Copy sentence", "\u25a3", _copy_test_sentence)

    def _setup_status_text() -> tuple[str, str, str]:
        mode = backend_var.get()
        backend_now = llm._backend.label or ""
        if backend_now:
            backend_txt = f"Ready: {backend_now}"
            backend_col = SUCCESS
        elif mode == "cli":
            if not cli_enabled_var.get():
                backend_txt = "AI CLI models are switched off. Enable them in the Model tab."
                backend_col = WARN
            elif not cli_model_var.get().strip():
                backend_txt = "Needs a command-line model selection."
                backend_col = WARN
            elif cli_model_var.get().strip() not in cli_model_ids:
                backend_txt = "The selected command-line model is not currently detected."
                backend_col = ERR
            else:
                backend_txt = f"AI CLI selected: {cli_model_var.get().strip()}. Click Connect."
                backend_col = WARN
        elif mode == "cloud":
            if not cloud_key_var.get().strip():
                backend_txt = "Needs API key for the selected cloud provider."
                backend_col = WARN
            elif not cloud_model_var.get().strip():
                backend_txt = "Needs a cloud model selection."
                backend_col = WARN
            else:
                backend_txt = f"Cloud selected: {cloud_model_var.get().strip()}. Click Test & Connect."
                backend_col = WARN
        elif mode == "server":
            if server_url_var.get().strip():
                backend_txt = "Server URL set. Click Test / Auto-Detect."
                backend_col = WARN
            elif autodetect_var.get():
                backend_txt = "Will auto-detect a local server on startup."
                backend_col = WARN
            else:
                backend_txt = "Needs a server URL or auto-detect enabled."
                backend_col = WARN
        else:
            path = model_var.get().strip()
            if path and os.path.isfile(path):
                backend_txt = "Local model selected. Click Save & Load / Connect."
                backend_col = WARN
            elif path:
                backend_txt = "Local model path does not point to a file."
                backend_col = ERR
            else:
                backend_txt = "Choose a local model, local server, or cloud provider."
                backend_col = WARN
        return backend_txt, backend_col, mode

    # One pending refresh at a time -- see _status_refresh_id above.  This is also
    # called directly from the provider-change trace and other handlers, so it must
    # cancel any previous schedule or the 1s loop multiplies and compounds the lag.
    _setup_refresh_id = [None]

    def _refresh_setup_tab(_force=False):
        try:
            if not win.winfo_exists():
                return
            # Skip the work while the Setup tab isn't visible -- light heartbeat only.
            if not _force and _active_canvas[0] is not _setup_canvas:
                if _setup_refresh_id[0] is not None:
                    try:
                        win.after_cancel(_setup_refresh_id[0])
                    except Exception:
                        pass
                _setup_refresh_id[0] = win.after(1000, _refresh_setup_tab)
                return
            backend_txt, backend_col, _mode = _setup_status_text()
            setup_vars["backend"].set(backend_txt)
            setup_labels["backend"].config(fg=backend_col)

            sc_mode = sc_mode_var.get()
            if sc_mode == "Toast":
                setup_vars["writing"].set("Review suggestions before replacement.")
                setup_labels["writing"].config(fg=SUCCESS)
            elif sc_mode == "Auto":
                setup_vars["writing"].set("Automatic replacement is on.")
                setup_labels["writing"].config(fg=WARN)
            else:
                setup_vars["writing"].set("Sentence correction is off. Assistant hotkeys still work.")
                setup_labels["writing"].config(fg=WARN)

            lang = lang_var.get()
            setup_vars["language"].set(
                "Auto keeps output in the input language." if lang == "Auto" else f"Using {lang}."
            )
            setup_labels["language"].config(fg=SUCCESS if lang == "Auto" else MUTED)

            setup_vars["test"].set("Copy the sample sentence, paste it into any app, then type the final period yourself.")
            setup_labels["test"].config(fg=MUTED)
            if _setup_refresh_id[0] is not None:
                try:
                    win.after_cancel(_setup_refresh_id[0])
                except Exception:
                    pass
            _setup_refresh_id[0] = win.after(1000, _refresh_setup_tab)
        except tk.TclError:
            return

    _refresh_setup_tab(_force=True)

    # ── publish shared state ──────────────────────────────────────────────────
    # Replaces the no-op placeholder installed by _open_settings_ui, so the
    # Model tab's Test & Connect can refresh the checklist.
    ui.refresh_setup_tab = _refresh_setup_tab
