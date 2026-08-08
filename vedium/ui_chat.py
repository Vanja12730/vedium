# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Standalone free-form AI chat window (tray menu: Chat with AI)."""

import threading
import tkinter as tk

from . import ui_thread
from .ui_thread import _ui_call, _update_sv_ttk_theme
from .config import save_cfg, resolve_prompt_format
from .llm import free_chat_stream, _stream_cancel, _streaming_answer, _strip_reasoning
from .constants import (
    BG, SURFACE, SURF2, TEXT, MUTED, ACCENT, ACCENT2,
    SUCCESS, FONT_B, FONT_SIZE_OFFSET,
)


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

    fmt = resolve_prompt_format(cfg)

    win = tk.Toplevel(ui_thread._UI_ROOT)
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
        _append_display("💭 Thinking…", "ai_stream")

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
