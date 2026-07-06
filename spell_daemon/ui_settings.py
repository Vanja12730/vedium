import sys
import os
import queue
import threading
import tkinter as tk
import tkinter.ttk as ttk
from tkinter import filedialog, messagebox

from . import constants as c
from . import llm
from . import diagnostics
from .llm import fetch_cloud_models, CLOUD_PROVIDERS, connect_server, detect_local_server, connect_cloud, free_chat_stream, _stream_cancel, _streaming_answer, _strip_reasoning
from .constants import BG, DEFAULT_CFG, GPU_OPTIONS, N_CTX_OPTIONS, WORKING_LANGUAGE_OPTIONS, REPHRASE_MODES, _BUILTIN_MODES, _FACTORY_PROMPTS, SURFACE, SURF2, TEXT, MUTED, ACCENT, ACCENT2, WARN, ERR, SUCCESS, FONT_B, FONT_SIZE_OFFSET, SCHEMES, _OS_RESERVED_HOTKEYS, _OS_RESERVED_HOTKEY_REASONS
from .config import (
    save_cfg,
    _merge_custom_modes,
    credential_store_available,
    get_stored_cloud_api_key,
    store_cloud_api_key,
    delete_stored_cloud_api_key,
)
from .constants import FORMATS, _detect_format_from_path

_settings_win = None   # module-level singleton reference
_UI_ROOT = None       # hidden root owned by the UI thread
_UI_THREAD_ID = None
_UI_QUEUE = queue.Queue(maxsize=64)
_UI_READY = threading.Event()
_UI_STARTED = False
_UI_START_LOCK = threading.Lock()

def _update_sv_ttk_theme():
    """Use Tk's lightweight recolorable theme.

    The third-party sv_ttk theme makes live resizing of this widget-heavy
    Settings window several times slower on Windows.  ``clam`` still honours
    the custom ttk colours below without the expensive resize path.
    """
    try:
        style = ttk.Style()
        if "clam" in style.theme_names() and style.theme_use() != "clam":
            style.theme_use("clam")
    except Exception:
        pass

def _ui_thread_main():
    """Own the single Tk interpreter used by settings, popups, and clipboard."""
    global _UI_ROOT, _UI_THREAD_ID
    _UI_THREAD_ID = threading.get_ident()
    root = tk.Tk()
    root.withdraw()
    _UI_ROOT = root
    _UI_READY.set()
    _update_sv_ttk_theme()

    def pump_queue():
        while True:
            try:
                fn, args, kwargs, done, box = _UI_QUEUE.get_nowait()
            except queue.Empty:
                break
            try:
                box["result"] = fn(*args, **kwargs)
            except Exception as e:
                box["error"] = e
                try:
                    diagnostics.record("ui", "error", "UI action failed.", f"{fn.__name__}: {e}")
                except Exception:
                    pass
            finally:
                if done is not None:
                    done.set()
        root.after(30, pump_queue)

    root.after(30, pump_queue)
    root.mainloop()


def _ensure_ui_thread():
    """Start the Tk UI thread once and wait until its hidden root exists."""
    global _UI_STARTED
    with _UI_START_LOCK:
        if not _UI_STARTED:
            threading.Thread(target=_ui_thread_main, daemon=True).start()
            _UI_STARTED = True
    _UI_READY.wait(timeout=5)


def _ui_call(fn, *args, wait: bool = False, **kwargs):
    """
    Run a callable on the single Tk UI thread.

    This prevents Tk widgets, Tk variables, popups, settings windows, and
    clipboard calls from being created/destroyed by different worker threads.
    """
    _ensure_ui_thread()
    if threading.get_ident() == _UI_THREAD_ID:
        return fn(*args, **kwargs)

    done = threading.Event() if wait else None
    box = {}
    try:
        _UI_QUEUE.put_nowait((fn, args, kwargs, done, box))
    except queue.Full:
        # Queue is full (> 64 pending callbacks). Fire-and-forget calls are
        # dropped rather than blocking the caller (which may be the pynput
        # listener thread or a streaming LLM thread holding _llm_lock).
        # Blocking calls fall back to a short-timeout put so they still
        # eventually execute without deadlocking indefinitely.
        if wait:
            _UI_QUEUE.put((fn, args, kwargs, done, box), timeout=10)
        else:
            return None  # drop the fire-and-forget call
    if wait:
        done.wait()
        if "error" in box:
            raise box["error"]
        return box.get("result")
    return None


def _open_settings(cfg: dict, on_cfg_saved, on_model_loaded):
    """Open (or focus) the Settings window. Safe to call from any thread."""
    _ui_call(_open_settings_ui, cfg, on_cfg_saved, on_model_loaded, wait=False)


def _add_tooltip(widget, text: str):
    """
    Attach a simple hover tooltip to any Tk widget.
    The tooltip appears after a short delay and follows the mouse.
    """
    tip = None

    def show(_event):
        nonlocal tip
        if tip:
            return
        x = widget.winfo_rootx() + 20
        y = widget.winfo_rooty() + widget.winfo_height() + 4
        tip = tk.Toplevel(widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        # Choose a high-contrast bg/fg pair regardless of the active scheme.
        # Luminance < 0.15 = dark scheme (Charcoal); use SURF2/TEXT.
        # Otherwise darken ACCENT slightly for the bg and use a near-white fg.
        def _lum(h):
            r,g,b = int(h[1:3],16)/255, int(h[3:5],16)/255, int(h[5:7],16)/255
            return 0.2126*r + 0.7152*g + 0.0722*b
        if _lum(BG) < 0.15:
            tip_bg, tip_fg = SURF2, TEXT
        else:
            r,g,b = int(ACCENT[1:3],16), int(ACCENT[3:5],16), int(ACCENT[5:7],16)
            tip_bg = "#{:02x}{:02x}{:02x}".format(max(0,r-30), max(0,g-30), max(0,b-30))
            tip_fg = "#f5f5f0"
        border = tk.Frame(tip, bg=MUTED, padx=1, pady=1)
        border.pack(fill="both", expand=True)
        tk.Label(
            border, text=text, font=("Segoe UI", 9), bg=tip_bg, fg=tip_fg,
            relief="flat", padx=10, pady=7, wraplength=320, justify="left",
        ).pack()

    def hide(_event):
        nonlocal tip
        if tip:
            tip.destroy()
            tip = None

    widget.bind("<Enter>", show)
    widget.bind("<Leave>", hide)



def lbl_row(text, row, frm):
    tk.Label(frm, text=text, font=FONT_B, bg=BG, fg=MUTED,
             anchor="e", width=20).grid(row=row, column=0, sticky="e",
                                        padx=(0, 10), pady=7)


# ── Pill-shaped toggle switch ─────────────────────────────────────────────
# Returns a tk.Frame containing [canvas pill] [text label].
# variable  : tk.BooleanVar that the toggle mirrors.
# text      : label shown to the right of the pill.
# state     : "normal" (default) or "disabled" (greyed out, non-interactive).
# command   : optional callable fired after each toggle (receives no args).
_TGL_W, _TGL_H = 40, 22          # pill track size in pixels
_TGL_PAD       = 3               # gap between knob edge and track edge
# Subtitle labels under a toggle should be indented by this much to
# clear the pill + its right padding.
_TGL_INDENT    = _TGL_W + 10

_OFF_X      = _TGL_H // 2            # knob centre x when off
_ON_X       = _TGL_W - _TGL_H // 2   # knob centre x when on
_KNOB_R     = _TGL_H // 2 - _TGL_PAD
_ANIM_STEPS = 8                      # frames per slide
_ANIM_MS    = 12                     # delay between frames (~96ms total)

def _hex_to_rgb(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

def _rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(int(round(v)) for v in rgb)

def _lerp(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))

def _make_toggle(parent, variable, text, state="normal", command=None):
    outer = tk.Frame(parent, bg=BG)

    cvs = tk.Canvas(outer, width=_TGL_W, height=_TGL_H,
                    bg=BG, highlightthickness=0,
                    cursor=("hand2" if state == "normal" else ""))
    cvs.pack(side="left", padx=(0, 8))

    lbl = tk.Label(outer, text=text, font=FONT_B, bg=BG,
                   fg=TEXT if state == "normal" else MUTED)
    lbl.pack(side="left")

    def _target():
        """Resting (knob_x, track_rgb) for the variable's current value."""
        on    = variable.get()
        track = ACCENT2 if (on and state == "normal") else SURF2
        return (_ON_X if on else _OFF_X), _hex_to_rgb(track)

    # Current drawn state, advanced frame-by-frame by the animation.
    _start_x, _start_track = _target()
    _cur  = {"x": float(_start_x), "track": _start_track}
    _anim = {"id": None}

    def _paint(knob_x, track_rgb):
        cvs.delete("all")
        track_col = _rgb_to_hex(track_rgb)
        # Pill track: two semicircle end-caps + centre fill
        cap = _TGL_H
        cvs.create_oval(0, 0, cap, _TGL_H,
                        fill=track_col, outline=track_col)
        cvs.create_rectangle(cap // 2, 0, _TGL_W - cap // 2, _TGL_H,
                             fill=track_col, outline=track_col)
        cvs.create_oval(_TGL_W - cap, 0, _TGL_W, _TGL_H,
                        fill=track_col, outline=track_col)
        # Knob
        cvs.create_oval(knob_x - _KNOB_R, _TGL_PAD,
                        knob_x + _KNOB_R, _TGL_H - _TGL_PAD,
                        fill="white", outline="")

    def _animate(*_):
        # Cancel any in-flight slide so rapid toggling stays clean.
        if _anim["id"] is not None:
            try:
                cvs.after_cancel(_anim["id"])
            except Exception:
                pass
            _anim["id"] = None
        end_x, end_track     = _target()
        start_x, start_track = _cur["x"], _cur["track"]

        def _step(i):
            # The window may close mid-animation; stop touching a dead canvas.
            try:
                if not cvs.winfo_exists():
                    return
            except tk.TclError:
                return
            t = 1 - (1 - i / _ANIM_STEPS) ** 2   # ease-out
            _cur["x"]     = start_x + (end_x - start_x) * t
            _cur["track"] = _lerp(start_track, end_track, t)
            _paint(_cur["x"], _cur["track"])
            if i < _ANIM_STEPS:
                _anim["id"] = cvs.after(_ANIM_MS, _step, i + 1)
            else:
                _cur["x"], _cur["track"] = end_x, end_track
                _anim["id"] = None

        _step(1)

    def _click(_event=None):
        if state != "normal":
            return
        variable.set(not variable.get())
        if command:
            command()

    cvs.bind("<Button-1>", _click)
    lbl.bind("<Button-1>", _click)
    variable.trace_add("write", _animate)
    _paint(_cur["x"], _cur["track"])   # initial draw, no animation
    # Expose the text label so callers can attach tooltips to a leaf widget
    # rather than the container frame (frame <Enter>/<Leave> misfires on children).
    outer.label = lbl
    return outer


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

    win = tk.Toplevel(_UI_ROOT)
    _settings_win = win
    win.title("Spell Daemon — Settings")
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
            "Rephrase": "\u201c",
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
    frm_rephrase,   _ = _make_tab("Rephrase")
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

    # ── Status tab ───────────────────────────────────────────────────────────
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
            if backend_mode == "cloud" and not cfg.get("cloud_api_key", "").strip():
                return "Needs setup", "Add an API key in the Model tab, then click Test & Connect.", WARN
            return "Needs setup", "Choose or test a backend in the Model tab.", WARN
        if mode == "Off":
            return "Ready, spell check off", "Turn Writing mode to Toast or Auto to get sentence corrections. Rephrase still uses F8.", WARN
        if state.get("busy"):
            return "Working", "Wait for the current correction or rephrase to finish.", ACCENT2
        return "Ready", "Type a sentence, or select text and press the rephrase hotkey.", SUCCESS

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
            backend = getattr(llm, "_llm_backend", "") or state.get("backend") or "None"
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



    # Determine initial mode from config. Prefer the explicit backend_mode; fall
    # back to inferring from server_url for configs saved before it existed.
    _init_mode = cfg.get("backend_mode", "")
    if _init_mode not in ("local", "server", "cloud"):
        _init_mode = "server" if cfg.get("server_url", "").strip() else "local"
    backend_var = tk.StringVar(master=win, value=_init_mode)

    lbl_row("Backend", 0, frm_model)
    mf = tk.Frame(frm_model, bg=BG)
    mf.grid(row=0, column=1, sticky="ew", pady=7)
    mf.columnconfigure(0, weight=1)

    # Radio buttons
    rb_frame = tk.Frame(mf, bg=BG)
    rb_frame.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
    tk.Radiobutton(rb_frame, text="Local model (.gguf)", variable=backend_var,
                   value="local", font=FONT_B, bg=BG, fg=TEXT,
                   activebackground=BG, selectcolor=SURFACE,
                   command=lambda: _on_backend_switch()).pack(side="left", padx=(0, 16))
    tk.Radiobutton(rb_frame, text="llama-server", variable=backend_var,
                   value="server", font=FONT_B, bg=BG, fg=TEXT,
                   activebackground=BG, selectcolor=SURFACE,
                   command=lambda: _on_backend_switch()).pack(side="left", padx=(0, 16))
    tk.Radiobutton(rb_frame, text="Cloud API", variable=backend_var,
                   value="cloud", font=FONT_B, bg=BG, fg=TEXT,
                   activebackground=BG, selectcolor=SURFACE,
                   command=lambda: _on_backend_switch()).pack(side="left")

    # ── Local model sub-panel ─────────────────────────────────────────────────
    local_frame = tk.Frame(mf, bg=BG)
    local_frame.columnconfigure(0, weight=1)

    model_var = tk.StringVar(master=win, value=cfg.get("model_path", ""))
    local_status_var = tk.StringVar(master=win, value="")
    tk.Entry(local_frame, textvariable=model_var, font=FONT_B,
             bg=SURFACE, fg=TEXT, insertbackground=ACCENT2,
             relief="flat").grid(row=0, column=0, sticky="ew", ipady=4, padx=(0, 6))

    def _check_local_model():
        path = model_var.get().strip()
        if not path:
            local_status_var.set("Choose a GGUF model file first.")
            local_status_lbl.config(fg=WARN)
            diagnostics.record("backend", "warning", "Local model path is empty.")
            return
        if not os.path.isfile(path):
            local_status_var.set("Model file was not found.")
            local_status_lbl.config(fg=ERR)
            diagnostics.record("backend", "error", "Local model file was not found.", path)
            return
        detected = _detect_format_from_path(path)
        if detected and detected in FORMATS:
            fmt_var.set(detected)
            fmt_status_var.set(f"  ✓ Auto-detected: {detected}")
            fmt_status_lbl.config(fg=SUCCESS)
            local_status_var.set(f"File found. Prompt format: {detected}.")
        else:
            local_status_var.set("File found. Pick the prompt format manually if needed.")
        local_status_lbl.config(fg=SUCCESS)
        diagnostics.record("backend", "info", "Local model file found.")

    def browse():
        p = filedialog.askopenfilename(parent=win, title="Select GGUF model",
                                       filetypes=[("GGUF", "*.gguf"), ("All", "*.*")])
        if p:
            model_var.set(p)
            detected = _detect_format_from_path(p)
            if detected and detected in FORMATS:
                fmt_var.set(detected)
                fmt_status_var.set(f"  ✓ Auto-detected: {detected}")
                fmt_status_lbl.config(fg=SUCCESS)
            else:
                fmt_status_var.set("  ✗ Could not detect — please select manually")
                fmt_status_lbl.config(fg=WARN)
            _check_local_model()

    tk.Button(local_frame, text="Browse", font=FONT_B, bg=ACCENT, fg="white",
              relief="flat", padx=8, cursor="hand2",
              command=browse).grid(row=0, column=1)
    tk.Button(local_frame, text="Check", font=FONT_B, bg=SURF2, fg=TEXT,
              relief="flat", padx=8, cursor="hand2",
              command=_check_local_model).grid(row=0, column=2, padx=(6, 0))
    local_status_lbl = tk.Label(local_frame, textvariable=local_status_var,
                                font=("Segoe UI", 8), bg=BG, fg=MUTED)
    local_status_lbl.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 0))

    # ── llama-server sub-panel ────────────────────────────────────────────────
    server_frame = tk.Frame(mf, bg=BG)
    server_frame.columnconfigure(0, weight=1)

    server_url_var    = tk.StringVar(master=win, value=cfg.get("server_url", ""))
    server_status_var = tk.StringVar(master=win, value="")

    sf_inner = tk.Frame(server_frame, bg=BG)
    sf_inner.grid(row=0, column=0, columnspan=2, sticky="ew")
    sf_inner.columnconfigure(0, weight=1)
    tk.Entry(sf_inner, textvariable=server_url_var, font=FONT_B,
             bg=SURFACE, fg=TEXT, insertbackground=ACCENT2,
             relief="flat").grid(row=0, column=0, sticky="ew", ipady=4, padx=(0, 6))

    server_status_lbl = tk.Label(server_frame, textvariable=server_status_var,
                                  font=("Segoe UI", 8), bg=BG, fg=MUTED)
    server_status_lbl.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))

    autodetect_var = tk.BooleanVar(master=win, value=cfg.get("auto_detect_server", True))
    autodetect_frame = tk.Frame(server_frame, bg=BG)
    autodetect_frame.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
    _make_toggle(autodetect_frame, autodetect_var,
                 "Auto-detect a running local server on startup").pack(anchor="w")
    tk.Label(autodetect_frame,
             text="Scans common ports (8080, 8000, 8033, 8081, 5000, …) and connects "
                  "to the first llama-server found — no URL needed",
             font=("Segoe UI", 8), bg=BG, fg=MUTED
             ).pack(anchor="w", padx=(_TGL_INDENT, 0))

    nothink_var = tk.BooleanVar(master=win, value=cfg.get("server_disable_thinking", True))
    nothink_frame = tk.Frame(server_frame, bg=BG)
    nothink_frame.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
    _make_toggle(nothink_frame, nothink_var,
                 "Disable thinking on reasoning models").pack(anchor="w")
    tk.Label(nothink_frame,
             text="Recommended ON: reasoning models (Gemma, Qwen3, …) answer directly "
                  "instead of spending the token budget on hidden chain-of-thought. "
                  "Turn OFF for higher-quality answers on complex chat questions (slower).",
             font=("Segoe UI", 8), bg=BG, fg=MUTED, wraplength=380, justify="left"
             ).pack(anchor="w", padx=(_TGL_INDENT, 0))

    def _test_server():
        url = server_url_var.get().strip()
        # No URL entered → scan common local ports and connect to whatever is found.
        if not url:
            server_status_var.set("Searching for a local server…")
            server_status_lbl.config(fg=MUTED)
            def _do_detect():
                found = detect_local_server()
                if not found:
                    _ui_call(lambda: (
                        server_status_var.set("✗ No local server detected on common ports"),
                        server_status_lbl.config(fg=ERR),
                    ), wait=False)
                    return
                # Reflect the discovered URL in the field, then connect to it.
                _ui_call(lambda u=found: server_url_var.set(u), wait=False)
                try:
                    connect_server(found, cfg_n_ctx=ctx_var.get())
                    n = llm._llm._n_ctx if hasattr(llm._llm, "_n_ctx") else "?"
                    _ui_call(lambda u=found, n=n: (
                        server_status_var.set(f"✓ Detected & connected: {u}  (n_ctx={n})"),
                        server_status_lbl.config(fg=SUCCESS),
                    ), wait=False)
                except Exception as exc:
                    msg = str(exc)
                    diagnostics.record("backend", "error", "Server auto-detect connection failed.", msg)
                    _ui_call(lambda m=msg: (
                        server_status_var.set(f"✗ {m}"),
                        server_status_lbl.config(fg=ERR),
                    ), wait=False)
            threading.Thread(target=_do_detect, daemon=True).start()
            return
        server_status_var.set("Testing…")
        server_status_lbl.config(fg=MUTED)
        def _do_test():
            try:
                connect_server(url, cfg_n_ctx=ctx_var.get())
                n = llm._llm._n_ctx if hasattr(llm._llm, "_n_ctx") else "?"
                _ui_call(lambda n=n: (
                    server_status_var.set(f"✓ Connected  (n_ctx={n})"),
                    server_status_lbl.config(fg=SUCCESS),
                ), wait=False)
            except Exception as exc:
                msg = str(exc)
                diagnostics.record("backend", "error", "Server connection test failed.", msg)
                _ui_call(lambda m=msg: (
                    server_status_var.set(f"✗ {m}"),
                    server_status_lbl.config(fg=ERR),
                ), wait=False)
        threading.Thread(target=_do_test, daemon=True).start()

    tk.Button(sf_inner, text="Test / Auto-Detect", font=FONT_B, bg=ACCENT2, fg="white",
              relief="flat", padx=8, cursor="hand2",
              command=_test_server).grid(row=0, column=1)

    # ── Cloud API sub-panel ───────────────────────────────────────────────────
    cloud_frame = tk.Frame(mf, bg=BG)
    cloud_frame.columnconfigure(1, weight=1)

    cloud_provider_var = tk.StringVar(master=win, value=cfg.get("cloud_provider") or "Google Gemini")
    cloud_url_var      = tk.StringVar(master=win, value=cfg.get("cloud_base_url", ""))
    cloud_key_var      = tk.StringVar(master=win, value=cfg.get("cloud_api_key", ""))
    cloud_model_var    = tk.StringVar(master=win, value=cfg.get("cloud_model", ""))
    cloud_status_var   = tk.StringVar(master=win, value="")
    cred_available = credential_store_available()
    remember_key_var   = tk.BooleanVar(master=win, value=bool(cfg.get("remember_cloud_api_key", True) and cred_available))
    # Per-provider key memory for this run + favorites (quick-switch list).
    _cloud_keys    = dict(cfg.get("cloud_api_keys", {}))     # provider -> api key, stripped before save
    _prev_provider = [cloud_provider_var.get()]
    _favorites     = [dict(f) for f in cfg.get("cloud_favorites", []) if isinstance(f, dict)]

    def _cloud_lbl(text, r):
        tk.Label(cloud_frame, text=text, font=("Segoe UI", 9), bg=BG, fg=MUTED,
                 anchor="w").grid(row=r, column=0, sticky="w", padx=(0, 8), pady=3)

    _cloud_lbl("Provider", 0)
    prov_menu = tk.OptionMenu(cloud_frame, cloud_provider_var, *CLOUD_PROVIDERS.keys())
    prov_menu.config(font=FONT_B, bg=SURFACE, fg=TEXT, activebackground=SURF2,
                     highlightthickness=0, relief="flat", anchor="w")
    prov_menu["menu"].config(bg=SURFACE, fg=TEXT)
    prov_menu.grid(row=0, column=1, sticky="ew", pady=3)

    _cloud_lbl("Base URL", 1)
    tk.Entry(cloud_frame, textvariable=cloud_url_var, font=FONT_B,
             bg=SURFACE, fg=TEXT, insertbackground=ACCENT2, relief="flat"
             ).grid(row=1, column=1, sticky="ew", ipady=3, pady=3)

    _cloud_lbl("API key", 2)
    keyrow = tk.Frame(cloud_frame, bg=BG)
    keyrow.grid(row=2, column=1, sticky="ew", pady=3)
    keyrow.columnconfigure(0, weight=1)
    cloud_key_entry = tk.Entry(keyrow, textvariable=cloud_key_var, font=FONT_B, show="•",
             bg=SURFACE, fg=TEXT, insertbackground=ACCENT2, relief="flat")
    cloud_key_entry.grid(row=0, column=0, sticky="ew", ipady=3, padx=(0, 6))
    def _toggle_key():
        cloud_key_entry.config(show="" if cloud_key_entry.cget("show") else "•")
    tk.Button(keyrow, text="👁", font=FONT_B, bg=SURF2, fg=TEXT, relief="flat",
              cursor="hand2", command=_toggle_key).grid(row=0, column=1)
    remember_state = "normal" if cred_available else "disabled"
    remember_toggle = _make_toggle(
        keyrow,
        remember_key_var,
        "Remember securely",
        state=remember_state,
    )
    remember_toggle.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
    _add_tooltip(
        remember_toggle,
        "Stores the API key in Windows Credential Manager for your Windows user. "
        "The key is still stripped from spell_daemon_config.json.",
    )

    # Model: an editable dropdown populated from the provider's /models endpoint.
    _cloud_lbl("Model", 3)
    modelrow = tk.Frame(cloud_frame, bg=BG)
    modelrow.grid(row=3, column=1, sticky="ew", pady=3)
    modelrow.columnconfigure(0, weight=1)
    _cb_style = ttk.Style()
    _cb_style.configure("Cloud.TCombobox", fieldbackground=SURFACE, background=SURFACE,
                        foreground=TEXT, arrowcolor=TEXT, relief="flat")
    win.option_add("*TCombobox*Listbox.background", SURFACE)
    win.option_add("*TCombobox*Listbox.foreground", TEXT)
    win.option_add("*TCombobox*Listbox.selectBackground", ACCENT2)
    win.option_add("*TCombobox*Listbox.font", FONT_B)
    cloud_model_cb = ttk.Combobox(modelrow, textvariable=cloud_model_var,
                                  font=FONT_B, style="Cloud.TCombobox", values=[])
    cloud_model_cb.grid(row=0, column=0, sticky="ew", ipady=2, padx=(0, 6))
    fetch_btn = tk.Button(modelrow, text="↻ Fetch", font=FONT_B, bg=SURF2, fg=TEXT,
                          relief="flat", cursor="hand2")
    fetch_btn.grid(row=0, column=1, padx=(0, 6))
    fav_add_btn = tk.Button(modelrow, text="★", font=FONT_B, bg=SURF2, fg=ACCENT2,
                            relief="flat", cursor="hand2")
    fav_add_btn.grid(row=0, column=2)
    _add_tooltip(fav_add_btn, "Save the current provider + model to Favorites")

    # Favorites: quick-switch list of saved provider+model combos.
    _cloud_lbl("Favorites", 4)
    favrow = tk.Frame(cloud_frame, bg=BG)
    favrow.grid(row=4, column=1, sticky="ew", pady=3)
    favrow.columnconfigure(0, weight=1)
    fav_var = tk.StringVar(master=win, value="")
    fav_cb = ttk.Combobox(favrow, textvariable=fav_var, font=FONT_B,
                          style="Cloud.TCombobox", values=[], state="readonly")
    fav_cb.grid(row=0, column=0, sticky="ew", ipady=2, padx=(0, 6))
    fav_use_btn = tk.Button(favrow, text="Use", font=FONT_B, bg=ACCENT2, fg="white",
                            relief="flat", cursor="hand2")
    fav_use_btn.grid(row=0, column=1, padx=(0, 6))
    fav_del_btn = tk.Button(favrow, text="✕", font=FONT_B, bg=SURF2, fg=ERR,
                            relief="flat", cursor="hand2")
    fav_del_btn.grid(row=0, column=2)
    _add_tooltip(fav_del_btn, "Remove the selected favorite")

    cloud_hint = tk.Label(cloud_frame, font=("Segoe UI", 8), bg=BG, fg=MUTED,
                          justify="left", wraplength=380, text="")
    cloud_hint.grid(row=5, column=0, columnspan=2, sticky="w", pady=(2, 0))

    cloud_status_lbl = tk.Label(cloud_frame, textvariable=cloud_status_var,
                                font=("Segoe UI", 8), bg=BG, fg=MUTED)
    cloud_status_lbl.grid(row=6, column=0, columnspan=2, sticky="w", pady=(2, 0))

    def _fetch_models(auto: bool = False):
        url, key = cloud_url_var.get().strip(), cloud_key_var.get().strip()
        if not url or not key:
            if not auto:
                cloud_status_var.set("Enter the base URL and API key first, then Fetch.")
                cloud_status_lbl.config(fg=WARN)
            return
        cloud_status_var.set("Fetching models…")
        cloud_status_lbl.config(fg=MUTED)
        def _do():
            try:
                models   = fetch_cloud_models(url, key)
                ids      = [mid for mid, _ in models]
                free_ids = [mid for mid, fr in models if fr]
                def _apply():
                    cloud_model_cb["values"] = ids
                    if not cloud_model_var.get().strip() and ids:
                        cloud_model_var.set(ids[0])
                    msg = f"✓ {len(ids)} models"
                    if free_ids:
                        msg += f" — {len(free_ids)} free (listed first)"
                    cloud_status_var.set(msg)
                    cloud_status_lbl.config(fg=SUCCESS)
                _ui_call(_apply, wait=False)
            except Exception as exc:
                m = str(exc)
                diagnostics.record("backend", "error", "Cloud model list failed.", m)
                _ui_call(lambda m=m: (cloud_status_var.set(f"✗ Could not list models: {m}"),
                                      cloud_status_lbl.config(fg=ERR)), wait=False)
        threading.Thread(target=_do, daemon=True).start()
    fetch_btn.config(command=_fetch_models)

    # ── Favorites logic ───────────────────────────────────────────────────────
    def _fav_display(f):
        return f"{f.get('provider', '?')} · {f.get('model', '?')}"
    def _refresh_favorites():
        fav_cb["values"] = [_fav_display(f) for f in _favorites]
    def _add_favorite():
        model = cloud_model_var.get().strip()
        if not model:
            cloud_status_var.set("Pick a model first, then click ★.")
            cloud_status_lbl.config(fg=WARN)
            return
        entry = {"provider": cloud_provider_var.get(),
                 "base_url": cloud_url_var.get().strip(), "model": model}
        if entry in _favorites:
            cloud_status_var.set("Already in favorites.")
            cloud_status_lbl.config(fg=MUTED)
            return
        _favorites.append(entry)
        _refresh_favorites()
        fav_var.set(_fav_display(entry))
        cloud_status_var.set(f"★ Added: {_fav_display(entry)}")
        cloud_status_lbl.config(fg=SUCCESS)
    def _use_favorite():
        match = next((f for f in _favorites if _fav_display(f) == fav_var.get()), None)
        if not match:
            return
        # Setting the provider fires _on_provider_change (URL + per-provider key +
        # model refresh); apply the saved URL/model afterwards so they win.
        cloud_provider_var.set(match.get("provider", "Custom"))
        if match.get("base_url"):
            cloud_url_var.set(match["base_url"])
        cloud_model_var.set(match.get("model", ""))
        cloud_status_var.set(f"Loaded favorite: {_fav_display(match)}")
        cloud_status_lbl.config(fg=SUCCESS)
    def _del_favorite():
        idx = next((i for i, f in enumerate(_favorites)
                    if _fav_display(f) == fav_var.get()), None)
        if idx is None:
            return
        removed = _favorites.pop(idx)
        _refresh_favorites()
        fav_var.set("")
        cloud_status_var.set(f"Removed: {_fav_display(removed)}")
        cloud_status_lbl.config(fg=MUTED)
    fav_add_btn.config(command=_add_favorite)
    fav_use_btn.config(command=_use_favorite)
    fav_del_btn.config(command=_del_favorite)
    _refresh_favorites()

    def _remember_provider_key(provider: str, key: str):
        provider = (provider or "").strip()
        key = (key or "").strip()
        if not provider:
            return
        if key:
            _cloud_keys[provider] = key
        if remember_key_var.get() and key:
            if not store_cloud_api_key(provider, key):
                cloud_status_var.set("Could not save key to Windows Credential Manager.")
                cloud_status_lbl.config(fg=WARN)
        elif cred_available:
            delete_stored_cloud_api_key(provider)

    _MODEL_HINTS = {
        "Google Gemini": "Free key at aistudio.google.com → 'Get API key'. All Gemini models are usable on the free tier (rate-limited).",
        "Grok (xAI)":    "Key from console.x.ai. Paid — no free models.",
        "OpenAI":        "Key from platform.openai.com. Paid — no free models.",
        "Groq":          "Free key from console.groq.com. Generous free tier on the listed open models.",
        "OpenRouter":    "Key from openrouter.ai. Models ending in ':free' cost nothing — those are listed first.",
        "DeepSeek":      "Key from platform.deepseek.com. Paid (low cost).",
        "Custom":        "Any OpenAI-compatible base URL (usually ends in /v1). Click Fetch to list its models.",
    }
    def _on_provider_change(*_):
        prov = cloud_provider_var.get()
        # Remember the key typed for the previous provider; load this one's key.
        if _prev_provider[0] and _prev_provider[0] != prov:
            _remember_provider_key(_prev_provider[0], cloud_key_var.get().strip())
            key = _cloud_keys.get(prov, "")
            if remember_key_var.get() and not key:
                key = get_stored_cloud_api_key(prov)
                if key:
                    _cloud_keys[prov] = key
            cloud_key_var.set(key)
        _prev_provider[0] = prov
        if prov != "Custom":
            cloud_url_var.set(CLOUD_PROVIDERS.get(prov, ""))
        cloud_hint.config(text=_MODEL_HINTS.get(prov, ""))
        cloud_model_cb["values"] = []          # clear stale list from the old provider
        if cloud_key_var.get().strip():
            _fetch_models(auto=True)            # auto-refresh when a key is already present
    # Fill the URL for a fresh config, but keep a saved/custom URL on first open.
    if not cloud_url_var.get().strip() and cloud_provider_var.get() != "Custom":
        cloud_url_var.set(CLOUD_PROVIDERS.get(cloud_provider_var.get(), ""))
    cloud_hint.config(text=_MODEL_HINTS.get(cloud_provider_var.get(), ""))
    cloud_provider_var.trace_add("write", _on_provider_change)
    # If a key is available in this session/environment, populate the dropdown on open.
    if cloud_key_var.get().strip() and cloud_url_var.get().strip():
        _fetch_models(auto=True)

    def _test_cloud():
        cloud_status_var.set("Connecting…")
        cloud_status_lbl.config(fg=MUTED)
        url = cloud_url_var.get().strip()
        key = cloud_key_var.get().strip()
        model = cloud_model_var.get().strip()
        def _do():
            try:
                connect_cloud(url, key, model, cfg_n_ctx=max(ctx_var.get(), 8192))
                def _connected():
                    cfg["backend_mode"] = "cloud"
                    cfg["cloud_provider"] = cloud_provider_var.get()
                    cfg["cloud_base_url"] = url
                    cfg["cloud_api_key"] = key
                    cfg["cloud_model"] = model
                    cfg["remember_cloud_api_key"] = bool(remember_key_var.get())
                    _remember_provider_key(cloud_provider_var.get(), key)
                    cfg["cloud_api_keys"] = dict(_cloud_keys)
                    diagnostics.set_status(backend_mode="cloud")
                    if remember_key_var.get() and cred_available:
                        cloud_status_var.set(f"Connected to {model}. API key saved in Windows Credential Manager.")
                    else:
                        cloud_status_var.set(f"Connected to {model} for this run. API keys are not saved to disk.")
                    cloud_status_lbl.config(fg=SUCCESS)
                    _refresh_setup_tab()
                _ui_call(_connected, wait=False)
            except Exception as exc:
                m = str(exc)
                diagnostics.record("backend", "error", "Cloud connection test failed.", m)
                _ui_call(lambda m=m: (cloud_status_var.set(f"✗ {m}"),
                                      cloud_status_lbl.config(fg=ERR)), wait=False)
        threading.Thread(target=_do, daemon=True).start()

    tk.Button(cloud_frame, text="Test & Connect", font=FONT_B, bg=ACCENT2, fg="white",
              relief="flat", padx=8, cursor="hand2", command=_test_cloud
              ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 0))

    tk.Label(
        cloud_frame, font=("Segoe UI", 8), bg=BG, fg=MUTED,
        justify="left", wraplength=380,
        text="API keys are stripped from the settings file. With Remember securely on, "
             "the key is saved in Windows Credential Manager for your Windows user.",
    ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(4, 0))

    # ── Show/hide sub-panels based on radio selection ─────────────────────────
    def _on_backend_switch():
        for f in (local_frame, server_frame, cloud_frame):
            f.grid_remove()
        mode = backend_var.get()
        panel = {"local": local_frame, "server": server_frame, "cloud": cloud_frame}.get(mode, local_frame)
        panel.grid(row=1, column=0, columnspan=2, sticky="ew")

    # Apply initial visibility
    _on_backend_switch()

    # ── Prompt format ─────────────────────────────────────────────────────────
    fmt_var = tk.StringVar(master=win, value=cfg.get("prompt_format", FORMATS[0]))
    fmt_status_var = tk.StringVar(master=win, value="")
    lbl_row("Prompt format", 1, frm_model)
    fmt_frame = tk.Frame(frm_model, bg=BG)
    fmt_frame.grid(row=1, column=1, sticky="ew", pady=7)
    fmt_frame.columnconfigure(0, weight=1)
    fmt_cb = ttk.Combobox(fmt_frame, textvariable=fmt_var, values=FORMATS,
                          state="readonly", font=FONT_B)
    fmt_cb.grid(row=0, column=0, sticky="ew")
    _bind_scroll(fmt_cb)
    fmt_status_lbl = tk.Label(fmt_frame, textvariable=fmt_status_var,
                               font=FONT_B, bg=BG, fg=SUCCESS)
    fmt_status_lbl.grid(row=1, column=0, sticky="w")

    # If a model is already configured, show detection result immediately
    existing_path = cfg.get("model_path", "")
    if existing_path:
        detected = _detect_format_from_path(existing_path)
        if detected and detected in FORMATS:
            # Only show the label — don't override a manually-saved format choice
            fmt_status_var.set(f"  ✓ Detected from filename: {detected}")
            fmt_status_lbl.config(fg=MUTED)
        # No warning on startup — the user may have set the format manually before

    # ── GPU acceleration ──────────────────────────────────────────────────────
    gpu_var = tk.StringVar(master=win, value=cfg.get("gpu_acceleration", "Auto"))
    lbl_row("GPU acceleration", 2, frm_model)
    gf = tk.Frame(frm_model, bg=BG)
    gf.grid(row=2, column=1, sticky="w", pady=7)
    gpu_cb = ttk.Combobox(gf, textvariable=gpu_var, values=GPU_OPTIONS,
                          state="readonly", font=FONT_B, width=8)
    gpu_cb.pack(side="left")
    _bind_scroll(gpu_cb)
    # Show the actual backend that loaded (GPU / CPU / CPU (GPU fallback) etc.)
    # so the user can immediately see whether GPU is really being used.
    backend_now = "not loaded"
    if "GPU" in backend_now and "fallback" not in backend_now and "failed" not in backend_now:
        backend_colour = SUCCESS
    elif "CPU" in backend_now:
        backend_colour = WARN
    else:
        backend_colour = MUTED
    backend_label_var = tk.StringVar(master=win, value=f"  Active: {backend_now}")
    backend_lbl = tk.Label(gf, textvariable=backend_label_var,
             font=FONT_B, bg=BG, fg=backend_colour)
    backend_lbl.pack(side="left")
    win.backend_lbl = backend_lbl
    win.backend_label_var = backend_label_var

    # ── Context size ──────────────────────────────────────────────────────────
    ctx_var = tk.IntVar(master=win, value=cfg.get("n_ctx", 768))
    lbl_row("Context size", 3, frm_model)
    cf = tk.Frame(frm_model, bg=BG)
    cf.grid(row=3, column=1, sticky="w", pady=7)
    ctx_top = tk.Frame(cf, bg=BG)
    ctx_top.pack(anchor="w")
    ctx_cb = ttk.Combobox(ctx_top, textvariable=ctx_var, values=N_CTX_OPTIONS,
                          state="readonly", font=FONT_B, width=6)
    ctx_cb.pack(side="left")
    _bind_scroll(ctx_cb)
    tk.Label(ctx_top, text=" tokens", font=FONT_B, bg=BG, fg=TEXT).pack(side="left")
    tk.Label(cf, text="Takes effect on Save & Load Model  ·  larger = more VRAM / RAM  ·  increase if rephrasing large selections",
             font=("Segoe UI", 8), bg=BG, fg=MUTED).pack(anchor="w")
    _add_tooltip(ctx_cb,
        "Controls how much text the model can read at once (prompt + output combined).\n\n"
        "Think of it as the model's working memory:\n"
        "  • 768–1024    Normal use — sentence-by-sentence correction\n"
        "  • 2048–4096   A few paragraphs selected for rephrasing\n"
        "  • 8192+        Half a page or more selected at once\n"
        "  • 32768+       Several pages of selected text\n\n"
        "If you select a large block of text and the result looks cut off or garbled,\n"
        "increase this value, save, and reload the model.\n\n"
        "The trade-off: larger values use more RAM (CPU) or VRAM (GPU).\n"
        "Takes effect only after clicking Save & Load Model."
    )

    # ── Working language ──────────────────────────────────────────────────────
    lang_var = tk.StringVar(master=win, value=cfg.get("working_language", "Auto"))
    lbl_row("Working language", 0, frm_behaviour)
    lf = tk.Frame(frm_behaviour, bg=BG)
    lf.grid(row=0, column=1, sticky="w", pady=7)
    lang_cb = ttk.Combobox(lf, textvariable=lang_var, values=WORKING_LANGUAGE_OPTIONS,
                           state="readonly", font=FONT_B, width=12)
    lang_cb.pack(anchor="w")
    _bind_scroll(lang_cb)
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
        ).pack(anchor="w")
    tk.Label(sc_frame,
             text="Off lets you type freely and still use the rephrase hotkey (F8)",
             font=("Segoe UI", 8), bg=BG, fg=MUTED).pack(anchor="w", pady=(2, 0))

    # ── Diacritics-only auto toggle ───────────────────────────────────────────
    diacr_var = tk.BooleanVar(master=win, value=cfg.get("diacritics_only_auto", False))
    lbl_row("Diacritics only", 2, frm_behaviour)
    diacr_frame = tk.Frame(frm_behaviour, bg=BG)
    diacr_frame.grid(row=2, column=1, sticky="w", pady=7)
    _make_toggle(diacr_frame, diacr_var,
                 "Auto spell-check restores diacritics only (no grammar fixes)"
                 ).pack(anchor="w")
    tk.Label(diacr_frame,
             text="Useful when typing BHS (Bosnian/Croatian/Serbian) without diacritics",
             font=("Segoe UI", 8), bg=BG, fg=MUTED
             ).pack(anchor="w", padx=(_TGL_INDENT, 0))

    # ── Setup tab ────────────────────────────────────────────────────────────
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
        backend_now = getattr(llm, "_llm_backend", "") or ""
        if backend_now:
            backend_txt = f"Ready: {backend_now}"
            backend_col = SUCCESS
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
                setup_vars["writing"].set("Sentence correction is off. Rephrase still works.")
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
        ).grid(row=0, column=_col, padx=(0, 14), sticky="w")
    tk.Label(_trig_frame, text="Characters that end a sentence and trigger spell-check",
             font=("Segoe UI", 8), bg=BG, fg=MUTED).grid(
                 row=1, column=0, columnspan=len(_TRIGGER_OPTIONS), sticky="w", pady=(2, 0))

    _adv_lbl("Min words", 1)
    tk.Spinbox(adv_body, from_=1, to=10, textvariable=min_var, width=5,
               font=FONT_B, bg=SURFACE, fg=TEXT,
               relief="flat").grid(row=1, column=1, sticky="w", pady=5)

    _adv_lbl("Idle reset (s)", 2)
    idle_sb = tk.Spinbox(adv_body, from_=0.0, to=120.0, increment=5.0,
                         textvariable=idle_var, width=5,
                         font=FONT_B, bg=SURFACE, fg=TEXT, relief="flat")
    idle_sb.grid(row=2, column=1, sticky="w", pady=5)
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

    # ── Rephrase hotkey capture ───────────────────────────────────────────────
    hotkey_var = tk.StringVar(master=win, value=cfg.get("rephrase_hotkey", "f8"))

    def _hotkey_display(s: str) -> str:
        """Turn a stored hotkey string into a readable label (e.g. 'f8' → 'F8')."""
        if not s:
            return "—"
        if len(s) == 1:
            return s.upper()          # 'a' → 'A', '/' → '/'
        return s[0].upper() + s[1:]   # 'f8' → 'F8', 'insert' → 'Insert'

    lbl_row("Hotkey", 0, frm_rephrase)
    hf = tk.Frame(frm_rephrase, bg=BG)
    hf.grid(row=0, column=1, sticky="w", pady=7)

    # Top row: key badge + Change button + hint text
    hf_controls = tk.Frame(hf, bg=BG)
    hf_controls.pack(side="top", anchor="w")

    hotkey_display_var = tk.StringVar(master=win,
                                      value=_hotkey_display(cfg.get("rephrase_hotkey", "f8")))
    # Styled to look like a physical keyboard key in normal state.
    # _start_capture / _cancel_capture swap bg+fg to signal the active capture state.
    hotkey_lbl = tk.Label(hf_controls, textvariable=hotkey_display_var,
                          font=("Segoe UI", 9, "bold"),
                          bg=SURF2, fg=TEXT, relief="groove",
                          anchor="center", padx=10, pady=4)
    hotkey_lbl.pack(side="left", padx=(0, 10))

    # Error label lives below the controls row; shown only when a bad key is pressed.
    hotkey_err_var = tk.StringVar(master=win, value="")
    hotkey_err_lbl = tk.Label(hf, textvariable=hotkey_err_var,
                               font=("Segoe UI", 8), bg=BG, fg=ERR)

    def _show_hotkey_err(msg: str):
        hotkey_err_var.set(msg)
        hotkey_err_lbl.pack(side="top", anchor="w", pady=(4, 0))
        win.after(2200, _hide_hotkey_err)

    def _hide_hotkey_err():
        hotkey_err_lbl.pack_forget()
        hotkey_err_var.set("")

    _hk_capturing = [False]

    def _start_capture():
        _hk_capturing[0] = True
        hotkey_display_var.set("Press a key…")
        hotkey_lbl.config(bg=ACCENT2, fg="white", relief="flat")
        capture_btn.config(text="Cancel", bg=WARN)
        win.bind("<KeyPress>", _capture_key)
        win.focus_set()

    def _cancel_capture():
        _hk_capturing[0] = False
        hotkey_display_var.set(_hotkey_display(hotkey_var.get()))
        hotkey_lbl.config(bg=SURF2, fg=TEXT, relief="groove")
        capture_btn.config(text="Change…", bg=SURFACE)
        win.unbind("<KeyPress>")
        _hide_hotkey_err()

    def _capture_key(event):
        if not _hk_capturing[0]:
            return
        keysym = event.keysym   # e.g. "F8", "Insert", "a", "slash"
        # Cancel on Escape; ignore bare modifier keypresses
        if keysym == "Escape":
            _cancel_capture()
            return "break"
        if keysym in ("Shift_L", "Shift_R", "Control_L", "Control_R",
                      "Alt_L", "Alt_R", "Super_L", "Super_R",
                      "Caps_Lock", "Num_Lock", "Scroll_Lock"):
            # Modifier keys fire on every shortcut or capitalised letter, so they
            # can't be used as a standalone hotkey — use an F-key instead.
            _show_hotkey_err("Modifier keys fire too often (every Shift, Ctrl, Alt press) — use F1–F12.")
            return "break"
        # Warn against keys that would break normal typing
        if keysym in ("Return", "Tab", "BackSpace", "Delete", "space"):
            _show_hotkey_err("This key types text — it can't be used as a hotkey.")
            return "break"
        # Block bare printable characters (letters, digits, punctuation).
        # The keyboard listener runs without OS-level suppression, so any
        # printable key set as a hotkey would ALSO type that character into
        # whatever app is focused — the rephrase fires and the letter lands.
        # F-keys and other special keys (Insert, Pause, etc.) do not have
        # this problem because they produce no printable output in text fields.
        if event.char and event.char.isprintable() and len(event.char) == 1:
            _show_hotkey_err("Letter keys also type into your app — use F1–F12 or a special key (Insert, Pause…).")
            return "break"
        # Block OS-reserved keys that interfere with the clipboard copy probe.
        if keysym.lower() in _OS_RESERVED_HOTKEYS:
            reason = _OS_RESERVED_HOTKEY_REASONS.get(keysym.lower(), f"{keysym} is reserved by the OS.")
            _show_hotkey_err(reason)
            return "break"
        # Use keysym name for all accepted keys (special / function keys only)
        stored = keysym.lower()   # "f8", "insert", "pause", etc.
        hotkey_var.set(stored)
        cfg["rephrase_hotkey"] = stored
        save_cfg(cfg)
        on_cfg_saved(cfg)
        diagnostics.record("config", "info", f"Rephrase hotkey set to {_hotkey_display(stored)}.")
        hotkey_display_var.set(_hotkey_display(stored))
        hotkey_lbl.config(bg=SURF2, fg=SUCCESS, relief="groove")
        _hk_capturing[0] = False
        capture_btn.config(text="Change…", bg=SURFACE)
        win.unbind("<KeyPress>")
        _hide_hotkey_err()
        # Brief green flash then back to normal colour
        win.after(900, lambda: hotkey_lbl.config(fg=TEXT))
        return "break"

    capture_btn = tk.Button(hf_controls, text="Change…", font=FONT_B,
                            bg=SURFACE, fg=TEXT, relief="flat",
                            padx=8, cursor="hand2",
                            command=lambda: _cancel_capture()
                            if _hk_capturing[0] else _start_capture())
    capture_btn.pack(side="left", padx=(0, 8))
    # Hint text on its own line so it doesn't crowd the controls
    tk.Label(hf, text="Press to rephrase current, last, or selected text",
             font=("Segoe UI", 8), bg=BG, fg=MUTED).pack(side="top", anchor="w", pady=(4, 0))

    # ── Rephrase mode dropdown ────────────────────────────────────────────────
    rephrase_var = tk.StringVar(master=win, value=cfg.get("rephrase_mode", "Diacritics Only"))
    lbl_row("Rephrase mode", 1, frm_rephrase)
    rf = tk.Frame(frm_rephrase, bg=BG)
    rf.grid(row=1, column=1, sticky="w", pady=7)

    def _selectable_rephrase_modes(custom_modes: dict | None = None) -> list[str]:
        custom_modes = custom_modes if custom_modes is not None else cfg.get("custom_rephrase_modes", {})
        custom_names = [name for name in custom_modes if name not in _BUILTIN_MODES]
        return list(_BUILTIN_MODES) + custom_names

    rephrase_cb = ttk.Combobox(rf, textvariable=rephrase_var,
                               values=_selectable_rephrase_modes(),
                                state="readonly", font=FONT_B, width=24)
    rephrase_cb.pack(side="left")
    _bind_scroll(rephrase_cb)

    _prev_rephrase = [rephrase_var.get()]
    def _on_rephrase_pick(event=None):
        val = rephrase_var.get()
        if val not in _selectable_rephrase_modes():
            rephrase_var.set(_prev_rephrase[0])
        else:
            _prev_rephrase[0] = val
            cfg["rephrase_mode"] = val
            save_cfg(cfg)
            on_cfg_saved(cfg)
            diagnostics.record("config", "info", f"Default rephrase mode set to {val}.")
    rephrase_cb.bind("<<ComboboxSelected>>", _on_rephrase_pick)
    tk.Label(rf, text="  None = spell-check only",
             font=FONT_B, bg=BG, fg=MUTED).pack(side="left")

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
    tk.Label(frm_rephrase, text="✦  Rephrase Mode Prompts", font=("Segoe UI", 9, "bold"),
             bg=BG, fg=ACCENT2).grid(row=4, column=0, columnspan=2, sticky="w",
                                     pady=(2, 2))
    tk.Label(frm_rephrase,
             text="Select any mode to view or edit its prompt.  "
                  "Highlighted names have been customised — Reset restores the factory text.",
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

    lb_frame = tk.Frame(cm_frame, bg=BG)
    lb_frame.grid(row=0, column=0, sticky="ew")
    lb_frame.columnconfigure(0, weight=1)
    lb_scroll = tk.Scrollbar(lb_frame, orient="vertical")
    lb = tk.Listbox(
        lb_frame, font=FONT_B, bg=SURFACE, fg=TEXT, selectbackground=ACCENT,
        selectforeground="white", relief="flat", height=7,
        yscrollcommand=lb_scroll.set, activestyle="none",
    )
    lb_scroll.config(command=lb.yview)
    lb.grid(row=0, column=0, sticky="ew")
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

    def _refresh_lb():
        """Repopulate listbox with all modes and sync the rephrase combobox."""
        # Remember selection by mode name, not raw label (label now includes trigger).
        sel_raw = lb.get(lb.curselection()[0]) if lb.curselection() else None
        sel_name = sel_raw.split("  ·  ")[0] if sel_raw else None
        lb.delete(0, "end")
        idx = 0
        for name in _BUILTIN_MODES:
            lb.insert("end", _lb_label(name))
            lb.itemconfig(idx, fg=ACCENT2 if name in _builtin_overrides else MUTED)
            idx += 1
        for name in _custom:
            lb.insert("end", _lb_label(name))
            lb.itemconfig(idx, fg=TEXT)
            idx += 1
        all_names = list(_BUILTIN_MODES) + list(_custom)
        if sel_name and sel_name in all_names:
            pos = all_names.index(sel_name)
            lb.selection_set(pos)
            lb.see(pos)
        _merge_custom_modes({**cfg, "custom_rephrase_modes": _custom,
                             "builtin_prompt_overrides": _builtin_overrides})
        rephrase_modes = _selectable_rephrase_modes(_custom)
        rephrase_cb["values"] = rephrase_modes
        if rephrase_var.get() not in rephrase_modes:
            rephrase_var.set("Rephrase Text")
            cfg["rephrase_mode"] = "Rephrase Text"
        _on_select()

    def _on_select(event=None):
        sel = lb.curselection()
        if not sel:
            edit_btn.config(state="disabled", fg=MUTED)
            reset_btn.config(state="disabled", fg=MUTED)
            delete_btn.config(state="disabled", fg=MUTED)
            return
        name       = lb.get(sel[0]).split("  ·  ")[0]  # strip trigger tag
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

    lb.bind("<<ListboxSelect>>", _on_select)
    lb.bind("<Double-Button-1>", lambda _e: _edit_mode())
    _refresh_lb()

    def _sync_mode_cfg(message: str):
        cfg["custom_rephrase_modes"]    = dict(_custom)
        cfg["builtin_prompt_overrides"] = dict(_builtin_overrides)
        cfg["mode_triggers"]            = dict(_mode_triggers)
        cfg["mode_hotkeys"]             = dict(_mode_hotkeys)
        cfg["rephrase_mode"]            = rephrase_var.get()
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
                 text="Type this prefix before your text, then press the rephrase hotkey.  "
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
                 text="Press this key anywhere to rephrase using this mode directly.  "
                      "Leave unset to use the global rephrase hotkey instead.",
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
            _sync_mode_cfg(f"Rephrase mode saved: {name}.")
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
        sel = lb.curselection()
        if not sel:
            return
        name = lb.get(sel[0]).split("  ·  ")[0]  # strip trigger tag
        _open_mode_dialog(edit_name=name, is_builtin=(name in _BUILTIN_MODES))

    def _reset_mode():
        sel = lb.curselection()
        if not sel:
            return
        name = lb.get(sel[0]).split("  ·  ")[0]  # strip trigger tag
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
        sel = lb.curselection()
        if not sel:
            return
        name = lb.get(sel[0]).split("  ·  ")[0]  # strip trigger/hotkey tag
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

    _add_tooltip(lb,
        "All rephrase modes — built-in and custom.\n"
        "Highlighted names have been customised; muted names use the factory prompt.\n"
        "Modes with a trigger or hotkey show them after the · separator.\n\n"
        "Double-click or select + Edit Prompt to change a mode's prompt, trigger, or hotkey.\n"
        "Trigger: type the prefix before your text and press the rephrase hotkey —\n"
        "the daemon will use that mode for just that one shot, then revert.\n"
        "Hotkey: press the assigned key directly to rephrase with that mode immediately.\n"
        "Built-in overrides can be undone with Reset.\n"
        "Custom modes can be added with + Add, or removed with Delete."
    )

    # ── Appearance ────────────────────────────────────────────────────────────
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
        on_close()
        _UI_ROOT.after(80, lambda: _ui_call(
            _open_settings_ui, cfg, on_cfg_saved, on_model_loaded,
            wait=False))

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

    # ── Status label + Save button (always visible, outside scroll area) ─────
    # Show the live backend if one is already connected ("None" = not connected).
    _backend_now = getattr(llm, "_llm_backend", "") or ""
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
        cfg["remember_cloud_api_key"] = bool(remember_key_var.get())
        # Keep provider keys in memory for this run; save_cfg strips secrets from disk.
        _remember_provider_key(cloud_provider_var.get(), cloud_key_var.get().strip())
        cfg["cloud_api_keys"]         = dict(_cloud_keys)
        cfg["cloud_favorites"]        = [dict(f) for f in _favorites]
        use_server = mode == "server"
        cfg["auto_detect_server"]     = autodetect_var.get()
        cfg["server_disable_thinking"] = nothink_var.get()
        # Apply live so the change takes effect without reconnecting the server.
        llm._server_disable_thinking = bool(nothink_var.get())
        cfg["prompt_format"]          = fmt_var.get()
        cfg["gpu_acceleration"]       = gpu_var.get()
        cfg["working_language"]       = lang_var.get()
        cfg["trigger_chars"]          = _trig_string()
        cfg["min_word_count"]         = min_var.get()
        cfg["idle_reset_sec"]         = idle_var.get()
        cfg["spell_check_mode"]       = sc_mode_var.get()
        cfg["diacritics_only_auto"]   = diacr_var.get()
        cfg["rephrase_hotkey"]        = hotkey_var.get()
        cfg["rephrase_mode"]          = rephrase_var.get()
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
                if mode == "cloud":
                    backend = connect_cloud(
                        cfg["cloud_base_url"], cfg["cloud_api_key"], cfg["cloud_model"],
                        cfg_n_ctx=max(cfg.get("n_ctx", 4096), 8192),
                    )
                elif use_server:
                    backend = connect_server(cfg["server_url"], cfg_n_ctx=cfg.get("n_ctx", 4096))
                else:
                    backend = on_model_loaded(model_var.get())
                def _refresh(b=backend):
                    active_win = _settings_win
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
                    active_win = _settings_win
                    if active_win and active_win.winfo_exists() and hasattr(active_win, "status_lbl") and active_win.status_lbl.winfo_exists():
                        active_win.status_lbl.config(text=f"Error: {m}", fg=ERR)
                _ui_call(_refresh_err, wait=False)

        if new_offset != old_offset:
            global FONT_SIZE_OFFSET
            FONT_SIZE_OFFSET = new_offset
            c.apply_font_size(new_offset)
            on_close()
            _UI_ROOT.after(80, lambda: _ui_call(
                _open_settings_ui, cfg, on_cfg_saved, on_model_loaded,
                wait=False))
            if model_changed:
                threading.Thread(target=_load, daemon=True).start()
            return

        if model_changed:
            status.config(
                text={"cloud": "Saved. Connecting to cloud API…",
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

    win.lift()
    win.focus_force()


_chat_win = None   # module-level singleton reference for the chat window


def _open_chat_window(cfg: dict):
    """
    Open (or lift) the standalone free-form AI chat window.

    The window is a singleton: if it is already open it is simply brought to the
    front.  It is scheduled on the single Tk UI thread so it is safe to call
    from any background thread (e.g. the tray callback thread).

    Layout:
      ┌─────────────────────────────────────┐
      │ ✏  Chat with AI              [Clear]│
      │─────────────────────────────────────│
      │  [chat history — scrollable Text]   │
      │─────────────────────────────────────│
      │  [entry field…………………]  [Send]       │
      └─────────────────────────────────────┘
    """
    _ui_call(_open_chat_window_ui, cfg, wait=False)


def _open_chat_window_ui(cfg: dict):
    """Build and show the chat window (must be called on the UI thread)."""
    global _chat_win
    _update_sv_ttk_theme()

    # If already open, just lift it to the front.
    if _chat_win is not None:
        try:
            _chat_win.lift()
            _chat_win.focus_force()
            return
        except tk.TclError:
            _chat_win = None   # window was destroyed externally

    fmt = cfg.get("prompt_format", "ChatML  (Qwen / Phi-3 / Yi)")

    win = tk.Toplevel(_UI_ROOT)
    win.title("Chat with AI")
    win.attributes("-topmost", False)
    win.configure(bg=SURFACE)
    win.resizable(True, True)

    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    saved_geo = cfg.get("chat_window_geometry", "")
    if saved_geo:
        win.geometry(saved_geo)
    else:
        scale = 1.0 + (FONT_SIZE_OFFSET / 10.0) if FONT_SIZE_OFFSET > 0 else 1.0
        w, h = int(520 * scale), int(560 * scale)
        win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    _chat_win = win

    def _on_destroy(_event=None):
        global _chat_win
        _stream_cancel.set()
        cfg["chat_window_geometry"] = win.geometry()
        save_cfg(cfg)
        _chat_win = None

    win.bind("<Destroy>", _on_destroy)

    # ── Top accent stripe + header ─────────────────────────────────────────────
    tk.Frame(win, bg=ACCENT, height=3).pack(fill="x")

    header = tk.Frame(win, bg=SURFACE, padx=14, pady=8)
    header.pack(fill="x")

    tk.Label(header, text="✏  Chat with AI",
             font=("Segoe UI", 10 + FONT_SIZE_OFFSET, "bold"), bg=SURFACE, fg=ACCENT2).pack(side="left")

    def _clear_chat():
        """Wipe conversation history and reset the display."""
        _history.clear()
        _stream_cancel.set()
        chat_txt.config(state="normal")
        chat_txt.delete("1.0", "end")
        chat_txt.config(state="disabled")

    tk.Button(header, text="Clear",
              font=FONT_B, bg=SURF2, fg=MUTED,
              relief="flat", padx=8, pady=3, cursor="hand2",
              command=_clear_chat).pack(side="right")

    tk.Frame(win, bg=SURF2, height=1).pack(fill="x")

    # ── Chat display ──────────────────────────────────────────────────────────
    body = tk.Frame(win, bg=SURFACE)
    body.pack(fill="both", expand=True, padx=0, pady=0)

    chat_frame = tk.Frame(body, bg=SURFACE)
    chat_frame.pack(fill="both", expand=True, padx=10, pady=(8, 4))

    scrollbar = tk.Scrollbar(chat_frame)
    scrollbar.pack(side="right", fill="y")

    chat_txt = tk.Text(
        chat_frame, font=("Segoe UI", 10 + FONT_SIZE_OFFSET), bg=BG, fg=TEXT,
        relief="flat", wrap="word", padx=10, pady=8,
        yscrollcommand=scrollbar.set,
        state="disabled",
    )
    chat_txt.pack(fill="both", expand=True)
    scrollbar.config(command=chat_txt.yview)

    # Tag styles for user vs assistant turns with improved spacing.
    # spacing1: padding above the block. spacing3: padding below.
    # lmargin1/2: indent message bodies relative to the sender name.
    chat_txt.tag_configure("user_name",  font=("Segoe UI", 9 + FONT_SIZE_OFFSET, "bold"), foreground=ACCENT2, spacing1=8,  spacing3=2)
    chat_txt.tag_configure("user_text",  font=("Segoe UI", 10 + FONT_SIZE_OFFSET),        foreground=TEXT,    spacing3=4,  lmargin1=4, lmargin2=4)
    chat_txt.tag_configure("ai_name",    font=("Segoe UI", 9 + FONT_SIZE_OFFSET, "bold"), foreground=MUTED,   spacing1=8,  spacing3=2)
    chat_txt.tag_configure("ai_text",    font=("Segoe UI", 10 + FONT_SIZE_OFFSET),        foreground=TEXT,    spacing3=4,  lmargin1=4, lmargin2=4)
    chat_txt.tag_configure("ai_stream",  font=("Segoe UI", 10 + FONT_SIZE_OFFSET),        foreground=SUCCESS, spacing3=4,  lmargin1=4, lmargin2=4)
    chat_txt.tag_configure("separator",  font=("Segoe UI", 6 + FONT_SIZE_OFFSET),         foreground=SURF2,   spacing1=4,  spacing3=4)

    # ── Input row ─────────────────────────────────────────────────────────────
    tk.Frame(win, bg=SURF2, height=1).pack(fill="x")

    input_frame = tk.Frame(win, bg=SURFACE, padx=10, pady=8)
    input_frame.pack(fill="x")
    input_frame.columnconfigure(0, weight=1)

    _placeholder = "Type a message…"
    entry = tk.Entry(input_frame, font=FONT_B, bg=BG, fg=MUTED,
                     insertbackground=ACCENT2, relief="flat")
    entry.insert(0, _placeholder)
    entry.grid(row=0, column=0, sticky="ew", ipady=5, padx=(0, 8))

    send_btn = tk.Button(input_frame, text="Send", font=("Segoe UI", 9 + FONT_SIZE_OFFSET, "bold"),
                         bg=ACCENT, fg="white",
                         relief="flat", padx=14, pady=4, cursor="hand2")
    send_btn.grid(row=0, column=1)

    # ── State ─────────────────────────────────────────────────────────────────
    _history: list = []          # [{"role": "user"|"assistant", "content": "..."}]
    _busy    = [False]           # True while a stream is running

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _append_display(text: str, *tags):
        """Append text to the read-only chat display (must be on UI thread)."""
        chat_txt.config(state="normal")
        chat_txt.insert("end", text, *tags)
        chat_txt.see("end")
        chat_txt.config(state="disabled")

    def _replace_last_ai_chunk(full_text: str):
        """Replace the live-streamed 'ai_stream' span with the finished 'ai_text'."""
        chat_txt.config(state="normal")
        # Find and remove the streamed chunk range, replace with finalized text.
        try:
            start = chat_txt.tag_ranges("ai_stream")
            if start:
                chat_txt.delete(start[0], start[1])
        except Exception:
            pass
        chat_txt.insert("end", full_text, "ai_text")
        chat_txt.see("end")
        chat_txt.config(state="disabled")

    def _do_send(_event=None):
        """Handle Send button / Enter key press."""
        if _busy[0]:
            return

        # Clear placeholder
        raw = entry.get()
        if raw == _placeholder or not raw.strip():
            return
        user_msg = raw.strip()
        entry.delete(0, "end")

        # Display user turn
        if _history:
            _append_display("\n", "separator")
        _append_display("You\n", "user_name")
        _append_display(user_msg + "\n", "user_text")

        _history.append({"role": "user", "content": user_msg})

        # Disable input while the model is generating
        _busy[0] = True
        send_btn.config(state="disabled", bg=SURF2)
        entry.config(state="disabled")

        _stream_cancel.clear()

        # Display AI label and stream placeholder
        _append_display("\nAI\n", "ai_name")

        def _stream_worker():
            chunks = []
            last_render = [None]

            def _render_stream(t):
                # Replace the current ai_stream region with the latest text so
                # chain-of-thought never lingers on screen as it streams.
                try:
                    chat_txt.config(state="normal")
                    ranges = chat_txt.tag_ranges("ai_stream")
                    if ranges:
                        chat_txt.delete(ranges[0], ranges[1])
                    chat_txt.insert("end", t, "ai_stream")
                    chat_txt.see("end")
                    chat_txt.config(state="disabled")
                except Exception:
                    pass

            try:
                for piece in free_chat_stream(_history, fmt, cfg):
                    chunks.append(piece)
                    # Hide chain-of-thought while streaming: show "Thinking…"
                    # until the answer emerges, then render the answer-so-far.
                    visible, thinking = _streaming_answer("".join(chunks))
                    shown = visible or "💭 Thinking…"
                    if shown == last_render[0]:
                        continue
                    last_render[0] = shown
                    _ui_call(lambda t=shown: _render_stream(t), wait=False)
            except Exception as _exc:  # noqa: BLE001
                # Log the real error so it shows up in the daemon log, then
                # inject a visible placeholder so _finish() re-enables the UI
                # and the user is not left staring at a frozen input box.
                import logging as _logging
                _logging.getLogger(__name__).error(
                    "free_chat_stream raised an unexpected error: %s",
                    _exc,
                    exc_info=True,
                )
                detail = str(_exc).strip() or "Error generating response. Check the daemon log for details."
                _err_msg = f"\n\n[AI note: {detail}]\n"
                chunks.append(_err_msg)
                _ui_call(lambda m=_err_msg: _append_display(m, "ai_stream"), wait=False)
            finally:
                full_reply = "".join(chunks).strip()
                full_reply = _strip_reasoning(full_reply).strip()

                if full_reply:
                    _history.append({"role": "assistant", "content": full_reply})

                def _finish():
                    # Replace the streamed (green) text with the finalised (normal) version
                    try:
                        chat_txt.config(state="normal")
                        ranges = chat_txt.tag_ranges("ai_stream")
                        if ranges:
                            chat_txt.delete(ranges[0], ranges[1])
                            chat_txt.insert("end", full_reply + "\n", "ai_text")
                        else:
                            # Nothing streamed yet (e.g. model not loaded)
                            if not full_reply:
                                chat_txt.insert("end",
                                    "(No model loaded — open Settings to load one.)\n",
                                    "ai_text")
                        chat_txt.see("end")
                        chat_txt.config(state="disabled")
                    except Exception:
                        pass

                    _busy[0] = False
                    try:
                        send_btn.config(state="normal", bg=ACCENT)
                        entry.config(state="normal")
                        entry.focus_set()
                    except Exception:
                        pass

                _ui_call(_finish, wait=False)

        threading.Thread(target=_stream_worker, daemon=True).start()

    def _on_focus_in(_event):
        if entry.get() == _placeholder:
            entry.delete(0, "end")
            entry.config(fg=TEXT)

    def _on_focus_out(_event):
        if not entry.get():
            entry.insert(0, _placeholder)
            entry.config(fg=MUTED)

    entry.bind("<FocusIn>",  _on_focus_in)
    entry.bind("<FocusOut>", _on_focus_out)
    entry.bind("<Return>",   _do_send)
    send_btn.config(command=_do_send)

    win.lift()
    win.focus_force()
    entry.focus_set()


# ── 10. UI – SUGGESTION POPUPS ───────────────────────────────────────────────
# _Toast is the compact suggestion popup used for automatic spell/grammar fixes.
# _EditableToast is the larger review popup used for deliberate rephrasing.
# Both are scheduled on the single Tk UI thread so worker threads never touch Tk directly.

def _info_toast(message: str, duration_ms: int = 2200):
    """Show a brief, auto-dismissing status notification (no buttons).

    Used to give feedback on hotkey presses that would otherwise fail silently —
    e.g. when there is no selected text and the typed-sentence buffer is empty.
    Scheduled on the UI thread so it is safe to call from any background thread.
    """
    def _show():
        root = tk.Toplevel(_UI_ROOT)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=SURFACE)
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        scale = 1.0 + (FONT_SIZE_OFFSET / 10.0) if FONT_SIZE_OFFSET > 0 else 1.0
        w = int(300 * scale)
        h = int(52 * scale)
        root.geometry(f"{w}x{h}+{sw - w - 20}+{sh - h - 28}")
        tk.Frame(root, bg=MUTED, height=2).pack(fill="x")
        tk.Label(
            root, text=message, font=FONT_B,
            bg=SURFACE, fg=MUTED, wraplength=int(270 * scale), justify="left",
            padx=14, pady=12,
        ).pack(fill="both", expand=True)
        root.after(duration_ms, root.destroy)
    _ui_call(_show, wait=False)


