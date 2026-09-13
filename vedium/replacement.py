# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

import sys
import time
import threading
from functools import wraps
import tkinter as tk
from pynput.keyboard import Controller as KbController
from pynput.keyboard import Key

from . import diagnostics
from .constants import _CMD_KEY
from .ui_thread import _ui_call
from . import ui_thread
from .input_context import focused_context, foreground_window

_kbctrl = KbController()
_clipboard_lock = threading.RLock()
_SOURCE_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


class ReplacementAborted(RuntimeError):
    """The source can no longer be safely copied or replaced."""

    def __init__(self, message: str, *, reason: str = "context"):
        super().__init__(message)
        self.reason = reason


def _serialized(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        with _clipboard_lock:
            return fn(*args, **kwargs)
    return call


def _clipboard_get_text() -> str:
    """Read text from the system clipboard. Returns empty string on failure/non-text."""
    def _get():
        try:
            return ui_thread._UI_ROOT.clipboard_get()
        except tk.TclError:
            return ""
        except Exception:
            return ""

    return _ui_call(_get, wait=True) or ""


def _clipboard_set_text(text: str):
    """Set system clipboard text using the single Tk UI thread."""
    def _set():
        for attempt in range(6):
            try:
                ui_thread._UI_ROOT.clipboard_clear()
                ui_thread._UI_ROOT.clipboard_append(text)
                ui_thread._UI_ROOT.update_idletasks()
                return
            except tk.TclError:
                if attempt < 5:
                    time.sleep(0.05)
                else:
                    raise

    _ui_call(_set, wait=True)


def _active_window_token() -> int | None:
    """Return the active foreground window handle on Windows, otherwise None."""
    if sys.platform != "win32":
        return None
    try:
        return foreground_window()
    except Exception:
        return None


def _window_process_id(window: int | None) -> int | None:
    """Return the owning process ID for a Windows foreground-window token."""
    if not window or sys.platform != "win32":
        return None
    try:
        import ctypes
        process_id = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(
            ctypes.c_void_p(window), ctypes.byref(process_id)
        )
        return int(process_id.value) if process_id.value else None
    except Exception:
        return None


def _ensure_expected_window(expected_window: int | None):
    """Refuse to paste if focus moved away from the captured target window."""
    if sys.platform != "win32":
        return
    current = _active_window_token()
    if not expected_window or current != expected_window:
        diagnostics.record(
            "replacement", "warning",
            "Replacement blocked because focus moved to another window.",
            "Use Copy if you want to paste the suggestion manually.",
        )
        raise ReplacementAborted("Focus moved before replacement; refusing to paste.")


def _target_guard(expected_window, expected_context=None, validate=None):
    """Check field identity and job lifetime as well as the foreground window."""
    def check():
        _ensure_expected_window(expected_window)
        if expected_context is not None:
            current = focused_context()
            if (current is None or current.identity != expected_context.identity
                    or current.password is not False):
                raise ReplacementAborted("Input field changed; refusing to paste. Use Copy instead.")
        if validate is not None and not validate():
            raise ReplacementAborted("Source changed or Vedium was disabled; refusing to paste. Use Copy instead.")
        # Accessibility itself is a cross-process call. Check the window again
        # after it returns, immediately before emitting any document input.
        _ensure_expected_window(expected_window)
    return check


@_serialized
def _copy_selected_text(*, guard=None) -> tuple[str | None, str]:
    """
    Try to copy the currently selected text.
    Returns (selected_text_or_None, previous_clipboard_text).
    """
    previous_clipboard = _clipboard_get_text()
    sentinel = f"__SPELL_DAEMON_NO_SELECTION_{time.time_ns()}__"

    try:
        _clipboard_set_text(sentinel)
        time.sleep(0.03)
        with _kbctrl.pressed(_CMD_KEY):
            if guard:
                guard()
            _kbctrl.press('c')
            _kbctrl.release('c')
        # Copy is handled asynchronously by the target application. A fixed
        # 120ms sleep mistook a slow editor for a changed/missing selection.
        # Poll the same copy request; never retry keys or accept stale text.
        deadline = time.monotonic() + 0.75
        while time.monotonic() < deadline:
            time.sleep(0.025)
            if guard:
                guard()
            copied = _clipboard_get_text()
            if guard:
                guard()
            if copied and copied != sentinel:
                return copied, previous_clipboard
        return None, previous_clipboard
    finally:
        try:
            _clipboard_set_text(previous_clipboard)
        except Exception as exc:
            diagnostics.record("clipboard", "warning", "Clipboard restore failed.", str(exc))


def _paste_text_temporarily(text: str, previous_clipboard: str, *, guard):
    """Paste text into the active app, then restore the previous clipboard."""
    try:
        _clipboard_set_text(text)
        time.sleep(0.05)
        with _kbctrl.pressed(_CMD_KEY):
            guard()
            _kbctrl.press('v')
            _kbctrl.release('v')
        time.sleep(0.08)
    finally:
        try:
            _clipboard_set_text(previous_clipboard)
        except Exception as exc:
            diagnostics.record("clipboard", "warning", "Clipboard restore failed.", str(exc))


def _join_completed_sentence(corrected: str, punct: str, post: str) -> str:
    """Join corrected sentence + punctuation + later typed text with safe spacing."""
    if post and post[0].isalnum():
        return corrected + punct + " " + post
    return corrected + punct + post


def _collapse_selection_to_end():
    """Drop a Shift+Left selection, leaving the caret where it started."""
    _kbctrl.press(Key.right)
    _kbctrl.release(Key.right)


def _ensure_selection_matches(expected: str, *, guard, exact=False,
                              collapse_on_failure=True, unchanged_suffix: str = ""):
    """
    Refuse to paste unless the selection really holds the text Vedium means to
    overwrite.

    The selection length is derived from the keystrokes the daemon observed, so
    it goes silently wrong whenever the host application edits the document on
    its own -- Windows 11 Notepad turning "doesnt" into "doesn't", for example.
    The span then covers the wrong
    characters and pasting corrupts the surrounding text, so check first.

    Whitespace is ignored: whitespace inside the span cannot be reconstructed
    from the caller's arguments, and it is never what a host autocorrect
    rewrites.

    Captured keystrokes can differ in case (Windows modifier/Caps Lock state)
    or quote style from the document. Accept those differences character by
    character, only within the source being corrected. Later typing must match
    unchanged because it is carried over, not corrected. Length differences
    and different words still fail verification.

    Unreadable selections fail closed. Explicit selections require an exact
    match (apart from platform newline encoding), including their whitespace.
    """
    selected, _previous_clipboard = _copy_selected_text(guard=guard)
    normalize = ((lambda text: text.replace("\r\n", "\n")) if exact
                 else (lambda text: "".join(text.split())))
    if selected is not None and normalize(selected) == normalize(expected):
        return
    if selected is not None and not exact:
        actual, captured = normalize(selected), normalize(expected)
        source_end = len(captured) - len(normalize(unchanged_suffix))
        if (len(actual) == len(captured)
                and actual[source_end:] == captured[source_end:]
                and all(a.translate(_SOURCE_QUOTES).casefold()
                        == b.translate(_SOURCE_QUOTES).casefold()
                        for a, b in zip(actual[:source_end], captured[:source_end]))):
            diagnostics.record(
                "replacement", "info",
                "Captured sentence verified despite capitalization or quote-style differences.",
            )
            return
    if collapse_on_failure:
        guard()
        _collapse_selection_to_end()
    if selected is None:
        diagnostics.record(
            "replacement", "warning",
            "Replacement blocked because the selection could not be copied.",
            "The editor did not return readable selected text before the copy timeout.",
        )
        raise ReplacementAborted(
            "Selection could not be copied; refusing to paste.",
            reason="selection_unavailable",
        )
    # Record counts, never the user's document or clipboard contents. These
    # distinguish a wrong selection span from equal-length text differences
    # without claiming that the host's autocorrect caused the mismatch.
    diagnostics.record(
        "replacement", "warning",
        "Replacement blocked because the selected text did not match the captured source.",
        f"Compared characters: expected={len(normalize(expected))}, "
        f"selected={len(normalize(selected))}. "
        "The selection range or document text may have changed.",
    )
    raise ReplacementAborted(
        "Selected text did not match the captured source; refusing to paste.",
        reason="source_mismatch",
    )


@_serialized
def _do_replace(original: str, corrected: str, punct: str, post: str, raw_len: int,
                leading_ws: str = "",
                from_toast: bool = False,
                expected_window: int | None = None,
                expected_context=None, validate=None):
    """
    Replace the original sentence + trigger punctuation (and any text already
    typed after the trigger) in a single select-and-paste operation.
    """
    if not corrected.strip():
        diagnostics.record("replacement", "warning", "Replacement blocked because the suggestion was empty.")
        raise ValueError("Refusing to replace text with an empty suggestion.")

    if post and post[0].isalnum():
        joined_post = " " + post
    else:
        joined_post = post

    full_new   = leading_ws + corrected + punct + joined_post
    total_back = raw_len + 1 + len(post)
    expected_window = expected_window or _active_window_token()
    guard = _target_guard(expected_window, expected_context, validate)

    time.sleep(0.15 if from_toast else 0.05)
    guard()

    if from_toast:
        for _mod in (Key.ctrl, Key.ctrl_l, Key.ctrl_r,
                     Key.cmd,  Key.cmd_l,  Key.cmd_r):
            try:
                _kbctrl.release(_mod)
            except Exception:
                pass

    with _kbctrl.pressed(Key.shift):
        for _ in range(total_back):
            _ensure_expected_window(expected_window)
            if validate is not None and not validate():
                raise ReplacementAborted("Source changed during selection; refusing to paste.")
            _kbctrl.press(Key.left)
            _kbctrl.release(Key.left)

    time.sleep(0.02)
    _ensure_selection_matches(original + punct + post, guard=guard,
                              unchanged_suffix=post)
    prev_clip = _clipboard_get_text()
    _paste_text_temporarily(full_new, prev_clip, guard=guard)


def _join_rephrase_text(corrected: str, post: str) -> str:
    """Join rephrased/translated text + later typed text with safe spacing."""
    if corrected and post and corrected[-1] in ".!?" and post[0].isalnum():
        return corrected + " " + post
    return corrected + post


@_serialized
def _do_replace_rephrase(original: str, corrected: str, post: str, raw_len: int,
                          from_toast: bool = True,
                          expected_window: int | None = None,
                          expected_original: str | None = None,
                          expected_context=None, validate=None):
    """
    Select (raw_len + len(post)) characters backwards and overwrite with the
    rephrased version.
    """
    if not corrected.strip():
        diagnostics.record("replacement", "warning", "Rephrase replacement blocked because the suggestion was empty.")
        raise ValueError("Refusing to replace text with an empty suggestion.")

    full_back = raw_len + len(post)
    full_new  = _join_rephrase_text(corrected, post)
    expected_window = expected_window or _active_window_token()
    guard = _target_guard(expected_window, expected_context, validate)

    time.sleep(0.15 if from_toast else 0.05)
    guard()

    if from_toast:
        for _mod in (Key.ctrl, Key.ctrl_l, Key.ctrl_r,
                     Key.cmd,  Key.cmd_l,  Key.cmd_r):
            try:
                _kbctrl.release(_mod)
            except Exception:
                pass

    with _kbctrl.pressed(Key.shift):
        for _ in range(full_back):
            _ensure_expected_window(expected_window)
            if validate is not None and not validate():
                raise ReplacementAborted("Source changed during selection; refusing to paste.")
            _kbctrl.press(Key.left)
            _kbctrl.release(Key.left)

    time.sleep(0.02)
    # expected_original is the text as the user typed it; `original` may have had
    # an inline mode trigger stripped, which the document still holds.
    _ensure_selection_matches(
        (original if expected_original is None else expected_original) + post,
        guard=guard, unchanged_suffix=post,
    )
    prev_clip = _clipboard_get_text()
    _paste_text_temporarily(full_new, prev_clip, guard=guard)


@_serialized
def _do_replace_selected_text(replacement: str, previous_clipboard: str,
                              expected_window: int | None = None, *,
                              expected_original: str,
                              expected_context=None, validate=None):
    """Replace the current selection with replacement text via temporary paste."""
    if not replacement.strip():
        diagnostics.record("replacement", "warning", "Selected-text replacement blocked because the suggestion was empty.")
        raise ValueError("Refusing to replace text with an empty suggestion.")
    time.sleep(0.12)
    guard = _target_guard(expected_window, expected_context, validate)
    guard()
    _ensure_selection_matches(expected_original, guard=guard, exact=True,
                              collapse_on_failure=False)
    _paste_text_temporarily(replacement, previous_clipboard, guard=guard)
