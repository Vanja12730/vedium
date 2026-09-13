# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Regression checks for foreground-window and disable-time buffer erasure."""

import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vedium.input_context import InputContext
from vedium.buffer import _Buffer
from vedium import daemon


def _bare_daemon() -> daemon.SpellDaemon:
    app = daemon.SpellDaemon.__new__(daemon.SpellDaemon)
    app.cfg = {"enabled": True}
    app.buf = _Buffer()
    app._foreground_lock = threading.Lock()
    app._buffer_window = None
    app._buffer_context = None
    app._ctrl_held = app._alt_held = app._shift_held = False
    app._active_spell_toast_lock = threading.Lock()
    app._active_spell_toast = None
    app._active_rephrase_lock = threading.Lock()
    app._active_rephrase_toast = None
    return app


def test_clear_erases_all_text_and_invalidates_late_jobs() -> None:
    buf = _Buffer()
    for char in "private words":
        buf.key_char(char)
    result = buf.trigger_with_epoch(".", min_words=2)
    assert result is not None
    old_epoch = result[-1]
    for char in " post text":
        buf.key_char(char)

    new_epoch = buf.clear()
    assert new_epoch > old_epoch
    assert buf.peek_sentence() == ""
    assert buf.post_text() == ""
    assert not buf.is_busy_or_pending()

    for char in "new window":
        buf.key_char(char)
    assert not buf.finalize(replaced=False, expected_epoch=old_epoch)
    assert buf.peek_sentence() == "new window"
    assert buf.snapshot_post_and_set_busy(old_epoch) is None


def test_foreground_change_clears_before_new_window_input() -> None:
    app = _bare_daemon()
    active = [101]
    with (
        mock.patch.object(daemon, "_active_window_token", side_effect=lambda: active[0]),
        mock.patch.object(daemon, "focused_context", side_effect=lambda: InputContext(active[0], 99999, (active[0],), False, True)),
        mock.patch.object(daemon, "_window_process_id", return_value=99999),
    ):
        app._sync_foreground_typing_context()
        for char in "sensitive text":
            app.buf.key_char(char)
        old_epoch = app.buf.epoch()

        active[0] = 202
        app._sync_foreground_typing_context()
        assert app.buf.peek_sentence() == ""
        assert app.buf.epoch() > old_epoch
        assert app._buffer_window == 202


def test_own_review_toast_does_not_cancel_deliberate_action() -> None:
    app = _bare_daemon()
    app._buffer_window = 101
    app._active_spell_toast = object()
    for char in "draft sentence":
        app.buf.key_char(char)
    old_epoch = app.buf.epoch()

    with (
        mock.patch.object(daemon, "_active_window_token", return_value=303),
        mock.patch.object(daemon, "_window_process_id", return_value=os.getpid()),
    ):
        app._sync_foreground_typing_context()

    assert app.buf.peek_sentence() == "draft sentence"
    assert app.buf.epoch() == old_epoch
    assert app._buffer_window == 101


def test_disabling_clears_immediately() -> None:
    app = _bare_daemon()
    app._buffer_window = 101
    for char in "do not retain this":
        app.buf.key_char(char)
    old_epoch = app.buf.epoch()
    icon = SimpleNamespace(icon=None, title="")

    with (
        mock.patch.object(daemon, "save_cfg"),
        mock.patch.object(daemon, "_make_icon", return_value=object()),
        mock.patch.object(daemon.diagnostics, "set_status"),
        mock.patch.object(daemon.diagnostics, "record"),
    ):
        app._toggle(icon, None)

    assert app.cfg["enabled"] is False
    assert app.buf.peek_sentence() == ""
    assert app.buf.post_text() == ""
    assert app.buf.epoch() > old_epoch
    assert app._buffer_window is None


def main() -> int:
    tests = [
        test_clear_erases_all_text_and_invalidates_late_jobs,
        test_foreground_change_clears_before_new_window_input,
        test_own_review_toast_does_not_cancel_deliberate_action,
        test_disabling_clears_immediately,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
