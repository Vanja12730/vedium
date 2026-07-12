"""Reusable Tk widgets shared by the Settings window tabs.

Colour and font names are from-imported; constants.apply_scheme() and
constants.apply_font_size() rewrite them on this module when the user
changes theme or text size, so they stay current.
"""

import tkinter as tk

from .constants import BG, SURF2, TEXT, MUTED, ACCENT, ACCENT2, FONT_B


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
    live_state = {"value": state}

    cvs = tk.Canvas(outer, width=_TGL_W, height=_TGL_H,
                    bg=BG, highlightthickness=0,
                    cursor=("hand2" if live_state["value"] == "normal" else ""))
    cvs.pack(side="left", padx=(0, 8))

    lbl = tk.Label(outer, text=text, font=FONT_B, bg=BG,
                   fg=TEXT if live_state["value"] == "normal" else MUTED)
    lbl.pack(side="left")

    def _target():
        """Resting (knob_x, track_rgb) for the variable's current value."""
        on    = variable.get()
        track = ACCENT2 if (on and live_state["value"] == "normal") else SURF2
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
        if live_state["value"] != "normal":
            return
        variable.set(not variable.get())
        if command:
            command()

    def _set_state(value):
        live_state["value"] = "normal" if value == "normal" else "disabled"
        cvs.config(cursor="hand2" if live_state["value"] == "normal" else "")
        lbl.config(fg=TEXT if live_state["value"] == "normal" else MUTED)
        _animate()

    cvs.bind("<Button-1>", _click)
    lbl.bind("<Button-1>", _click)
    variable.trace_add("write", _animate)
    _paint(_cur["x"], _cur["track"])   # initial draw, no animation
    # Expose the text label so callers can attach tooltips to a leaf widget
    # rather than the container frame (frame <Enter>/<Leave> misfires on children).
    outer.label = lbl
    outer.variable = variable
    outer.invoke = _click
    outer.set_state = _set_state
    return outer
