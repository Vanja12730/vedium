"""Assistant tab: hotkeys, default modes, and the mode-prompt manager."""

import tkinter as tk
import tkinter.ttk as ttk
from tkinter import messagebox

from .. import diagnostics
from ..config import save_cfg, _merge_custom_modes
from ..constants import (
    BG, SURFACE, SURF2, TEXT, MUTED, ACCENT, ACCENT2,
    WARN, ERR, SUCCESS, FONT_B,
    _BUILTIN_MODES, _FACTORY_PROMPTS, _is_answer_mode,
    _OS_RESERVED_HOTKEYS, _OS_RESERVED_HOTKEY_REASONS,
)
from ..ui_widgets import _add_tooltip, lbl_row


def _build_rephrase_tab(ui):
    """Build the Assistant tab: action hotkeys, default modes,
    creativity slider, and the mode-prompt manager (add/edit/reset/delete,
    per-mode triggers and hotkeys)."""
    win, cfg = ui.win, ui.cfg
    on_cfg_saved = ui.on_cfg_saved
    frm_rephrase = ui.frm_rephrase
    _bind_scroll = ui.bind_scroll

    # ── Assistant action hotkeys ──────────────────────────────────────────────
    understand_hotkey_var = tk.StringVar(
        master=win, value=cfg.get("understand_hotkey", "f8"))
    writing_hotkey_var = tk.StringVar(
        master=win, value=cfg.get("writing_hotkey", "f9"))

    def _hotkey_display(s: str) -> str:
        """Turn a stored hotkey string into a readable label (e.g. 'f8' → 'F8')."""
        if not s:
            return "—"
        if len(s) == 1:
            return s.upper()          # 'a' → 'A', '/' → '/'
        return s[0].upper() + s[1:]   # 'f8' → 'F8', 'insert' → 'Insert'

    lbl_row("Hotkeys", 0, frm_rephrase)
    hf = tk.Frame(frm_rephrase, bg=BG)
    hf.grid(row=0, column=1, sticky="w", pady=7)
    hotkey_vars = {
        "understand_hotkey": understand_hotkey_var,
        "writing_hotkey": writing_hotkey_var,
    }
    active_capture = [None]

    def _hotkey_control(label: str, hint: str, variable, cfg_key: str):
        panel = tk.Frame(hf, bg=BG)
        panel.pack(anchor="w", pady=(0, 8))
        tk.Label(panel, text=label, font=("Segoe UI", 8, "bold"),
                 bg=BG, fg=TEXT).pack(anchor="w")
        controls = tk.Frame(panel, bg=BG)
        controls.pack(anchor="w", pady=(2, 0))
        display_var = tk.StringVar(master=win, value=_hotkey_display(variable.get()))
        badge = tk.Label(controls, textvariable=display_var,
                         font=("Segoe UI", 9, "bold"), bg=SURF2, fg=TEXT,
                         relief="groove", anchor="center", padx=10, pady=4)
        badge.pack(side="left", padx=(0, 10))
        error_var = tk.StringVar(master=win, value="")
        error_lbl = tk.Label(panel, textvariable=error_var,
                             font=("Segoe UI", 8), bg=BG, fg=ERR)
        capturing = [False]

        def hide_error():
            error_lbl.pack_forget()
            error_var.set("")

        def show_error(message: str):
            error_var.set(message)
            error_lbl.pack(anchor="w", pady=(3, 0))
            win.after(2200, hide_error)

        def cancel_capture():
            capturing[0] = False
            if active_capture[0] is cancel_capture:
                active_capture[0] = None
            display_var.set(_hotkey_display(variable.get()))
            badge.config(bg=SURF2, fg=TEXT, relief="groove")
            capture_btn.config(text="Change…", bg=SURFACE)
            win.unbind("<KeyPress>")
            hide_error()

        def start_capture():
            if active_capture[0] and active_capture[0] is not cancel_capture:
                active_capture[0]()
            active_capture[0] = cancel_capture
            capturing[0] = True
            display_var.set("Press a key…")
            badge.config(bg=ACCENT2, fg="white", relief="flat")
            capture_btn.config(text="Cancel", bg=WARN)
            win.bind("<KeyPress>", capture_key)
            win.focus_set()

        def capture_key(event):
            if not capturing[0]:
                return
            keysym = event.keysym
            if keysym == "Escape":
                cancel_capture()
                return "break"
            if keysym in ("Shift_L", "Shift_R", "Control_L", "Control_R",
                          "Alt_L", "Alt_R", "Super_L", "Super_R",
                          "Caps_Lock", "Num_Lock", "Scroll_Lock"):
                show_error("Modifier keys fire too often — use F1–F12.")
                return "break"
            if keysym in ("Return", "Tab", "BackSpace", "Delete", "space"):
                show_error("This key types text — it can't be used as a hotkey.")
                return "break"
            if event.char and event.char.isprintable() and len(event.char) == 1:
                show_error("Letter keys also type into your app — use F1–F12 or Insert, Pause…")
                return "break"
            if keysym.lower() in _OS_RESERVED_HOTKEYS:
                show_error(_OS_RESERVED_HOTKEY_REASONS.get(
                    keysym.lower(), f"{keysym} is reserved by the OS."))
                return "break"

            stored = keysym.lower()
            for other_key, other_var in hotkey_vars.items():
                if other_key != cfg_key and stored == other_var.get().strip().lower():
                    show_error("Understand and Improve Writing need different keys.")
                    return "break"
            mode_owner = next((name for name, value in cfg.get("mode_hotkeys", {}).items()
                               if stored == str(value).strip().lower()), "")
            if mode_owner:
                show_error(f"That key is already assigned to {mode_owner}.")
                return "break"

            variable.set(stored)
            cfg[cfg_key] = stored
            save_cfg(cfg)
            on_cfg_saved(cfg)
            diagnostics.record("config", "info", f"{label} set to {_hotkey_display(stored)}.")
            display_var.set(_hotkey_display(stored))
            badge.config(bg=SURF2, fg=SUCCESS, relief="groove")
            capturing[0] = False
            active_capture[0] = None
            capture_btn.config(text="Change…", bg=SURFACE)
            win.unbind("<KeyPress>")
            hide_error()
            win.after(900, lambda: badge.config(fg=TEXT))
            return "break"

        capture_btn = tk.Button(
            controls, text="Change…", font=FONT_B, bg=SURFACE, fg=TEXT,
            relief="flat", padx=8, cursor="hand2",
            command=lambda: cancel_capture() if capturing[0] else start_capture(),
        )
        capture_btn.pack(side="left")
        tk.Label(panel, text=hint, font=("Segoe UI", 8), bg=BG, fg=MUTED).pack(
            anchor="w", pady=(3, 0))

    _hotkey_control(
        "Understand selected text",
        "Shows an answer and never replaces the source text.",
        understand_hotkey_var, "understand_hotkey",
    )
    _hotkey_control(
        "Improve Writing",
        "Improves selected text, or the current/last text you typed.",
        writing_hotkey_var, "writing_hotkey",
    )

    # ── Default modes ─────────────────────────────────────────────────────────
    reading_rephrase_var = tk.StringVar(
        master=win, value=cfg.get("rephrase_reading_mode", "What's This About?"))
    writing_rephrase_var = tk.StringVar(
        master=win, value=cfg.get("rephrase_writing_mode", "Polish My Writing"))
    lbl_row("Default modes", 1, frm_rephrase)
    rf = tk.Frame(frm_rephrase, bg=BG)
    rf.grid(row=1, column=1, sticky="w", pady=7)

    def _reading_rephrase_modes() -> list[str]:
        return [mode for mode in _BUILTIN_MODES if _is_answer_mode(mode)]

    def _writing_rephrase_modes(custom_modes: dict | None = None) -> list[str]:
        custom_modes = custom_modes if custom_modes is not None else cfg.get("custom_rephrase_modes", {})
        custom_names = [name for name in custom_modes if name not in _BUILTIN_MODES]
        return [mode for mode in _BUILTIN_MODES if not _is_answer_mode(mode)] + custom_names

    def _mode_picker(parent, label, variable, values):
        panel = tk.Frame(parent, bg=BG)
        panel.pack(side="left", padx=(0, 14))
        tk.Label(panel, text=label, font=("Segoe UI", 8, "bold"), bg=BG, fg=MUTED).pack(anchor="w")
        cb = ttk.Combobox(panel, textvariable=variable, values=values,
                          state="readonly", font=FONT_B, width=22)
        cb.pack(anchor="w", pady=(2, 0))
        _bind_scroll(cb)
        return cb

    reading_cb = _mode_picker(rf, "Reading (selected text)", reading_rephrase_var,
                              _reading_rephrase_modes())
    writing_cb = _mode_picker(rf, "Writing (typed text)", writing_rephrase_var,
                              _writing_rephrase_modes())

    _prev_reading_rephrase = [reading_rephrase_var.get()]
    _prev_writing_rephrase = [writing_rephrase_var.get()]

    def _on_rephrase_pick(variable, previous, valid_modes, cfg_key, label):
        val = variable.get()
        if val not in valid_modes():
            variable.set(previous[0])
            return
        previous[0] = val
        cfg[cfg_key] = val
        save_cfg(cfg)
        on_cfg_saved(cfg)
        diagnostics.record("config", "info", f"Default {label.lower()} rephrase mode set to {val}.")

    reading_cb.bind("<<ComboboxSelected>>", lambda _event: _on_rephrase_pick(
        reading_rephrase_var, _prev_reading_rephrase, _reading_rephrase_modes,
        "rephrase_reading_mode", "Reading"))
    writing_cb.bind("<<ComboboxSelected>>", lambda _event: _on_rephrase_pick(
        writing_rephrase_var, _prev_writing_rephrase, _writing_rephrase_modes,
        "rephrase_writing_mode", "Writing"))

    # ── Rephrase creativity slider ────────────────────────────────────────────
    creativity_var = tk.IntVar(master=win,
                               value=round(cfg.get("rephrase_creativity", 0.35) * 100))
    lbl_row("Creativity", 2, frm_rephrase)
    cf = tk.Frame(frm_rephrase, bg=BG)
    cf.grid(row=2, column=1, sticky="w", pady=7)
    tk.Label(cf, text="Conservative", font=FONT_B, bg=BG, fg=MUTED).pack(side="left")
    creativity_scale = tk.Scale(
        cf, from_=0, to=100, orient="horizontal", variable=creativity_var,
        showvalue=False, length=160, bg=BG, fg=TEXT,
        highlightthickness=0, troughcolor=SURF2, activebackground=ACCENT,
    )
    creativity_scale.pack(side="left", padx=6)
    tk.Label(cf, text="Creative", font=FONT_B, bg=BG, fg=MUTED).pack(side="left")

    # ── Rephrase mode prompts ─────────────────────────────────────────────────
    tk.Frame(frm_rephrase, bg=SURF2, height=1).grid(
        row=3, column=0, columnspan=2, sticky="ew", pady=(10, 6))
    tk.Label(frm_rephrase, text="✦  Modes", font=("Segoe UI", 9, "bold"),
             bg=BG, fg=ACCENT2).grid(row=4, column=0, columnspan=2, sticky="w",
                                     pady=(2, 2))
    tk.Label(frm_rephrase,
             text="Reading modes show an answer about the selected text — nothing is replaced.  "
                  "Writing modes rewrite the text and offer to replace it.  "
                  "Select any mode to view or edit its prompt — highlighted names have been customised.",
             font=("Segoe UI", 8), bg=BG, fg=MUTED, wraplength=480, justify="left",
             ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(0, 6))

    # Working copies — edited in-place and persisted on Save & Load Model.
    _custom: dict[str, str]            = dict(cfg.get("custom_rephrase_modes", {}))
    _builtin_overrides: dict[str, str] = dict(cfg.get("builtin_prompt_overrides", {}))
    _mode_triggers: dict[str, str]     = dict(cfg.get("mode_triggers", {}))
    _mode_hotkeys:  dict[str, str]     = dict(cfg.get("mode_hotkeys",  {}))

    cm_frame = tk.Frame(frm_rephrase, bg=BG)
    cm_frame.grid(row=6, column=0, columnspan=2, sticky="ew", padx=(0, 6), pady=(0, 14))
    cm_frame.columnconfigure(0, weight=1)

    # Two panels side by side: Reading (answer modes) and Writing (rewrite
    # modes, with translations as a separator-labelled block at the end).
    # exportselection=False so the two listboxes don't steal each other's
    # selection via the system selection — exclusivity is handled explicitly
    # in the select handlers instead.
    lb_frame = tk.Frame(cm_frame, bg=BG)
    lb_frame.grid(row=0, column=0, sticky="ew")
    lb_frame.columnconfigure(0, weight=2, uniform="modecols")
    lb_frame.columnconfigure(1, weight=3, uniform="modecols")

    read_panel = tk.Frame(lb_frame, bg=BG)
    read_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
    tk.Label(read_panel, text="📖  Reading — shows an answer",
             font=("Segoe UI", 8, "bold"), bg=BG, fg=ACCENT2).pack(anchor="w", pady=(0, 3))
    lb_read = tk.Listbox(
        read_panel, font=FONT_B, bg=SURFACE, fg=TEXT, selectbackground=ACCENT,
        selectforeground="white", relief="flat", height=7,
        activestyle="none", exportselection=False,
    )
    lb_read.pack(fill="both", expand=True)

    write_panel = tk.Frame(lb_frame, bg=BG)
    write_panel.grid(row=0, column=1, sticky="nsew")
    tk.Label(write_panel, text="✏  Writing — replaces your text",
             font=("Segoe UI", 8, "bold"), bg=BG, fg=ACCENT2).pack(anchor="w", pady=(0, 3))
    write_list_frame = tk.Frame(write_panel, bg=BG)
    write_list_frame.pack(fill="both", expand=True)
    write_list_frame.columnconfigure(0, weight=1)
    lb_scroll = tk.Scrollbar(write_list_frame, orient="vertical")
    lb_write = tk.Listbox(
        write_list_frame, font=FONT_B, bg=SURFACE, fg=TEXT, selectbackground=ACCENT,
        selectforeground="white", relief="flat", height=7,
        yscrollcommand=lb_scroll.set, activestyle="none", exportselection=False,
    )
    lb_scroll.config(command=lb_write.yview)
    lb_write.grid(row=0, column=0, sticky="nsew")
    lb_scroll.grid(row=0, column=1, sticky="ns")

    # Declare buttons early so _refresh_lb / _on_select can reference them.
    btn_bar    = tk.Frame(cm_frame, bg=BG)
    btn_bar.grid(row=1, column=0, sticky="w", pady=(6, 0))
    add_btn    = tk.Button(btn_bar, text="+ Add",       font=FONT_B, bg=ACCENT,  fg="white",
                           relief="flat", padx=10, pady=3, cursor="hand2")
    edit_btn   = tk.Button(btn_bar, text="Edit Prompt", font=FONT_B, bg=SURFACE, fg=MUTED,
                           relief="flat", padx=10, pady=3, cursor="hand2", state="disabled")
    reset_btn  = tk.Button(btn_bar, text="Reset",       font=FONT_B, bg=SURFACE, fg=MUTED,
                           relief="flat", padx=10, pady=3, cursor="hand2", state="disabled")
    delete_btn = tk.Button(btn_bar, text="Delete",      font=FONT_B, bg=SURFACE, fg=MUTED,
                           relief="flat", padx=10, pady=3, cursor="hand2", state="disabled")
    add_btn.pack(side="left")
    edit_btn.pack(side="left",  padx=(6, 0))
    reset_btn.pack(side="left", padx=(6, 0))
    delete_btn.pack(side="left",padx=(6, 0))

    def _lb_label(name: str) -> str:
        """Build the listbox display string, appending trigger and hotkey tags if set."""
        t  = _mode_triggers.get(name, "").strip()
        hk = _mode_hotkeys.get(name, "").strip()
        meta = ""
        if t:
            meta += t
        if hk:
            badge = hk[0].upper() + hk[1:]   # "f2" → "F2", "insert" → "Insert"
            meta += f"  [{badge}]" if meta else f"[{badge}]"
        return f"{name}  ·  {meta}" if meta else name

    def _selected_mode() -> str | None:
        """Return the mode name selected in either panel, or None.
        Separator rows ("── … ──") never count as a selection."""
        for box in (lb_read, lb_write):
            sel = box.curselection()
            if sel:
                label = box.get(sel[0])
                if label.startswith("──"):
                    return None
                return label.split("  ·  ")[0]  # strip trigger/hotkey tag
        return None

    def _refresh_lb():
        """Repopulate both mode panels and sync the rephrase combobox."""
        sel_name = _selected_mode()

        reading      = [m for m in _BUILTIN_MODES if _is_answer_mode(m)]
        writing      = [m for m in _BUILTIN_MODES
                        if not _is_answer_mode(m) and not m.startswith("Translate ")]
        translations = [m for m in _BUILTIN_MODES if m.startswith("Translate ")]
        custom_names = [n for n in _custom if n not in _BUILTIN_MODES]

        lb_read.delete(0, "end")
        for i, name in enumerate(reading):
            lb_read.insert("end", _lb_label(name))
            lb_read.itemconfig(i, fg=ACCENT2 if name in _builtin_overrides else MUTED)

        lb_write.delete(0, "end")
        idx = 0
        for name in writing:
            lb_write.insert("end", _lb_label(name))
            lb_write.itemconfig(idx, fg=ACCENT2 if name in _builtin_overrides else MUTED)
            idx += 1
        for name in custom_names:
            lb_write.insert("end", _lb_label(name))
            lb_write.itemconfig(idx, fg=TEXT)
            idx += 1
        if translations:
            lb_write.insert("end", "──  Translation  ──")
            lb_write.itemconfig(idx, fg=MUTED)
            idx += 1
            for name in translations:
                lb_write.insert("end", _lb_label(name))
                lb_write.itemconfig(idx, fg=ACCENT2 if name in _builtin_overrides else MUTED)
                idx += 1

        # Restore selection by mode name in whichever panel now holds it.
        if sel_name:
            for box in (lb_read, lb_write):
                labels = [box.get(i).split("  ·  ")[0] for i in range(box.size())]
                if sel_name in labels:
                    pos = labels.index(sel_name)
                    box.selection_set(pos)
                    box.see(pos)
                    break

        _merge_custom_modes({**cfg, "custom_rephrase_modes": _custom,
                             "builtin_prompt_overrides": _builtin_overrides})
        reading_cb["values"] = _reading_rephrase_modes()
        writing_cb["values"] = _writing_rephrase_modes(_custom)
        if reading_rephrase_var.get() not in _reading_rephrase_modes():
            reading_rephrase_var.set("What's This About?")
            cfg["rephrase_reading_mode"] = "What's This About?"
        if writing_rephrase_var.get() not in _writing_rephrase_modes(_custom):
            writing_rephrase_var.set("Polish My Writing")
            cfg["rephrase_writing_mode"] = "Polish My Writing"
        _on_select()

    def _on_select(event=None):
        name = _selected_mode()
        if name is None:
            edit_btn.config(state="disabled", fg=MUTED)
            reset_btn.config(state="disabled", fg=MUTED)
            delete_btn.config(state="disabled", fg=MUTED)
            return
        is_builtin = name in _BUILTIN_MODES
        overridden = name in _builtin_overrides
        edit_btn.config(state="normal", fg=TEXT)
        reset_btn.config(
            state="normal"    if (is_builtin and overridden) else "disabled",
            fg=ERR            if (is_builtin and overridden) else MUTED,
        )
        delete_btn.config(
            state="normal"    if not is_builtin else "disabled",
            fg=ERR            if not is_builtin else MUTED,
        )

    def _on_select_read(event=None):
        if lb_read.curselection():
            lb_write.selection_clear(0, "end")
        _on_select()

    def _on_select_write(event=None):
        sel = lb_write.curselection()
        if sel:
            lb_read.selection_clear(0, "end")
            if lb_write.get(sel[0]).startswith("──"):  # separator row
                lb_write.selection_clear(0, "end")
        _on_select()

    lb_read.bind("<<ListboxSelect>>",  _on_select_read)
    lb_write.bind("<<ListboxSelect>>", _on_select_write)
    lb_read.bind("<Double-Button-1>",  lambda _e: _edit_mode())
    lb_write.bind("<Double-Button-1>", lambda _e: _edit_mode())
    _refresh_lb()

    def _sync_mode_cfg(message: str):
        cfg["custom_rephrase_modes"]    = dict(_custom)
        cfg["builtin_prompt_overrides"] = dict(_builtin_overrides)
        cfg["mode_triggers"]            = dict(_mode_triggers)
        cfg["mode_hotkeys"]             = dict(_mode_hotkeys)
        cfg["rephrase_reading_mode"]    = reading_rephrase_var.get()
        cfg["rephrase_writing_mode"]    = writing_rephrase_var.get()
        _merge_custom_modes(cfg)
        save_cfg(cfg)
        on_cfg_saved(cfg)
        diagnostics.record("config", "info", message)

    def _open_mode_dialog(edit_name: str | None = None, is_builtin: bool = False):
        dlg = tk.Toplevel(win)
        dlg.title(
            f"Edit built-in prompt — {edit_name}" if is_builtin
            else ("Edit custom mode" if edit_name else "Add custom mode")
        )
        dlg.configure(bg=BG)
        dlg.geometry("540x520")
        dlg.resizable(True, True)
        dlg.grab_set()
        dlg.transient(win)

        tk.Label(dlg, text="Mode name", font=("Segoe UI", 9, "bold"), bg=BG, fg=TEXT).pack(
            anchor="w", padx=16, pady=(16, 2))
        name_var   = tk.StringVar(master=dlg, value=edit_name or "")
        name_entry = tk.Entry(dlg, textvariable=name_var, font=FONT_B,
                              bg=SURFACE, fg=TEXT, insertbackground=ACCENT2, relief="flat")
        name_entry.pack(fill="x", padx=16, ipady=4)
        if edit_name:
            name_entry.config(state="disabled")

        if is_builtin:
            tk.Label(dlg,
                     text="This is a built-in mode.  Saving will override the factory prompt; "
                          "use Reset in the list to restore it.",
                     font=("Segoe UI", 8), bg=BG, fg=MUTED, wraplength=490, justify="left",
                     ).pack(anchor="w", padx=16, pady=(3, 0))

        name_err = tk.Label(dlg, text="", font=FONT_B, bg=BG, fg=ERR)
        name_err.pack(anchor="w", padx=16, pady=(2, 0))

        # Trigger phrase field
        trig_row = tk.Frame(dlg, bg=BG)
        trig_row.pack(fill="x", padx=16, pady=(8, 0))
        tk.Label(trig_row, text="Trigger phrase", font=("Segoe UI", 9, "bold"),
                 bg=BG, fg=TEXT).pack(anchor="w")
        tk.Label(trig_row,
                 text="Type this prefix before your text, then press the matching action hotkey.  "
                      "Leave blank to use the mode only from the Settings dropdown.",
                 font=("Segoe UI", 8), bg=BG, fg=MUTED, wraplength=490, justify="left",
                 ).pack(anchor="w")
        trigger_var = tk.StringVar(master=dlg,
                                   value=_mode_triggers.get(edit_name or "", ""))
        tk.Entry(dlg, textvariable=trigger_var, font=FONT_B,
                 bg=SURFACE, fg=TEXT, insertbackground=ACCENT2,
                 relief="flat").pack(fill="x", padx=16, ipady=4, pady=(4, 0))

        # Per-mode hotkey capture field
        hk_outer = tk.Frame(dlg, bg=BG)
        hk_outer.pack(fill="x", padx=16, pady=(10, 0))
        tk.Label(hk_outer, text="Dedicated hotkey", font=("Segoe UI", 9, "bold"),
                 bg=BG, fg=TEXT).pack(anchor="w")
        tk.Label(hk_outer,
                 text="Press this key anywhere to use this mode directly.  "
                      "Leave unset to use Understand or Improve Writing instead.",
                 font=("Segoe UI", 8), bg=BG, fg=MUTED, wraplength=490, justify="left",
                 ).pack(anchor="w")

        _stored_hk    = [_mode_hotkeys.get(edit_name or "", "")]
        _hk_cap       = [False]
        _hk_display   = tk.StringVar(master=dlg,
                                     value=_hotkey_display(_stored_hk[0]) if _stored_hk[0] else "—")

        hk_row = tk.Frame(hk_outer, bg=BG)
        hk_row.pack(anchor="w", pady=(4, 0))

        hk_badge = tk.Label(hk_row, textvariable=_hk_display,
                            font=("Segoe UI", 9, "bold"),
                            bg=SURF2, fg=TEXT, relief="groove",
                            anchor="center", padx=10, pady=4)
        hk_badge.pack(side="left", padx=(0, 8))

        _hk_err_var = tk.StringVar(master=dlg, value="")
        _hk_err_lbl = tk.Label(hk_outer, textvariable=_hk_err_var,
                                font=("Segoe UI", 8), bg=BG, fg=ERR)

        def _show_hk_err(msg: str):
            _hk_err_var.set(msg)
            _hk_err_lbl.pack(anchor="w", pady=(2, 0))
            dlg.after(2200, _hide_hk_err)

        def _hide_hk_err():
            _hk_err_lbl.pack_forget()
            _hk_err_var.set("")

        def _start_hk_capture():
            _hk_cap[0] = True
            _hk_display.set("Press a key…")
            hk_badge.config(bg=ACCENT2, fg="white", relief="flat")
            hk_change_btn.config(text="Cancel", bg=WARN)
            dlg.bind("<KeyPress>", _capture_hk)
            dlg.focus_set()

        def _cancel_hk_capture():
            _hk_cap[0] = False
            _hk_display.set(_hotkey_display(_stored_hk[0]) if _stored_hk[0] else "—")
            hk_badge.config(bg=SURF2, fg=TEXT, relief="groove")
            hk_change_btn.config(text="Change…", bg=SURFACE)
            dlg.unbind("<KeyPress>")
            _hide_hk_err()

        def _capture_hk(event):
            if not _hk_cap[0]:
                return
            keysym = event.keysym
            if keysym == "Escape":
                _cancel_hk_capture()
                return "break"
            if keysym in ("Shift_L", "Shift_R", "Control_L", "Control_R",
                          "Alt_L", "Alt_R", "Super_L", "Super_R",
                          "Caps_Lock", "Num_Lock", "Scroll_Lock"):
                _show_hk_err("Modifier keys fire too often — use F1–F12.")
                return "break"
            if keysym in ("Return", "Tab", "BackSpace", "Delete", "space"):
                _show_hk_err("This key types text — it can't be used as a hotkey.")
                return "break"
            if event.char and event.char.isprintable() and len(event.char) == 1:
                _show_hk_err("Letter keys also type into your app — use F1–F12 or Insert, Pause…")
                return "break"
            # Block OS-reserved keys that interfere with the clipboard copy probe.
            if keysym.lower() in _OS_RESERVED_HOTKEYS:
                reason = _OS_RESERVED_HOTKEY_REASONS.get(keysym.lower(), f"{keysym} is reserved by the OS.")
                _show_hk_err(reason)
                return "break"
            stored = keysym.lower()
            if stored in {
                    understand_hotkey_var.get().strip().lower(),
                    writing_hotkey_var.get().strip().lower()}:
                _show_hk_err("That key is already assigned to an Assistant action.")
                return "break"
            _stored_hk[0] = stored
            _hk_display.set(_hotkey_display(stored))
            hk_badge.config(bg=SURF2, fg=SUCCESS, relief="groove")
            _hk_cap[0] = False
            hk_change_btn.config(text="Change…", bg=SURFACE)
            dlg.unbind("<KeyPress>")
            _hide_hk_err()
            dlg.after(900, lambda: hk_badge.config(fg=TEXT))
            return "break"

        def _clear_hk():
            _stored_hk[0] = ""
            _hk_display.set("—")
            hk_badge.config(bg=SURF2, fg=MUTED, relief="groove")
            if _hk_cap[0]:
                _cancel_hk_capture()

        hk_change_btn = tk.Button(hk_row, text="Change…" if _stored_hk[0] else "Set…",
                                  font=FONT_B, bg=SURFACE, fg=TEXT, relief="flat",
                                  padx=8, cursor="hand2",
                                  command=lambda: _cancel_hk_capture()
                                  if _hk_cap[0] else _start_hk_capture())
        hk_change_btn.pack(side="left", padx=(0, 6))
        tk.Button(hk_row, text="Clear", font=FONT_B, bg=SURFACE, fg=MUTED,
                  relief="flat", padx=8, cursor="hand2",
                  command=_clear_hk).pack(side="left")

        tk.Label(dlg, text="Prompt instructions", font=("Segoe UI", 9, "bold"), bg=BG, fg=TEXT).pack(
            anchor="w", padx=16, pady=(10, 2))

        btn_row = tk.Frame(dlg, bg=BG)
        btn_row.pack(fill="x", padx=16, pady=(8, 16), side="bottom")

        prompt_frame = tk.Frame(dlg, bg=BG)
        prompt_frame.pack(fill="both", expand=True, padx=16)
        prompt_frame.columnconfigure(0, weight=1)
        prompt_frame.rowconfigure(0, weight=1)
        prompt_scroll = tk.Scrollbar(prompt_frame, orient="vertical")
        prompt_text   = tk.Text(
            prompt_frame, font=FONT_B, bg=SURFACE, fg=TEXT,
            insertbackground=ACCENT2, relief="flat", wrap="word",
            yscrollcommand=prompt_scroll.set,
        )
        prompt_scroll.config(command=prompt_text.yview)
        prompt_text.grid(row=0, column=0, sticky="nsew")
        prompt_scroll.grid(row=0, column=1, sticky="ns")

        if edit_name:
            if is_builtin:
                initial = _builtin_overrides.get(edit_name, _FACTORY_PROMPTS.get(edit_name, ""))
            else:
                initial = _custom.get(edit_name, "")
            prompt_text.insert("1.0", initial)

        def _save_mode():
            name    = name_var.get().strip()
            prompt  = prompt_text.get("1.0", "end").strip()
            trigger = trigger_var.get().strip()
            if not prompt:
                name_err.config(text="Prompt cannot be empty.")
                return
            if is_builtin:
                _builtin_overrides[name] = prompt
            else:
                if not name:
                    name_err.config(text="Name cannot be empty.")
                    return
                if name in _BUILTIN_MODES:
                    name_err.config(text="That name is reserved for a built-in mode.")
                    return
                if name != edit_name and name in _custom:
                    name_err.config(text="A mode with that name already exists.")
                    return
                if edit_name and edit_name != name:
                    del _custom[edit_name]
                    if edit_name in _mode_triggers:
                        del _mode_triggers[edit_name]  # migrate trigger to new name
                    if edit_name in _mode_hotkeys:
                        del _mode_hotkeys[edit_name]              # migrate hotkey to new name
                        cfg["mode_hotkeys"].pop(edit_name, None)  # mirror into live config
                _custom[name] = prompt
            # Persist trigger: set or clear
            if trigger:
                _mode_triggers[name] = trigger
            elif name in _mode_triggers:
                del _mode_triggers[name]
            # Persist per-mode hotkey: set or clear
            new_hk = _stored_hk[0].strip()
            if new_hk:
                # Remove this hotkey from every other mode that already uses it
                # so no two modes ever share the same key.
                for _other in [k for k, v in _mode_hotkeys.items()
                               if v == new_hk and k != name]:
                    del _mode_hotkeys[_other]
                    cfg["mode_hotkeys"].pop(_other, None)
                _mode_hotkeys[name]          = new_hk
                cfg["mode_hotkeys"][name]    = new_hk   # live config — takes effect immediately
            elif name in _mode_hotkeys:
                del _mode_hotkeys[name]
                cfg["mode_hotkeys"].pop(name, None)     # live config — takes effect immediately
            _refresh_lb()
            _sync_mode_cfg(f"Assistant mode saved: {name}.")
            dlg.destroy()

        tk.Button(btn_row, text="Save", font=("Segoe UI", 9, "bold"),
                  bg=ACCENT, fg="white", relief="flat", padx=14, pady=4,
                  cursor="hand2", command=_save_mode).pack(side="left")
        tk.Button(btn_row, text="Cancel", font=FONT_B,
                  bg=SURF2, fg=MUTED, relief="flat", padx=14, pady=4,
                  cursor="hand2", command=dlg.destroy).pack(side="left", padx=(8, 0))

        (prompt_text if edit_name else name_entry).focus_set()
        dlg.wait_window()

    def _add_mode():
        _open_mode_dialog(edit_name=None, is_builtin=False)

    def _edit_mode():
        name = _selected_mode()
        if name is None:
            return
        _open_mode_dialog(edit_name=name, is_builtin=(name in _BUILTIN_MODES))

    def _reset_mode():
        name = _selected_mode()
        if name is None:
            return
        if name not in _BUILTIN_MODES or name not in _builtin_overrides:
            return
        if not messagebox.askyesno(
            "Reset built-in prompt",
            f'Reset "{name}" to its factory default?\nYour custom prompt will be lost.',
            parent=win,
        ):
            return
        del _builtin_overrides[name]
        _refresh_lb()
        _sync_mode_cfg(f"Built-in rephrase mode reset: {name}.")

    def _delete_mode():
        name = _selected_mode()
        if name is None:
            return
        if name in _BUILTIN_MODES:
            return
        if not messagebox.askyesno(
            "Delete custom mode",
            f'Delete the custom mode "{name}"?\nThis cannot be undone.',
            parent=win,
        ):
            return
        del _custom[name]
        _mode_triggers.pop(name, None)
        _mode_hotkeys.pop(name, None)
        cfg["mode_hotkeys"].pop(name, None)   # mirror into live config
        # Flush all four mode dicts into cfg before writing to disk (same
        # consistency guarantee as _save_mode — see comment there).
        _refresh_lb()
        _sync_mode_cfg(f"Custom rephrase mode deleted: {name}.")

    add_btn.config(command=_add_mode)
    edit_btn.config(command=_edit_mode)
    reset_btn.config(command=_reset_mode)
    delete_btn.config(command=_delete_mode)

    _mode_tooltip = (
        "Highlighted names have been customised; muted names use the factory prompt.\n"
        "Modes with a trigger or hotkey show them after the · separator.\n\n"
        "Double-click or select + Edit Prompt to change a mode's prompt, trigger, or hotkey.\n"
        "Trigger: type the prefix before your text and press the matching action hotkey —\n"
        "the daemon will use that mode for just that one shot, then revert.\n"
        "Hotkey: press the assigned key directly to use that mode immediately.\n"
        "Built-in overrides can be undone with Reset.\n"
        "Custom modes can be added with + Add, or removed with Delete."
    )
    _add_tooltip(lb_read,
        "Reading modes answer a question about the selected text —\n"
        "the answer is shown in a card and NEVER replaces the text.\n"
        "They work on any selection, even a single word.\n\n" + _mode_tooltip
    )
    _add_tooltip(lb_write,
        "Writing modes rewrite the text and offer to replace it.\n"
        "Custom modes you add appear here.\n\n" + _mode_tooltip
    )

    # ── publish shared state ──────────────────────────────────────────────────
    ui.understand_hotkey_var = understand_hotkey_var
    ui.writing_hotkey_var = writing_hotkey_var
    ui.reading_rephrase_var, ui.writing_rephrase_var = reading_rephrase_var, writing_rephrase_var
    ui.creativity_var = creativity_var
    ui.custom, ui.builtin_overrides = _custom, _builtin_overrides
    ui.mode_triggers, ui.mode_hotkeys = _mode_triggers, _mode_hotkeys
