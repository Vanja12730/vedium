"""Settings window: shell, singleton, and the per-tab builders.

The window is assembled here; each tab lives in its own submodule and
receives a _SettingsUI namespace (``ui``) carrying the shared tk variables
and callbacks. ``_settings_win`` is rebound at runtime -- read it via the
module (or ``ui.current_win()``), never a from-import.
"""

import os
import sys
import tkinter as tk
import tkinter.ttk as ttk

from .. import constants as c
from ..constants import BG, SURFACE, SURF2, TEXT, MUTED, ACCENT2, FONT_B, FONT_SIZE_OFFSET
from ..config import save_cfg
from .. import ui_thread
from ..ui_thread import _ui_call, _update_sv_ttk_theme

from .status_tab import _build_status_tab
from .model_tab import _build_model_tab
from .behaviour_tab import _build_behaviour_tab
from .setup_tab import _build_setup_tab
from .rephrase_tab import _build_rephrase_tab
from .appearance_tab import _build_appearance_tab
from .bottom_bar import _build_bottom_bar

_settings_win = None   # module-level singleton reference


def _open_settings(cfg: dict, on_cfg_saved, on_model_loaded):
    """Open (or focus) the Settings window. Safe to call from any thread."""
    _ui_call(_open_settings_ui, cfg, on_cfg_saved, on_model_loaded, wait=False)


class _SettingsUI:
    """Shared state passed between the per-tab builders of the Settings window.

    Each _build_*_tab function reads what it needs from this namespace and
    publishes the tk variables / dicts other tabs (and Save) depend on.
    Attribute lookup happens at call time, which preserves the late-binding
    behaviour the original single-closure implementation relied on.
    """


def _open_settings_ui(cfg: dict, on_cfg_saved, on_model_loaded):
    """Create/focus Settings. Must run on the UI thread."""
    global _settings_win
    _update_sv_ttk_theme()
    try:
        if _settings_win is not None and _settings_win.winfo_exists():
            _settings_win.deiconify()   # restore if minimised to taskbar
            _settings_win.lift()
            _settings_win.focus_force()
            return
    except tk.TclError:
        _settings_win = None

    win = tk.Toplevel(ui_thread._UI_ROOT)
    _settings_win = win
    win.title("Vedium — Settings")
    win.configure(bg=BG)
    try:
        win.iconbitmap(os.path.join(c._HERE, "app.ico"))
    except Exception:
        pass
    _screen_w  = win.winfo_screenwidth()
    _screen_h  = win.winfo_screenheight()
    _default_w = 997
    _default_h = 553
    # Restore the size and position the user last left the window at.
    # Clamp both axes so the window can never open off-screen (e.g. after
    # switching from a dual-monitor to a single-monitor setup).
    _saved_w = cfg.get("settings_window_width", _default_w)
    _win_w   = max(760, min(_saved_w, _screen_w - 80))
    _saved_h = cfg.get("settings_window_height", _default_h)
    # One-time migration: heights saved from the old single-scroll layout are
    # too tall for the tabbed layout.  Reset once, then respect user resizes.
    if _saved_h > 640 and not cfg.get("_settings_tabs_migrated"):
        _saved_h = _default_h
        cfg["_settings_tabs_migrated"] = True
    _win_h   = max(420, min(_saved_h, _screen_h - 80))
    _saved_x = cfg.get("settings_window_x", None)
    _saved_y = cfg.get("settings_window_y", None)
    if _saved_x is not None and _saved_y is not None:
        _win_x = max(0, min(int(_saved_x), _screen_w - _win_w))
        _win_y = max(0, min(int(_saved_y), _screen_h - _win_h))
        win.geometry(f"{_win_w}x{_win_h}+{_win_x}+{_win_y}")
    else:
        win.geometry(f"{_win_w}x{_win_h}")
    win.resizable(True, True)
    win.minsize(760, 420)

    def on_close():
        global _settings_win
        if sys.platform == "linux":
            win.unbind_all("<Button-4>")
            win.unbind_all("<Button-5>")
        else:
            win.unbind_all("<MouseWheel>")
        # Persist size and position so the window reopens exactly where it was.
        try:
            cfg["settings_window_width"]  = win.winfo_width()
            cfg["settings_window_height"] = win.winfo_height()
            cfg["settings_window_x"]      = win.winfo_x()
            cfg["settings_window_y"]      = win.winfo_y()
            save_cfg(cfg)
        except Exception:
            pass
        _settings_win = None
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", on_close)

    # Save button lives in bottom_bar, packed before the notebook so it's
    # always visible regardless of which tab is active or scroll position.
    bottom_bar = tk.Frame(win, bg=BG)
    bottom_bar.pack(side="bottom", fill="x", pady=(0, 14))

    # ── Notebook tabs ────────────────────────────────────────────────────────
    _nb_style = ttk.Style()
    _nb_style.configure("Settings.TNotebook",
                        background=BG, borderwidth=0, tabmargins=[0, 0, 0, 0])
    # Tabs are window chrome, not body text: keep them compact so all six fit
    # inside the window's minimum width (760px) without ttk clipping the labels
    # to single letters.  Trimming the padding (28 -> 12) and capping the font
    # growth keeps the whole tab row at ~740px even at a large font offset.
    _nb_style.configure("Settings.TNotebook.Tab",
                        background=SURFACE, foreground=TEXT,
                        font=("Georgia", 11 + min(FONT_SIZE_OFFSET, 2)),
                        padding=(12, 10))
    _nb_style.map("Settings.TNotebook.Tab",
                  background=[("selected", BG)],
                  foreground=[("selected", ACCENT2)])

    # clam (unlike the old sv_ttk theme) does not auto-colour ttk widgets to match
    # the scheme, so any Combobox that doesn't opt into a custom style renders in
    # clam's default grey.  Style the BASE TCombobox once here so every dropdown in
    # the window inherits the active scheme.  The map() is essential: clam paints
    # the readonly/disabled field from a separate state colour, so without it a
    # readonly combobox stays grey even when configured.
    _cb_base = ttk.Style()
    _cb_base.configure("TCombobox",
                       fieldbackground=SURFACE, background=SURFACE,
                       foreground=TEXT, arrowcolor=TEXT,
                       selectbackground=ACCENT2, selectforeground="white",
                       relief="flat", borderwidth=1)
    _cb_base.map("TCombobox",
                 fieldbackground=[("readonly", SURFACE), ("disabled", SURF2)],
                 foreground=[("disabled", MUTED)],
                 background=[("active", SURF2)],
                 arrowcolor=[("disabled", MUTED), ("active", ACCENT2)])
    # Dropdown list popup colours (apply to comboboxes created after this point).
    win.option_add("*TCombobox*Listbox.background", SURFACE)
    win.option_add("*TCombobox*Listbox.foreground", TEXT)
    win.option_add("*TCombobox*Listbox.selectBackground", ACCENT2)
    win.option_add("*TCombobox*Listbox.selectForeground", "white")
    win.option_add("*TCombobox*Listbox.font", FONT_B)

    notebook = ttk.Notebook(win, style="Settings.TNotebook")
    notebook.pack(fill="both", expand=True, padx=0, pady=0)

    # Per-tab scroll target; updated on <<NotebookTabChanged>>.
    _tab_canvases: list = []
    _active_canvas: list = [None]

    def _make_tab(title: str):
        """Create one notebook tab with a hover-scrollbar canvas. Returns (inner_frame, canvas)."""
        outer = tk.Frame(notebook, bg=BG)
        tab_icons = {
            "Setup": "\u2699",
            "Status": "\u23c1",
            "Model": "\u25a1",
            "Behaviour": "\u2637",
            "Assistant": "\u201c",
            "Appearance": "\u270e",
        }
        notebook.add(outer, text=f" {tab_icons.get(title, '')} {title} ")

        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        vsb    = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)

        # Overlay the scrollbar on the canvas right edge via place so it never
        # participates in pack layout.  This keeps the canvas width constant on
        # hover/leave, eliminating the jitter caused by pack/pack_forget reflow.
        def _show_vsb(e=None):
            if not vsb.winfo_ismapped():
                vsb.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")
        def _hide_vsb(e=None):
            try:
                if vsb.winfo_ismapped():
                    x, y   = win.winfo_pointerxy()
                    cx, cy = canvas.winfo_rootx(), canvas.winfo_rooty()
                    cw, ch = canvas.winfo_width(), canvas.winfo_height()
                    bx, by = vsb.winfo_rootx(), vsb.winfo_rooty()
                    bw, bh = vsb.winfo_width(), vsb.winfo_height()
                    if not (cx <= x <= cx + cw and cy <= y <= cy + ch) and \
                       not (bx <= x <= bx + bw and by <= y <= by + bh):
                        vsb.place_forget()
            except Exception:
                pass
        canvas.bind("<Enter>", _show_vsb)
        canvas.bind("<Leave>", lambda e: win.after(300, _hide_vsb))
        vsb.bind("<Leave>",    lambda e: win.after(300, _hide_vsb))

        inner = tk.Frame(canvas, bg=BG)
        inner.columnconfigure(1, weight=1)
        _wid = canvas.create_window((56, 28), window=inner, anchor="nw")

        def _update_scrollregion(_e=None, _c=canvas, _i=inner):
            _c.update_idletasks()
            content_h = _i.winfo_reqheight() + 56   # top + bottom margin
            visible_h = _c.winfo_height()
            # When content fits, make scrollregion == canvas so scrolling is impossible.
            # When content overflows, cap the region at the real content edge so the
            # user can never scroll blank space above or below the settings.
            _c.configure(scrollregion=(0, 0, _c.winfo_width(),
                                       max(content_h, visible_h)))

        canvas.bind("<Configure>", lambda e: (canvas.itemconfig(_wid, width=e.width - 112),
                                               _update_scrollregion()))
        inner.bind("<Configure>",  lambda e: _update_scrollregion())

        _tab_canvases.append(canvas)
        return inner, canvas

    frm_setup,      _setup_canvas  = _make_tab("Setup")
    frm_status,     _status_canvas = _make_tab("Status")
    frm_model,      _ = _make_tab("Model")
    frm_behaviour,  _ = _make_tab("Behaviour")
    frm_rephrase,   _ = _make_tab("Assistant")
    frm_appearance, _ = _make_tab("Appearance")

    def _on_tab_change(_event=None):
        idx = notebook.index("current")
        _active_canvas[0] = _tab_canvases[idx]
    notebook.bind("<<NotebookTabChanged>>", _on_tab_change)
    _active_canvas[0] = _tab_canvases[0]   # default to first tab

    def _on_mousewheel(event):
        c = _active_canvas[0]
        if c:
            c.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_mousewheel_linux(event, direction):
        c = _active_canvas[0]
        if c:
            c.yview_scroll(direction, "units")

    # Linux fires Button-4/5 for scroll; Windows and macOS use <MouseWheel>.
    if sys.platform == "linux":
        win.bind_all("<Button-4>", lambda e: _on_mousewheel_linux(e, -1))
        win.bind_all("<Button-5>", lambda e: _on_mousewheel_linux(e,  1))
    else:
        win.bind_all("<MouseWheel>", _on_mousewheel)

    def _redirect_scroll(event):
        """Redirect mousewheel to the canvas instead of changing the combobox value."""
        if sys.platform == "linux":
            # event.num is 4 (up) or 5 (down) on Linux
            _on_mousewheel_linux(event, -1 if event.num == 4 else 1)
        else:
            _on_mousewheel(event)
        return "break"

    def _bind_scroll(widget):
        """Bind scroll-redirect to a widget, cross-platform."""
        if sys.platform == "linux":
            widget.bind("<Button-4>", _redirect_scroll)
            widget.bind("<Button-5>", _redirect_scroll)
        else:
            widget.bind("<MouseWheel>", _redirect_scroll)

    # ── Shared state handed to the per-tab builders ─────────────────────────
    ui = _SettingsUI()
    ui.win, ui.cfg = win, cfg
    ui.on_cfg_saved, ui.on_model_loaded = on_cfg_saved, on_model_loaded
    ui.on_close = on_close
    ui.notebook = notebook
    ui.bottom_bar = bottom_bar
    ui.bind_scroll = _bind_scroll
    ui.frm_setup, ui.frm_status = frm_setup, frm_status
    ui.frm_model, ui.frm_behaviour = frm_model, frm_behaviour
    ui.frm_rephrase, ui.frm_appearance = frm_rephrase, frm_appearance
    ui.active_canvas = _active_canvas
    ui.setup_canvas, ui.status_canvas = _setup_canvas, _status_canvas
    # Placeholder until _build_setup_tab replaces it; _build_model_tab wires
    # its Test & Connect success path through this hook.
    ui.refresh_setup_tab = lambda *a, **k: None

    def _reopen():
        """Close and rebuild the window (theme or font-size changes need it)."""
        on_close()
        ui_thread._UI_ROOT.after(80, lambda: _ui_call(
            _open_settings_ui, cfg, on_cfg_saved, on_model_loaded,
            wait=False))
    ui.reopen = _reopen
    # Call-time read of the singleton: after a reopen, closures holding `win`
    # point at the destroyed window; this always yields the live one.
    ui.current_win = lambda: _settings_win

    _build_status_tab(ui)
    _build_model_tab(ui)
    _build_behaviour_tab(ui)
    _build_setup_tab(ui)
    _build_rephrase_tab(ui)
    _build_appearance_tab(ui)
    _build_bottom_bar(ui)

    win.lift()
    win.focus_force()
