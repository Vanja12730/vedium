# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Behaviour tab: language, spell-check mode, and advanced trigger options."""

import tkinter as tk
import tkinter.ttk as ttk

from .. import diagnostics
from ..config import save_cfg
from ..constants import BG, SURFACE, TEXT, MUTED, ACCENT2, FONT_B, WORKING_LANGUAGE_OPTIONS
from ..ui_widgets import _add_tooltip, _make_toggle, lbl_row, _TGL_INDENT


def _build_behaviour_tab(ui):
    """Build the Behaviour tab: language, spell-check mode, diacritics-only
    toggle, and the Advanced disclosure (trigger chars / min words / idle).

    Every control applies and persists immediately — no Save button involved.
    Connection settings (Model tab) are the only ones behind an explicit
    Connect action."""
    win, cfg = ui.win, ui.cfg
    frm_behaviour = ui.frm_behaviour
    _bind_scroll = ui.bind_scroll
    on_cfg_saved = ui.on_cfg_saved

    def _commit(key: str, value, note: str):
        """Apply one behaviour setting immediately: update the live config,
        persist it, and push it to the running daemon."""
        if cfg.get(key) == value:
            return
        cfg[key] = value
        save_cfg(cfg)
        on_cfg_saved(cfg)
        if key == "spell_check_mode":
            diagnostics.set_status(spell_check_mode=value)
        diagnostics.record("config", "info", note)

    def _commit_spin(key: str, var, fallback, cast, note: str):
        """Commit a spinbox value; restore the stored value on unparsable input."""
        try:
            value = cast(var.get())
        except (tk.TclError, ValueError):
            var.set(cfg.get(key, fallback))
            return
        _commit(key, value, note)

    # ── Working language ──────────────────────────────────────────────────────
    lang_var = tk.StringVar(master=win, value=cfg.get("working_language", "Auto"))
    lbl_row("Working language", 0, frm_behaviour)
    lf = tk.Frame(frm_behaviour, bg=BG)
    lf.grid(row=0, column=1, sticky="w", pady=7)
    lang_cb = ttk.Combobox(lf, textvariable=lang_var, values=WORKING_LANGUAGE_OPTIONS,
                           state="readonly", font=FONT_B, width=14)
    lang_cb.pack(anchor="w")
    _bind_scroll(lang_cb)
    lang_cb.bind("<<ComboboxSelected>>", lambda _e: _commit(
        "working_language", lang_var.get(),
        f"Working language set to {lang_var.get()}."))
    tk.Label(lf, text="Auto: output stays in the same language as the input",
             font=("Segoe UI", 8), bg=BG, fg=MUTED).pack(anchor="w")

    # ── Spell-check mode radio group ─────────────────────────────────────────
    # Three options in one row: Auto (silent replace) / Toast / Off.
    sc_mode_var = tk.StringVar(master=win, value=cfg.get("spell_check_mode", "Auto"))
    lbl_row("Spell-check", 1, frm_behaviour)
    sc_frame = tk.Frame(frm_behaviour, bg=BG)
    sc_frame.grid(row=1, column=1, sticky="w", pady=7)
    _SC_MODES = [
        ("Auto",  "Auto  –  replace silently"),
        ("Toast", "Toast  –  always ask first"),
        ("Off",   "Off  –  disable (keep rephrase)"),
    ]
    for _sc_val, _sc_label in _SC_MODES:
        tk.Radiobutton(
            sc_frame, text=_sc_label, variable=sc_mode_var, value=_sc_val,
            font=FONT_B, bg=BG, fg=TEXT, activebackground=BG,
            selectcolor=SURFACE, relief="flat", cursor="hand2",
            command=lambda: _commit(
                "spell_check_mode", sc_mode_var.get(),
                f"Spell-check mode set to {sc_mode_var.get()}."),
        ).pack(anchor="w")
    tk.Label(sc_frame,
             text="Off disables sentence correction; both Assistant hotkeys still work",
             font=("Segoe UI", 8), bg=BG, fg=MUTED).pack(anchor="w", pady=(2, 0))

    # ── Diacritics-only auto toggle ───────────────────────────────────────────
    diacr_var = tk.BooleanVar(master=win, value=cfg.get("diacritics_only_auto", False))
    lbl_row("Diacritics only", 2, frm_behaviour)
    diacr_frame = tk.Frame(frm_behaviour, bg=BG)
    diacr_frame.grid(row=2, column=1, sticky="w", pady=7)
    _make_toggle(diacr_frame, diacr_var,
                 "Auto spell-check restores diacritics only (no grammar fixes)",
                 command=lambda: _commit(
                     "diacritics_only_auto", bool(diacr_var.get()),
                     f"Diacritics-only mode {'enabled' if diacr_var.get() else 'disabled'}."),
                 ).pack(anchor="w")
    tk.Label(diacr_frame,
             text="Useful when typing BHS (Bosnian/Croatian/Serbian) without diacritics",
             font=("Segoe UI", 8), bg=BG, fg=MUTED
             ).pack(anchor="w", padx=(_TGL_INDENT, 0))

    # ── Advanced disclosure section ───────────────────────────────────────────
    # Trigger chars / Min words / Idle reset are implementation-level knobs that
    # most users will never change.  They live behind a disclosure triangle so
    # they don't clutter the main form, but are still fully accessible.

    # Trigger chars: stored as a plain string like ".!?"; each checkbox toggles
    # one character in or out.  The six options cover all realistic use cases;
    # the underlying format is unchanged so old configs load without migration.
    _TRIGGER_OPTIONS = [
        (".",  "Period  ."),
        ("!",  "Exclamation  !"),
        ("?",  "Question  ?"),
        (",",  "Comma  ,"),
        (";",  "Semicolon  ;"),
        (":",  "Colon  :"),
    ]
    _trig_current = list(cfg.get("trigger_chars", ".!?"))
    _trig_vars: dict[str, tk.BooleanVar] = {}
    for _ch, _ in _TRIGGER_OPTIONS:
        v = tk.BooleanVar(master=win, value=_ch in _trig_current)
        _trig_vars[_ch] = v

    def _trig_string() -> str:
        """Rebuild the trigger_chars string from the current checkbox state."""
        return "".join(ch for ch, _ in _TRIGGER_OPTIONS if _trig_vars[ch].get())

    min_var  = tk.IntVar(master=win, value=cfg.get("min_word_count", 2))
    idle_var = tk.DoubleVar(master=win, value=cfg.get("idle_reset_sec", 30.0))

    _adv_open = [False]

    adv_hdr = tk.Frame(frm_behaviour, bg=BG)
    adv_hdr.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

    adv_arrow_lbl = tk.Label(
        adv_hdr, text="▸  Advanced",
        font=("Segoe UI", 9, "bold"), bg=BG, fg=MUTED, cursor="hand2",
    )
    adv_arrow_lbl.pack(side="left")
    tk.Label(
        adv_hdr,
        text="  trigger chars · min words · idle reset",
        font=("Segoe UI", 8), bg=BG, fg=MUTED,
    ).pack(side="left")

    # Body frame: built once, toggled in/out of the grid on click.
    adv_body = tk.Frame(frm_behaviour, bg=BG)
    adv_body.columnconfigure(1, weight=1)

    def _adv_lbl(text, row):
        tk.Label(adv_body, text=text, font=FONT_B, bg=BG, fg=MUTED,
                 anchor="e", width=20).grid(row=row, column=0, sticky="e",
                                            padx=(0, 10), pady=5)

    _adv_lbl("Trigger on", 0)
    _trig_frame = tk.Frame(adv_body, bg=BG)
    _trig_frame.grid(row=0, column=1, sticky="w", pady=5)
    for _col, (_ch, _label) in enumerate(_TRIGGER_OPTIONS):
        tk.Checkbutton(
            _trig_frame, text=_label, variable=_trig_vars[_ch],
            font=FONT_B, bg=BG, fg=TEXT,
            activebackground=BG, activeforeground=ACCENT2,
            selectcolor=SURFACE,
            relief="flat", cursor="hand2",
            command=lambda: _commit(
                "trigger_chars", _trig_string(), "Trigger characters updated."),
        ).grid(row=0, column=_col, padx=(0, 14), sticky="w")
    tk.Label(_trig_frame, text="Characters that end a sentence and trigger spell-check",
             font=("Segoe UI", 8), bg=BG, fg=MUTED).grid(
                 row=1, column=0, columnspan=len(_TRIGGER_OPTIONS), sticky="w", pady=(2, 0))

    def _commit_min(_event=None):
        _commit_spin("min_word_count", min_var, 2, int,
                     "Minimum word count updated.")

    def _commit_idle(_event=None):
        _commit_spin("idle_reset_sec", idle_var, 30.0, float,
                     "Idle reset updated.")

    _adv_lbl("Min words", 1)
    min_sb = tk.Spinbox(adv_body, from_=1, to=10, textvariable=min_var, width=5,
                        font=FONT_B, bg=SURFACE, fg=TEXT,
                        relief="flat", command=_commit_min)
    min_sb.grid(row=1, column=1, sticky="w", pady=5)
    min_sb.bind("<FocusOut>", _commit_min)
    min_sb.bind("<Return>", _commit_min)

    _adv_lbl("Idle reset (s)", 2)
    idle_sb = tk.Spinbox(adv_body, from_=0.0, to=120.0, increment=5.0,
                         textvariable=idle_var, width=5,
                         font=FONT_B, bg=SURFACE, fg=TEXT, relief="flat",
                         command=_commit_idle)
    idle_sb.grid(row=2, column=1, sticky="w", pady=5)
    idle_sb.bind("<FocusOut>", _commit_idle)
    idle_sb.bind("<Return>", _commit_idle)
    _add_tooltip(idle_sb,
        "If you stop typing for this many seconds (e.g. while switching apps),\n"
        "the sentence buffer is automatically cleared before the next keystroke.\n"
        "This prevents text from a previous app or context from being mixed into\n"
        "the sentence that is about to be spell-checked.\n\n"
        "Default: 30 seconds.  A low value will discard your sentence when you\n"
        "pause to backspace-edit mid-sentence (the next '.' then won't correct).\n"
        "Set to 0 to disable the reset entirely."
    )

    def _toggle_advanced(_event=None):
        if _adv_open[0]:
            adv_body.grid_remove()
            adv_arrow_lbl.config(text="▸  Advanced")
            _adv_open[0] = False
        else:
            adv_body.grid(row=6, column=0, columnspan=2, sticky="ew",
                          padx=(22, 0), pady=(0, 4))
            adv_arrow_lbl.config(text="▾  Advanced")
            _adv_open[0] = True

    adv_arrow_lbl.bind("<Button-1>", _toggle_advanced)
    for _w in adv_hdr.winfo_children():
        _w.bind("<Button-1>", _toggle_advanced)

    # ── publish shared state ──────────────────────────────────────────────────
    ui.lang_var, ui.sc_mode_var, ui.diacr_var = lang_var, sc_mode_var, diacr_var
    ui.trig_string = _trig_string
    ui.min_var, ui.idle_var = min_var, idle_var
