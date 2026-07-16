import sys
import re
import difflib
import tkinter as tk
from tkinter import messagebox
import threading

from . import constants as c
from .constants import BG, SURFACE, SURF2, TEXT, MUTED, ACCENT, ACCENT2, WARN, ERR, SUCCESS, FONT_B, FONT_SIZE_OFFSET, SCHEMES, _rephrase_history
from .constants import DEFAULT_CFG
from .llm import _stream_cancel, chat_with_text_stream, _streaming_answer, _clean_llm_output
from .cli_models import CLIModelError

# _UI_ROOT is read via the module (ui_thread._UI_ROOT) because it is rebound
# when the UI thread starts — a from-import here would freeze the None it held
# at import time and every toast would silently fall back to Tk's default root.
from . import ui_thread
from .ui_thread import _ui_call

def _apply_windows_decorations(win):
    """Apply modern visual decorations to top-level windows on Windows (10/11).
    Restores the standard drop shadow for borderless windows (overrideredirect)
    and enables Windows 11 rounded corners.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import windll, byref, c_int
        
        # Ensure window has drawn its internal structures and has a valid Win32 HWND
        win.update_idletasks()
        hwnd = windll.user32.GetParent(win.winfo_id())
        if not hwnd:
            hwnd = win.winfo_id()
            
        # 1. Round corners (Windows 11)
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUNDSMALL = 3
        windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_WINDOW_CORNER_PREFERENCE,
            byref(c_int(DWMWCP_ROUNDSMALL)),
            ctypes.sizeof(c_int)
        )
        
        # 2. Force drop shadow for borderless window
        DWMWA_NCRENDERING_POLICY = 2
        DWMNCRP_ENABLED = 2
        windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_NCRENDERING_POLICY,
            byref(c_int(DWMNCRP_ENABLED)),
            ctypes.sizeof(c_int)
        )
    except Exception as e:
        print(f"[Vedium] Win32 decorations failed: {e}", file=sys.stderr)

class _Toast:
    """Compact suggestion popup for automatic spell/grammar fixes."""
    def __init__(self, original: str, corrected: str, on_accept, on_ignore,
                 label: str = "Spell suggestion", fmt: str = "", cfg: dict | None = None):
        self._original        = original
        self._corrected       = corrected
        self._on_accept       = on_accept
        self._on_ignore       = on_ignore
        self._label           = label
        self._fmt             = fmt or DEFAULT_CFG["prompt_format"]
        self._cfg             = cfg
        self._notice_label    = None
        self._notice_before   = None
        self._trigger_accept  = None   # set by _show; called by global pynput dispatch
        self._trigger_ignore  = None   # set by _show; called by global pynput dispatch
        _ui_call(self._show, wait=True)   # wait=True ensures _trigger_accept/_trigger_ignore are set before __init__ returns

    def trigger_accept(self):
        """Accept the suggestion via the global keyboard listener (no focus needed)."""
        if self._trigger_accept:
            _ui_call(self._trigger_accept, wait=False)

    def trigger_ignore(self):
        """Dismiss the suggestion via the global keyboard listener (no focus needed)."""
        if self._trigger_ignore:
            _ui_call(self._trigger_ignore, wait=False)

    def show_model_switch(self, failed_model: str, next_model: str):
        """Show a cloud failover notice without changing the suggestion text."""
        message = f"⚠  {failed_model} hit a rate limit — using {next_model} instead"

        def _show_notice():
            label = self._notice_label
            if label is None:
                return
            try:
                label.config(text=message)
                if not label.winfo_manager() and self._notice_before is not None:
                    label.pack(anchor="w", pady=(2, 0), before=self._notice_before)
            except tk.TclError:
                pass

        _ui_call(_show_notice, wait=False)

    def _show(self):
        root = tk.Toplevel(ui_thread._UI_ROOT)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=SURFACE)

        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        scale = 1.0 + (FONT_SIZE_OFFSET / 10.0) if FONT_SIZE_OFFSET > 0 else 1.0
        w = int(400 * scale)
        h = int(168 * scale)
        root.geometry(f"{w}x{h}+{sw - w - 20}+{sh - h - 32}")
        _apply_windows_decorations(root)
        
        root.focus_force()   # grab focus on appearance so keyboard shortcuts work immediately
        # Electron apps (Obsidian, Chrome) can hold focus momentarily; retry after
        # a short delay so the window is ready to receive keyboard events even if
        # the initial focus_force() is overridden by the host application.
        root.after(150, root.focus_force)

        # Top accent stripe
        tk.Frame(root, bg=ACCENT, height=3).pack(fill="x")

        body = tk.Frame(root, bg=SURFACE, padx=14, pady=10)
        body.pack(fill="both", expand=True)

        tk.Label(body, text=f"✏  {self._label}",
                 font=("Segoe UI", 9 + FONT_SIZE_OFFSET, "bold"), bg=SURFACE, fg=ACCENT2).pack(anchor="w")

        # Mutable corrected text so chat updates can change what Replace accepts
        _current = [self._corrected]
        
        text_widget = tk.Text(body, font=("Georgia", 10 + FONT_SIZE_OFFSET), bg=SURFACE, fg=TEXT,
                               relief="flat", bd=0, highlightthickness=0, wrap="word",
                               height=2)
        text_widget.pack(fill="x", anchor="w", pady=(4, 6))
        self._notice_before = text_widget
        self._notice_label = tk.Label(
            body, font=FONT_B, bg=SURFACE, fg=WARN, justify="left",
            wraplength=int(370 * scale),
        )

        # Inline word-diff styling: unchanged text in TEXT, removed words struck
        # through in ERR, added/changed words highlighted in SUCCESS.
        text_widget.tag_configure("eq",  foreground=TEXT)
        text_widget.tag_configure("del", foreground=ERR, overstrike=True)
        text_widget.tag_configure("add", foreground=SUCCESS)

        def _update_widget_text(orig: str, corr: str):
            """Show `corr` as a word-level diff against `orig` so the user can see
            exactly what changed before accepting."""
            # Each token is a word plus its trailing whitespace, so reassembled
            # runs preserve spacing and the diff aligns on word boundaries.
            a = re.findall(r"\S+\s*", orig)
            b = re.findall(r"\S+\s*", corr)
            text_widget.config(state="normal")
            text_widget.delete("1.0", "end")
            for op, i1, i2, j1, j2 in difflib.SequenceMatcher(
                    a=a, b=b, autojunk=False).get_opcodes():
                if op == "equal":
                    text_widget.insert("end", "".join(b[j1:j2]), "eq")
                elif op == "insert":
                    text_widget.insert("end", "".join(b[j1:j2]), "add")
                elif op == "delete":
                    text_widget.insert("end", "".join(a[i1:i2]), "del")
                else:  # replace — show the old words struck through, then the new
                    text_widget.insert("end", "".join(a[i1:i2]), "del")
                    text_widget.insert("end", "".join(b[j1:j2]), "add")
            text_widget.config(state="disabled")
            
        _update_widget_text(self._original, self._corrected)

        btn_row = tk.Frame(body, bg=SURFACE)
        btn_row.pack(anchor="w")

        closed = {"value": False}
        # Keep a reference to the auto-close timer so we can cancel it if the
        # user starts interacting with the chat input.
        _auto_close_id = [None]

        def close_with(callback):
            if closed["value"]:
                return
            closed["value"] = True
            if _auto_close_id[0] is not None:
                try:
                    root.after_cancel(_auto_close_id[0])
                except Exception:
                    pass
            # Signal any in-progress chat stream to stop on its next token so
            # _backend.lock is released promptly rather than after the full response
            # finishes generating.  The pipelines call _stream_cancel.clear()
            # before each new stream, so this stale signal never affects them.
            _stream_cancel.set()
            try:
                root.destroy()
            finally:
                callback()

        # Wrap on_accept so it forwards the (possibly AI-updated) current text to
        # the pipeline as a parameter.  The pipeline's accept() closure stores it
        # in _accepted_text[0] so the replacement uses whatever the chat panel
        # produced rather than the original LLM suggestion.
        def accept_current():
            self._on_accept(_current[0])

        # Register the trigger callbacks so the global pynput listener can accept
        # or dismiss the toast even when keyboard focus is held by an Electron app
        # (Obsidian, Chrome, VS Code) that does not yield to focus_force().
        self._trigger_accept = lambda: close_with(accept_current)
        self._trigger_ignore = lambda: close_with(self._on_ignore)

        tk.Button(btn_row, text="Replace  ✓",
                  font=("Segoe UI", 9 + FONT_SIZE_OFFSET, "bold"), bg=ACCENT, fg="white",
                  relief="flat", padx=10, pady=3, cursor="hand2",
                  command=lambda: close_with(accept_current)).pack(side="left")

        def _toast_copy():
            # Copy the current (possibly AI-updated) suggestion without closing.
            try:
                root.clipboard_clear()
                root.clipboard_append(_current[0])
                root.update()
            except Exception:
                pass
            copy_btn.config(text="Copied ✓")
            root.after(1500, lambda: copy_btn.config(text="Copy"))

        copy_btn = tk.Button(btn_row, text="Copy",
                  font=FONT_B, bg=SURF2, fg=TEXT,
                  relief="flat", padx=10, pady=3, cursor="hand2",
                  command=_toast_copy)
        copy_btn.pack(side="left", padx=(8, 0))

        tk.Button(btn_row, text="Ignore",
                  font=FONT_B, bg=SURF2, fg=MUTED,
                  relief="flat", padx=10, pady=3, cursor="hand2",
                  command=lambda: close_with(self._on_ignore)).pack(side="left", padx=(8, 0))

        # ── Chat row ──────────────────────────────────────────────────────────
        tk.Frame(body, bg=SURF2, height=1).pack(fill="x", pady=(8, 6))

        # History: list of text snapshots, index of the one currently shown.
        # Starts with the original suggestion; each AI reply appends a new entry.
        # Entirely in-memory — cleared automatically when the toast closes.
        _history  = [self._corrected]
        _hist_idx = [0]

        chat_row = tk.Frame(body, bg=SURFACE)
        chat_row.pack(fill="x")
        chat_row.columnconfigure(2, weight=1)   # col 2 = entry expands

        back_btn = tk.Button(chat_row, text="←", font=FONT_B, bg=SURF2, fg=MUTED,
                             relief="flat", padx=6, pady=2, cursor="hand2",
                             state="disabled")
        back_btn.grid(row=0, column=0, padx=(0, 2))

        fwd_btn = tk.Button(chat_row, text="→", font=FONT_B, bg=SURF2, fg=MUTED,
                            relief="flat", padx=6, pady=2, cursor="hand2",
                            state="disabled")
        fwd_btn.grid(row=0, column=1, padx=(0, 6))

        chat_entry = tk.Entry(chat_row, font=FONT_B, bg=BG, fg=MUTED,
                              insertbackground=ACCENT2, relief="flat")
        chat_entry.insert(0, "Ask AI…")
        chat_entry.grid(row=0, column=2, sticky="ew", ipady=4, padx=(0, 6))

        send_btn = tk.Button(chat_row, text="Send", font=FONT_B, bg=ACCENT2, fg="white",
                             relief="flat", padx=8, pady=2, cursor="hand2")
        send_btn.grid(row=0, column=3)

        def _refresh_nav():
            """Update back/forward button states to match current history position."""
            back_btn.config(state="normal" if _hist_idx[0] > 0 else "disabled",
                            fg=TEXT      if _hist_idx[0] > 0 else MUTED)
            fwd_btn.config( state="normal" if _hist_idx[0] < len(_history) - 1 else "disabled",
                            fg=TEXT      if _hist_idx[0] < len(_history) - 1 else MUTED)

        def _show_history(idx: int):
            """Display a specific history entry and update all dependent state."""
            _hist_idx[0] = idx
            entry = _history[idx]
            _current[0]  = entry
            _update_widget_text(self._original, entry)
            _refresh_nav()

        back_btn.config(command=lambda: _show_history(_hist_idx[0] - 1))
        fwd_btn.config( command=lambda: _show_history(_hist_idx[0] + 1))

        def _on_chat_focus_in(event):
            if chat_entry.get() == "Ask AI…":
                chat_entry.delete(0, "end")
                chat_entry.config(fg=TEXT)
            # Cancel the auto-close so it doesn't dismiss while the user is typing
            if _auto_close_id[0] is not None:
                try:
                    root.after_cancel(_auto_close_id[0])
                    _auto_close_id[0] = None
                except Exception:
                    pass

        def _on_chat_focus_out(event):
            if not chat_entry.get().strip():
                chat_entry.delete(0, "end")
                chat_entry.insert(0, "Ask AI…")
                chat_entry.config(fg=MUTED)

        chat_entry.bind("<FocusIn>",  _on_chat_focus_in)
        chat_entry.bind("<FocusOut>", _on_chat_focus_out)

        def _do_chat(instruction: str):
            """Run the LLM in a background thread; stream result into the label."""
            def _worker():
                # Clear any stale cancel signal from a previously dismissed toast
                # before starting a new stream.  Without this, chat_with_text_stream()
                # would return immediately on its first token check and produce no output.
                _stream_cancel.clear()
                _ui_call(lambda: (
                    text_widget.config(state="normal"),
                    text_widget.delete("1.0", "end"),
                    text_widget.insert("1.0", "⏳ Thinking…"),
                    text_widget.config(state="disabled")
                ), wait=False)
                try:
                    chunks = []
                    for piece in chat_with_text_stream(
                            _current[0], instruction, self._fmt, cfg=self._cfg,
                            on_fallback=self.show_model_switch):
                        chunks.append(piece)
                        # Hide chain-of-thought while streaming: show "Thinking…"
                        # until the answer emerges, then render the answer-so-far.
                        visible, thinking = _streaming_answer("".join(chunks))
                        shown = f'"{visible}"' if visible else "💭 Thinking…"
                        _ui_call(lambda t=shown: (
                            text_widget.config(state="normal"),
                            text_widget.delete("1.0", "end"),
                            text_widget.insert("1.0", t),
                            text_widget.config(state="disabled")
                        ), wait=False)

                    result = _clean_llm_output("".join(chunks).strip())
                    if not result:
                        result = _current[0]

                    def _done(r=result):
                        # Discard any forward history beyond current position, then push.
                        del _history[_hist_idx[0] + 1:]
                        _history.append(r)
                        _current[0] = r
                        _hist_idx[0] = len(_history) - 1
                        _update_widget_text(self._original, r)
                        _refresh_nav()
                        chat_entry.config(state="normal")
                        send_btn.config(state="normal")
                        chat_entry.delete(0, "end")
                        chat_entry.focus_set()
                    _ui_call(_done, wait=False)
                except Exception as exc:
                    if isinstance(exc, CLIModelError):
                        _info_toast(f"AI CLI error: {exc}")
                    def _err():
                        _update_widget_text(self._original, _current[0])
                        chat_entry.config(state="normal")
                        send_btn.config(state="normal")
                    _ui_call(_err, wait=False)

            threading.Thread(target=_worker, daemon=True).start()

        def _send(event=None):
            instruction = chat_entry.get().strip()
            if not instruction or instruction == "Ask AI…":
                return
            chat_entry.config(state="disabled")
            send_btn.config(state="disabled")
            _do_chat(instruction)

        send_btn.config(command=_send)
        chat_entry.bind("<Return>", _send)

        _auto_close_id[0] = root.after(9000, lambda: close_with(self._on_ignore))

        # ── Hover protection ──────────────────────────────────────────────────
        # Cancel the auto-close timer while the cursor is anywhere inside the
        # toast; restart it (same 9 s) only after the cursor truly exits.
        def _on_toast_enter(event=None):
            if _auto_close_id[0] is not None:
                try:
                    root.after_cancel(_auto_close_id[0])
                    _auto_close_id[0] = None
                except Exception:
                    pass

        def _on_toast_leave(event=None):
            if closed["value"] or _auto_close_id[0] is not None:
                return
            # Only restart the timer if the pointer has truly left the window.
            w = root.winfo_containing(event.x_root, event.y_root)
            if w is None or not str(w).startswith(str(root)):
                _auto_close_id[0] = root.after(9000, lambda: close_with(self._on_ignore))

        root.bind("<Enter>", _on_toast_enter, add="+")
        root.bind("<Leave>", _on_toast_leave, add="+")

        # Keyboard shortcuts matching _EditableToast so power users never need
        # the mouse: Ctrl+Return to accept the suggestion, Escape to ignore.
        root.bind("<Control-Return>", lambda _e: close_with(accept_current))
        root.bind("<Escape>",         lambda _e: close_with(self._on_ignore))


class _EditableToast:
    """
    Review popup for deliberate rephrasing.
    The suggestion is editable before replacement.

    answer_only=True turns it into an answer card for reading modes
    (What's This About?, Define This, …): no Replace button — the output is an
    answer about the text, not a replacement for it — Copy becomes the primary
    action, and the follow-up chat asks about the ORIGINAL passage rather than
    editing the shown text.
    """
    def __init__(self, corrected: str, on_accept, on_ignore, label: str = "Writing suggestion",
                 original: str = "", fmt: str = "", answer_only: bool = False,
                 cfg: dict | None = None):
        self._corrected   = corrected
        self._original    = original
        self._on_accept   = on_accept
        self._on_ignore   = on_ignore
        self._label       = label
        self._answer_only = answer_only
        self._fmt       = fmt or DEFAULT_CFG["prompt_format"]
        self._cfg       = cfg
        self._close_fn   = None   # populated by _show(); enables external dismissal
        self._txt        = None   # populated by _show(); enables streaming updates
        self._notice_label = None
        self._notice_before = None
        self._replace_btn = None  # populated by _show(); disabled until streaming ends
        self._stop_btn    = None  # populated by _show(); enabled only while streaming
        _ui_call(self._show, wait=True)   # wait=True ensures _close_fn is set before __init__ returns

    def close(self):
        """Programmatically dismiss this toast (equivalent to clicking Ignore)."""
        if self._close_fn is not None:
            _ui_call(self._close_fn, wait=False)

    def show_model_switch(self, failed_model: str, next_model: str):
        """Show a cloud failover notice without adding it to the answer text."""
        message = f"⚠  {failed_model} hit a rate limit — using {next_model} instead"

        def _show_notice():
            label = self._notice_label
            if label is None:
                return
            try:
                label.config(text=message)
                if not label.winfo_manager() and self._notice_before is not None:
                    label.pack(anchor="w", pady=(2, 6), before=self._notice_before)
            except tk.TclError:
                pass

        _ui_call(_show_notice, wait=False)

    def set_text(self, text: str, select: bool = False):
        """Replace the editor contents from a worker thread.
        When select=True (streaming complete), select all text so it is
        ready to copy or accept.
        During streaming (select=False) plain text is shown as-is so chunks
        can be appended without re-parsing on every token.
        """
        def _set():
            if self._txt is None:
                return
            self._txt.delete("1.0", "end")
            if select:
                # Streaming done (or stopped early) — show the final/partial text,
                # select all, enable Replace, and retire the Stop button.
                if text:
                    self._txt.insert("end", text)
                    self._txt.tag_add("sel", "1.0", "end-1c")
                if self._replace_btn is not None:
                    self._replace_btn.config(state="normal")
                if self._stop_btn is not None:
                    self._stop_btn.config(state="disabled", text="■ Stop", fg="white")
            else:
                # First streamed chunk is arriving — a stream is now live, so the
                # Stop button becomes usable.
                self._txt.insert("1.0", text)
                self._txt.tag_remove("sel", "1.0", "end")
                if self._stop_btn is not None:
                    self._stop_btn.config(state="normal", text="■ Stop", fg="white")
            self._txt.see("end")

        _ui_call(_set, wait=False)

    def append_text(self, text: str):
        """Append streamed model text from a worker thread."""
        def _append():
            if self._txt is None:
                return
            self._txt.insert("end", text)
            self._txt.see("end")

        _ui_call(_append, wait=False)

    def _show(self):
        root = tk.Toplevel(ui_thread._UI_ROOT)
        root.title(self._label)
        root.attributes("-topmost", True)
        root.configure(bg=SURFACE)
        root.resizable(True, True)

        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        scale = 1.0 + (FONT_SIZE_OFFSET / 10.0) if FONT_SIZE_OFFSET > 0 else 1.0
        w = int(520 * scale)
        h = int(330 * scale)
        root.geometry(f"{w}x{h}+{sw - w - 30}+{sh - h - 70}")
        _apply_windows_decorations(root)

        tk.Frame(root, bg=ACCENT, height=3).pack(fill="x")

        body = tk.Frame(root, bg=SURFACE, padx=14, pady=10)
        body.pack(fill="both", expand=True)

        icon = "💡" if self._answer_only else "✏"
        tk.Label(body, text=f"{icon}  {self._label}",
                 font=("Segoe UI", 9 + FONT_SIZE_OFFSET, "bold"), bg=SURFACE, fg=ACCENT2).pack(anchor="w")
        hint = ("Read the answer below — copy it or ask a follow-up."
                if self._answer_only else
                "Edit the suggestion below, then choose Replace or Ignore.")
        tk.Label(body, text=hint,
                 font=FONT_B, bg=SURFACE, fg=MUTED).pack(anchor="w", pady=(2, 8))

        txt = tk.Text(body, font=("Segoe UI", 10 + FONT_SIZE_OFFSET), bg=BG, fg=TEXT,
                      insertbackground=ACCENT2, relief="flat", wrap="word",
                      height=6, padx=8, pady=8,
                      spacing1=2, spacing2=2, spacing3=4)
        txt.pack(fill="both", expand=True)
        txt.insert("end", self._corrected)
        txt.focus_set()
        txt.tag_add("sel", "1.0", "end-1c")
        self._txt = txt
        self._notice_before = txt
        self._notice_label = tk.Label(
            body, font=FONT_B, bg=SURFACE, fg=WARN, justify="left",
            wraplength=int(490 * scale),
        )

        btn_row = tk.Frame(body, bg=SURFACE)
        btn_row.pack(anchor="w", pady=(10, 0))

        closed = {"value": False}

        def accept():
            # Answer cards have no Replace: accepting would paste the answer
            # over the passage the user is reading.
            if closed["value"] or self._answer_only:
                return
            # Respect the disabled state even for keyboard shortcuts.
            # _replace_btn is None only if _show() hasn't finished yet, which
            # can't happen here (we're on the UI thread and _show() already ran).
            if self._replace_btn is not None and str(self._replace_btn.cget("state")) != "normal":
                return
            closed["value"] = True
            if _et_auto_close_id[0] is not None:
                try:
                    root.after_cancel(_et_auto_close_id[0])
                    _et_auto_close_id[0] = None
                except Exception:
                    pass
            # Stop any in-progress chat stream so _backend.lock is released promptly.
            _stream_cancel.set()
            edited = txt.get("1.0", "end-1c")
            self._txt = None          # prevent stale append_text() calls after close
            try:
                root.destroy()
            finally:
                self._on_accept(edited)

        def ignore():
            if closed["value"]:
                return
            closed["value"] = True
            if _et_auto_close_id[0] is not None:
                try:
                    root.after_cancel(_et_auto_close_id[0])
                    _et_auto_close_id[0] = None
                except Exception:
                    pass
            # Stop any in-progress chat stream so _backend.lock is released promptly.
            _stream_cancel.set()
            self._txt = None          # prevent stale append_text() calls after close
            try:
                root.destroy()
            finally:
                self._on_ignore()

        self._close_fn = ignore   # expose for external dismissal via .close()

        # Replace button starts disabled — enabled by set_text(..., select=True)
        # once streaming is complete, preventing a mid-stream accept from
        # capturing partial text as the final replacement.
        # Answer cards get no Replace button at all: the answer describes the
        # passage, it must never be pasted over it (self._replace_btn stays
        # None, which every _replace_btn access already guards against).
        replace_btn = None
        if not self._answer_only:
            replace_btn = tk.Button(btn_row, text="Replace  ✓",
                      font=("Segoe UI", 9 + FONT_SIZE_OFFSET, "bold"), bg=ACCENT, fg="white",
                      relief="flat", padx=10, pady=3, cursor="hand2",
                      state="disabled",
                      command=accept)
        self._replace_btn = replace_btn

        def _do_stop():
            # Halt the in-progress stream (initial rephrase or a chat reply) on its
            # next token WITHOUT discarding what has already been generated.  The
            # worker that owns the stream finalises the partial text via
            # set_text(..., select=True) / its _done handler, which re-enables
            # Replace and retires this button.
            _stream_cancel.set()
            stop_btn.config(state="disabled", text="Stopping…", fg=MUTED)

        stop_btn = tk.Button(btn_row, text="■ Stop",
                  font=FONT_B, bg=WARN, fg="white",
                  relief="flat", padx=10, pady=3, cursor="hand2",
                  command=_do_stop)
        self._stop_btn = stop_btn

        def _do_copy():
            # Copy the current editor contents to the clipboard WITHOUT closing the
            # toast, so the user can paste elsewhere and still edit / Replace / Ignore.
            if self._txt is None:
                return
            to_copy = self._txt.get("1.0", "end-1c")
            try:
                root.clipboard_clear()
                root.clipboard_append(to_copy)
                root.update()   # flush so the clipboard survives after the toast closes
            except Exception:
                pass
            copy_btn.config(text="Copied ✓")
            root.after(1500, lambda: copy_btn.config(text="Copy"))

        # On an answer card Copy is the primary (accent-coloured, first) action;
        # on a rewrite toast it stays secondary after Replace and Stop.
        copy_btn = tk.Button(btn_row, text="Copy",
                  font=("Segoe UI", 9 + FONT_SIZE_OFFSET, "bold") if self._answer_only else FONT_B,
                  bg=ACCENT if self._answer_only else SURF2,
                  fg="white" if self._answer_only else TEXT,
                  relief="flat", padx=10, pady=3, cursor="hand2",
                  command=_do_copy)

        if self._answer_only:
            copy_btn.pack(side="left")
            stop_btn.pack(side="left", padx=(8, 0))
        else:
            replace_btn.pack(side="left")
            stop_btn.pack(side="left", padx=(8, 0))
            copy_btn.pack(side="left", padx=(8, 0))

        tk.Button(btn_row, text="Close" if self._answer_only else "Ignore",
                  font=FONT_B, bg=SURF2, fg=MUTED,
                  relief="flat", padx=10, pady=3, cursor="hand2",
                  command=ignore).pack(side="left", padx=(8, 0))

        # ── Chat row ──────────────────────────────────────────────────────────
        tk.Frame(body, bg=SURF2, height=1).pack(fill="x", pady=(10, 6))

        # History: list of text snapshots, index of the one currently shown.
        # Starts with the original suggestion; each AI reply appends a new entry.
        # Entirely in-memory — cleared automatically when the toast closes.
        _et_history  = [self._corrected]
        _et_hist_idx = [0]

        chat_row = tk.Frame(body, bg=SURFACE)
        chat_row.pack(fill="x", pady=(0, 2))
        chat_row.columnconfigure(2, weight=1)   # col 2 = entry expands

        et_back_btn = tk.Button(chat_row, text="←", font=FONT_B, bg=SURF2, fg=MUTED,
                                relief="flat", padx=6, pady=2, cursor="hand2",
                                state="disabled")
        et_back_btn.grid(row=0, column=0, padx=(0, 2))

        et_fwd_btn = tk.Button(chat_row, text="→", font=FONT_B, bg=SURF2, fg=MUTED,
                               relief="flat", padx=6, pady=2, cursor="hand2",
                               state="disabled")
        et_fwd_btn.grid(row=0, column=1, padx=(0, 6))

        _chat_placeholder = (
            "Ask a follow-up…  (e.g. explain more, give an example)"
            if self._answer_only else
            "Ask AI…  (e.g. rephrase that, make it shorter, explain)")

        chat_entry = tk.Entry(chat_row, font=FONT_B, bg=BG, fg=MUTED,
                              insertbackground=ACCENT2, relief="flat")
        chat_entry.insert(0, _chat_placeholder)
        chat_entry.grid(row=0, column=2, sticky="ew", ipady=4, padx=(0, 6))

        send_btn = tk.Button(chat_row, text="Send", font=FONT_B, bg=ACCENT2, fg="white",
                             relief="flat", padx=8, pady=2, cursor="hand2")
        send_btn.grid(row=0, column=3)

        def _et_refresh_nav():
            """Update back/forward button states to match current history position."""
            can_back = _et_hist_idx[0] > 0
            can_fwd  = _et_hist_idx[0] < len(_et_history) - 1
            et_back_btn.config(state="normal" if can_back else "disabled",
                               fg=TEXT      if can_back else MUTED)
            et_fwd_btn.config( state="normal" if can_fwd  else "disabled",
                               fg=TEXT      if can_fwd  else MUTED)

        def _et_show_history(idx: int):
            """Display a specific history entry in the editor."""
            _et_hist_idx[0] = idx
            entry = _et_history[idx]
            if self._txt is not None:
                self._txt.delete("1.0", "end")
                self._txt.insert("1.0", entry)
                self._txt.config(fg=TEXT)
                self._txt.see("1.0")
            _et_refresh_nav()

        et_back_btn.config(command=lambda: _et_show_history(_et_hist_idx[0] - 1))
        et_fwd_btn.config( command=lambda: _et_show_history(_et_hist_idx[0] + 1))

        def _on_chat_focus_in(event):
            if chat_entry.get() == _chat_placeholder:
                chat_entry.delete(0, "end")
                chat_entry.config(fg=TEXT)

        def _on_chat_focus_out(event):
            if not chat_entry.get().strip():
                chat_entry.delete(0, "end")
                chat_entry.insert(0, _chat_placeholder)
                chat_entry.config(fg=MUTED)

        chat_entry.bind("<FocusIn>",  _on_chat_focus_in)
        chat_entry.bind("<FocusOut>", _on_chat_focus_out)

        def _do_chat_editable(instruction: str):
            """Run the LLM on the current editor contents and stream the result back."""
            def _worker():
                # Clear any stale cancel signal from a previously dismissed toast
                # before starting a new stream.  Without this, chat_with_text_stream()
                # would return immediately on its first token check and produce no output.
                _stream_cancel.clear()
                # Snapshot the current editor text to use as the base
                current_text = [None]
                def _snap():
                    current_text[0] = txt.get("1.0", "end-1c") if self._txt is not None else ""
                _ui_call(_snap, wait=True)
                base = current_text[0] or ""
                # Answer cards: follow-up questions are about the ORIGINAL
                # passage the reader selected, not about the answer shown in
                # the editor ("give an example", "explain the second part").
                # `base` still holds the editor snapshot for history purposes.
                llm_base = (self._original
                            if (self._answer_only and self._original)
                            else base)

                # Disable Replace while the AI is working to prevent mid-stream accepts.
                def _lock_ui():
                    if self._replace_btn is not None:
                        self._replace_btn.config(state="disabled")
                    if self._stop_btn is not None:
                        self._stop_btn.config(state="normal", text="■ Stop", fg="white")
                    if self._txt is not None:
                        self._txt.delete("1.0", "end")
                        self._txt.insert("1.0", "⏳ Thinking…")
                        self._txt.config(fg=MUTED)
                _ui_call(_lock_ui, wait=False)

                try:
                    chunks = []
                    last_render = [None]
                    for piece in chat_with_text_stream(
                            llm_base, instruction, self._fmt, cfg=self._cfg,
                            on_fallback=self.show_model_switch):
                        chunks.append(piece)
                        # Hide chain-of-thought while streaming: show "Thinking…"
                        # until the answer emerges, then render the answer-so-far.
                        visible, thinking = _streaming_answer("".join(chunks))
                        render = visible or "💭 Thinking…"
                        if render == last_render[0]:
                            continue
                        last_render[0] = render
                        def _render(t=render, dim=(not visible)):
                            if self._txt is not None:
                                self._txt.delete("1.0", "end")
                                self._txt.insert("1.0", t)
                                self._txt.config(fg=MUTED if dim else TEXT)
                                self._txt.see("end")
                        _ui_call(_render, wait=False)

                    result = _clean_llm_output("".join(chunks).strip())
                    if not result:
                        result = base

                    def _done(r=result, b=base):
                        # Update the current slot with the actual text that was in the
                        # editor before this chat ran. This matters because the slot may
                        # still hold the original placeholder ("Generating suggestion…")
                        # from before the rephrase pipeline finished streaming — the
                        # widget was updated live but _et_history[0] was never patched.
                        _et_history[_et_hist_idx[0]] = b
                        # Discard any forward history beyond current position, then push.
                        del _et_history[_et_hist_idx[0] + 1:]
                        _et_history.append(r)
                        _et_hist_idx[0] = len(_et_history) - 1
                        if self._txt is not None:
                            self._txt.delete("1.0", "end")
                            self._txt.insert("1.0", r)
                            self._txt.config(fg=TEXT)
                            self._txt.tag_add("sel", "1.0", "end-1c")
                            self._txt.see("end")
                        if self._replace_btn is not None:
                            self._replace_btn.config(state="normal")
                        if self._stop_btn is not None:
                            self._stop_btn.config(state="disabled", text="■ Stop", fg="white")
                        _et_refresh_nav()
                        chat_entry.config(state="normal")
                        send_btn.config(state="normal")
                        chat_entry.delete(0, "end")
                        chat_entry.focus_set()
                    _ui_call(_done, wait=False)

                except Exception as exc:
                    if isinstance(exc, CLIModelError):
                        _info_toast(f"AI CLI error: {exc}")
                    def _err():
                        if self._txt is not None:
                            self._txt.delete("1.0", "end")
                            self._txt.insert("1.0", base)
                            self._txt.config(fg=ERR)
                        if self._replace_btn is not None:
                            self._replace_btn.config(state="normal")
                        if self._stop_btn is not None:
                            self._stop_btn.config(state="disabled", text="■ Stop", fg="white")
                        chat_entry.config(state="normal")
                        send_btn.config(state="normal")
                    _ui_call(_err, wait=False)

            threading.Thread(target=_worker, daemon=True).start()

        def _send_chat(event=None):
            instruction = chat_entry.get().strip()
            if not instruction or instruction == _chat_placeholder:
                return
            chat_entry.config(state="disabled")
            send_btn.config(state="disabled")
            _do_chat_editable(instruction)

        send_btn.config(command=_send_chat)
        chat_entry.bind("<Return>", _send_chat)

        root.protocol("WM_DELETE_WINDOW", ignore)
        # Ctrl+Enter: Replace on a rewrite toast, Copy on an answer card
        # (accept() is a guarded no-op there — nothing to replace).
        root.bind("<Control-Return>",
                  lambda _e: (_do_copy() if self._answer_only else accept()))
        root.bind("<Escape>", lambda _e: ignore())

        # Auto-close after 60 s so the background LLM thread is never blocked forever.
        # The id is stored so hover protection and explicit close can cancel it.
        _et_auto_close_id = [root.after(60_000, ignore)]

        # ── Hover protection ──────────────────────────────────────────────────
        # Cancel the auto-close timer permanently while the cursor is inside the
        # popup (user is actively reviewing/editing — don't pull the rug out).
        # The safety-net timer is not restarted on leave; close manually or wait
        # for a fresh 60 s window that begins only when the cursor re-enters and
        # then leaves again.
        def _on_et_enter(event=None):
            if _et_auto_close_id[0] is not None:
                try:
                    root.after_cancel(_et_auto_close_id[0])
                    _et_auto_close_id[0] = None
                except Exception:
                    pass

        def _on_et_leave(event=None):
            if closed["value"] or _et_auto_close_id[0] is not None:
                return
            w = root.winfo_containing(event.x_root, event.y_root)
            if w is None or not str(w).startswith(str(root)):
                # Cursor truly left — reinstate a generous 60 s safety net.
                _et_auto_close_id[0] = root.after(60_000, ignore)

        root.bind("<Enter>", _on_et_enter, add="+")
        root.bind("<Leave>", _on_et_leave, add="+")


# ── Recent Assistant results popup ───────────────────────────────────────────

def _open_recent_rephrases():
    """
    Open a small window showing the last (up to 3) Assistant results.
    Each entry shows the mode, a snippet of the original text, the full
    rephrased output, and a Copy button so the user can paste it anywhere.
    Accessible via the tray icon right-click menu.
    """
    def _show():
        if not _rephrase_history:
            messagebox.showinfo(
                "Recent Assistant Results",
                "No Assistant results yet.\n\nUse Understand or Improve Writing "
                "to generate one.",
                parent=ui_thread._UI_ROOT,
            )
            return

        win = tk.Toplevel(ui_thread._UI_ROOT)
        win.title("Recent Assistant Results")
        win.attributes("-topmost", True)
        win.configure(bg=BG)
        win.resizable(True, True)
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        scale = 1.0 + (FONT_SIZE_OFFSET / 10.0) if FONT_SIZE_OFFSET > 0 else 1.0
        w = int(500 * scale)
        h = int(400 * scale)
        win.geometry(f"{w}x{h}+{sw - w - 30}+{sh - h - 60}")

        tk.Frame(win, bg=ACCENT, height=3).pack(fill="x")

        header = tk.Frame(win, bg=BG, padx=14, pady=10)
        header.pack(fill="x")
        tk.Label(header, text="↩  Recent Assistant Results",
                 font=("Segoe UI", 10 + FONT_SIZE_OFFSET, "bold"), bg=BG, fg=ACCENT2).pack(anchor="w")
        tk.Label(header,
                 text="Last 3 Assistant results.  Click Copy to grab any of them.",
                 font=FONT_B, bg=BG, fg=MUTED).pack(anchor="w", pady=(2, 0))

        tk.Frame(win, bg=SURF2, height=1).pack(fill="x")

        # Scrollable body
        canvas = tk.Canvas(win, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        body = tk.Frame(canvas, bg=BG, padx=14, pady=10)
        canvas_win = canvas.create_window((0, 0), window=body, anchor="nw")

        def _on_body_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
        def _on_canvas_configure(event):
            canvas.itemconfig(canvas_win, width=event.width)
        body.bind("<Configure>", _on_body_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        for i, entry in enumerate(_rephrase_history):
            if i > 0:
                tk.Frame(body, bg=SURF2, height=1).pack(fill="x", pady=(4, 8))

            # ── Mode + source snippet ─────────────────────────────────────────
            meta_row = tk.Frame(body, bg=BG)
            meta_row.pack(fill="x", anchor="w")
            tk.Label(meta_row, text=f"[{entry['mode']}]",
                     font=("Segoe UI", 8 + FONT_SIZE_OFFSET, "bold"), bg=BG, fg=ACCENT2).pack(side="left")
            src = entry.get("source", "").strip()
            if src:
                snippet = src[:48] + ("…" if len(src) > 48 else "")
                tk.Label(meta_row, text=f'  ← "{snippet}"',
                         font=("Segoe UI", 8 + FONT_SIZE_OFFSET), bg=BG, fg=MUTED).pack(side="left")

            # ── Rephrased text (read-only) ────────────────────────────────────
            txt = tk.Text(body, font=("Segoe UI", 9 + FONT_SIZE_OFFSET), bg=SURFACE, fg=TEXT,
                          relief="flat", wrap="word", height=4, padx=8, pady=6)
            txt.insert("1.0", entry["text"])
            txt.config(state="disabled")
            txt.pack(fill="x", pady=(4, 0))

            # ── Copy button ───────────────────────────────────────────────────
            btn_row = tk.Frame(body, bg=BG)
            btn_row.pack(fill="x", anchor="e", pady=(4, 2))

            _copied_lbl = tk.Label(btn_row, text="", font=("Segoe UI", 8 + FONT_SIZE_OFFSET),
                                   bg=BG, fg=SUCCESS)
            _copied_lbl.pack(side="right", padx=(0, 6))

            def _copy(t=entry["text"], lbl=_copied_lbl):
                win.clipboard_clear()
                win.clipboard_append(t)
                win.update()
                lbl.config(text="Copied ✓")
                win.after(2000, lambda: lbl.config(text=""))

            tk.Button(btn_row, text="Copy",
                      font=FONT_B, bg=ACCENT2, fg="white",
                      relief="flat", padx=10, pady=2, cursor="hand2",
                      command=_copy).pack(side="right")

        # ── Close button ──────────────────────────────────────────────────────
        tk.Frame(win, bg=SURF2, height=1).pack(fill="x", side="bottom")
        tk.Button(win, text="Close", font=FONT_B, bg=SURF2, fg=MUTED,
                  relief="flat", padx=14, pady=6, cursor="hand2",
                  command=win.destroy).pack(side="bottom", pady=8)

        win.lift()
        win.focus_force()

    _ui_call(_show, wait=False)


def _info_toast(message: str, duration_ms: int = 2200):
    """Show a brief, auto-dismissing status notification (no buttons).

    Used to give feedback on hotkey presses that would otherwise fail silently —
    e.g. when there is no selected text and the typed-sentence buffer is empty.
    Scheduled on the UI thread so it is safe to call from any background thread.

    NOTE: this is a self-contained notification, NOT the _Toast suggestion popup
    (which needs original/corrected text and accept/ignore callbacks).
    """
    def _show():
        root = tk.Toplevel(ui_thread._UI_ROOT)
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


def _warn_toast(message: str, duration_ms: int = 2500):
    """Auto-dismissing warning notification (shares _info_toast's plain styling)."""
    _info_toast(message, duration_ms)


def _error_toast(message: str, duration_ms: int = 3500):
    """Auto-dismissing error notification (shares _info_toast's plain styling)."""
    _info_toast(message, duration_ms)


def _toast(message: str):
    _info_toast(message)
