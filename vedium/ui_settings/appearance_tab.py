# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Appearance tab: colour theme picker and text size."""

import tkinter as tk
import tkinter.ttk as ttk

from .. import constants as c
from ..config import save_cfg
from ..constants import BG, SURF2, TEXT, MUTED, ACCENT, FONT_B, SCHEMES


def _build_appearance_tab(ui):
    """Build the Appearance tab: colour theme picker and text size."""
    win, cfg = ui.win, ui.cfg
    frm_appearance = ui.frm_appearance
    _bind_scroll = ui.bind_scroll

    active_scheme_var = tk.StringVar(master=win,
                                     value=cfg.get("colour_scheme", "Parchment"))

    tk.Label(frm_appearance,
             text="Choose the colour theme used by Settings and suggestion popups.",
             font=("Segoe UI", 8), bg=BG, fg=MUTED, justify="left",
             ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

    def _draw_swatches(parent, scheme_name):
        s = SCHEMES.get(scheme_name, SCHEMES["Parchment"])
        for key in ("BG", "SURFACE", "SURF2", "ACCENT", "ACCENT2",
                    "SUCCESS", "WARN", "ERR"):
            tk.Frame(parent, bg=s[key], width=22, height=22).pack(
                side="left", padx=(0, 5))

    theme_row_frame = tk.Frame(frm_appearance, bg=BG, padx=10, pady=10,
                               highlightthickness=1, highlightbackground=SURF2)
    theme_row_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
    theme_row_frame.columnconfigure(1, weight=1)

    tk.Label(theme_row_frame, text="Theme", font=FONT_B,
             bg=BG, fg=TEXT, width=8, anchor="w").grid(
                 row=0, column=0, sticky="w", padx=(0, 10))

    theme_dd = ttk.Combobox(theme_row_frame, textvariable=active_scheme_var,
                            values=list(SCHEMES.keys()), state="readonly",
                            font=FONT_B, width=18)
    theme_dd.grid(row=0, column=1, sticky="w")
    _bind_scroll(theme_dd)

    active_theme_lbl = tk.Label(theme_row_frame, text="Active", font=FONT_B,
                                bg=ACCENT, fg="white", padx=10, pady=2)
    active_theme_lbl.grid(row=0, column=2, padx=(10, 0))

    swatch_bar = tk.Frame(theme_row_frame, bg=BG)
    swatch_bar.grid(row=1, column=0, columnspan=3, sticky="w", pady=(9, 0))
    _draw_swatches(swatch_bar, active_scheme_var.get())

    def _apply_theme_choice(_event=None):
        name = active_scheme_var.get()
        cfg["colour_scheme"] = name
        cfg.pop("colour_scheme_slots", None)
        c.apply_scheme(name)
        save_cfg(cfg)
        ui.reopen()

    theme_dd.bind("<<ComboboxSelected>>", _apply_theme_choice)

    # Text Size Option
    FONT_SIZE_OPTIONS = ["Small (-1)", "Normal (0)", "Medium (+2)", "Large (+4)", "Extra Large (+6)"]
    FONT_SIZE_MAP = {
        "Small (-1)": -1,
        "Normal (0)": 0,
        "Medium (+2)": 2,
        "Large (+4)": 4,
        "Extra Large (+6)": 6
    }
    FONT_SIZE_REV_MAP = {v: k for k, v in FONT_SIZE_MAP.items()}

    current_offset = cfg.get("font_size_offset", 0)
    font_size_var = tk.StringVar(master=win, value=FONT_SIZE_REV_MAP.get(current_offset, "Normal (0)"))

    font_row_frame = tk.Frame(frm_appearance, bg=BG, padx=10, pady=10,
                              highlightthickness=1, highlightbackground=SURF2)
    font_row_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 8))
    font_row_frame.columnconfigure(1, weight=1)

    tk.Label(font_row_frame, text="Text Size", font=FONT_B,
             bg=BG, fg=TEXT, width=8, anchor="w").grid(row=0, column=0, sticky="w", padx=(0, 10))

    font_dd = ttk.Combobox(font_row_frame, textvariable=font_size_var,
                            values=list(FONT_SIZE_MAP.keys()), state="readonly",
                            font=FONT_B, width=14)
    font_dd.grid(row=0, column=1, sticky="w")
    _bind_scroll(font_dd)

    def _apply_font_choice(_event=None):
        new_offset = FONT_SIZE_MAP.get(font_size_var.get(), 0)
        if cfg.get("font_size_offset", 0) == new_offset:
            return
        cfg["font_size_offset"] = new_offset
        save_cfg(cfg)
        c.apply_font_size(new_offset)
        # Rebuild the window so every widget picks up the new base size —
        # same flow the theme picker uses.
        ui.reopen()

    font_dd.bind("<<ComboboxSelected>>", _apply_font_choice)

    # ── publish shared state ──────────────────────────────────────────────────
    ui.active_scheme_var, ui.font_size_var = active_scheme_var, font_size_var
