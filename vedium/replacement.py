# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

import sys
import time
import tkinter as tk
from pynput.keyboard import Controller as KbController
from pynput.keyboard import Key

from . import diagnostics
from .constants import _CMD_KEY
from .ui_thread import _ui_call
from . import ui_thread

_kbctrl = KbController()


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
        import ctypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        return int(hwnd) if hwnd else None
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
    if not expected_window or sys.platform != "win32":
        return
    current = _active_window_token()
    if current and current != expected_window:
        diagnostics.record(
            "replacement", "warning",
            "Replacement blocked because focus moved to another window.",
            "Use Copy if you want to paste the suggestion manually.",
        )
        raise RuntimeError("Focus moved before replacement; refusing to paste.")


def _copy_selected_text() -> tuple[str | None, str]:
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
            _kbctrl.press('c')
            _kbctrl.release('c')
        time.sleep(0.12)

        copied = _clipboard_get_text()
        if copied and copied != sentinel:
            return copied, previous_clipboard
        return None, previous_clipboard
    finally:
        try:
            _clipboard_set_text(previous_clipboard)
        except Exception as exc:
            diagnostics.record("clipboard", "warning", "Clipboard restore failed.", str(exc))


def _paste_text_temporarily(text: str, previous_clipboard: str):
    """Paste text into the active app, then restore the previous clipboard."""
    try:
        _clipboard_set_text(text)
        time.sleep(0.05)
        with _kbctrl.pressed(_CMD_KEY):
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


def _do_replace(original: str, corrected: str, punct: str, post: str, raw_len: int,
                leading_ws: str = "",
                from_toast: bool = False,
                expected_window: int | None = None):
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

    time.sleep(0.15 if from_toast else 0.05)
    _ensure_expected_window(expected_window)

    if from_toast:
        for _mod in (Key.ctrl, Key.ctrl_l, Key.ctrl_r,
                     Key.cmd,  Key.cmd_l,  Key.cmd_r):
            try:
                _kbctrl.release(_mod)
            except Exception:
                pass

    with _kbctrl.pressed(Key.shift):
        for _ in range(total_back):
            _kbctrl.press(Key.left)
            _kbctrl.release(Key.left)

    time.sleep(0.02)
    prev_clip = _clipboard_get_text()
    try:
        _clipboard_set_text(full_new)
        time.sleep(0.05)
        with _kbctrl.pressed(_CMD_KEY):
            _kbctrl.press('v')
            _kbctrl.release('v')
        time.sleep(0.10)
    finally:
        try:
            _clipboard_set_text("" if from_toast else prev_clip)
        except Exception as exc:
            diagnostics.record("clipboard", "warning", "Clipboard restore failed.", str(exc))


def _join_rephrase_text(corrected: str, post: str) -> str:
    """Join rephrased/translated text + later typed text with safe spacing."""
    if corrected and post and corrected[-1] in ".!?" and post[0].isalnum():
        return corrected + " " + post
    return corrected + post


def _do_replace_rephrase(original: str, corrected: str, post: str, raw_len: int,
                          from_toast: bool = True,
                          expected_window: int | None = None):
    """
    Select (raw_len + len(post)) characters backwards and overwrite with the
    rephrased version.
    """
    if not corrected.strip():
        diagnostics.record("replacement", "warning", "Rephrase replacement blocked because the suggestion was empty.")
        raise ValueError("Refusing to replace text with an empty suggestion.")

    full_back = raw_len + len(post)
    full_new  = _join_rephrase_text(corrected, post)

    time.sleep(0.15 if from_toast else 0.05)
    _ensure_expected_window(expected_window)

    if from_toast:
        for _mod in (Key.ctrl, Key.ctrl_l, Key.ctrl_r,
                     Key.cmd,  Key.cmd_l,  Key.cmd_r):
            try:
                _kbctrl.release(_mod)
            except Exception:
                pass

    with _kbctrl.pressed(Key.shift):
        for _ in range(full_back):
            _kbctrl.press(Key.left)
            _kbctrl.release(Key.left)

    time.sleep(0.02)
    prev_clip = _clipboard_get_text()
    try:
        _clipboard_set_text(full_new)
        time.sleep(0.05)
        with _kbctrl.pressed(_CMD_KEY):
            _kbctrl.press('v')
            _kbctrl.release('v')
        time.sleep(0.10)
    finally:
        try:
            _clipboard_set_text("" if from_toast else prev_clip)
        except Exception as exc:
            diagnostics.record("clipboard", "warning", "Clipboard restore failed.", str(exc))


def _do_replace_selected_text(replacement: str, previous_clipboard: str,
                              expected_window: int | None = None):
    """Replace the current selection with replacement text via temporary paste."""
    if not replacement.strip():
        diagnostics.record("replacement", "warning", "Selected-text replacement blocked because the suggestion was empty.")
        raise ValueError("Refusing to replace text with an empty suggestion.")
    time.sleep(0.12)
    _ensure_expected_window(expected_window)
    _paste_text_temporarily(replacement, previous_clipboard)
