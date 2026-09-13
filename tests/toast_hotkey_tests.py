# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Regression checks for configurable, non-blocking spell-review toasts."""

from pathlib import Path
import sys
import threading
from unittest import mock

from pynput.keyboard import Key, KeyCode

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vedium.input_context import InputContext
from vedium.buffer import _Buffer
from vedium import daemon
from vedium.daemon import _matches_toast_accept_hotkey
from vedium.hotkeys import (
    normalize_toast_accept_hotkey,
    toast_accept_hotkey_tk_sequence,
)
from vedium.constants import DEFAULT_TRIGGER_CHARS


def test_shortcut_normalization_and_matching() -> None:
    assert normalize_toast_accept_hotkey("ALT+SHIFT+ENTER") == "alt+shift+enter"
    assert normalize_toast_accept_hotkey("unsupported") == "ctrl+enter"
    assert toast_accept_hotkey_tk_sequence("ctrl+shift+enter").endswith(
        "-Shift-Return>"
    )
    assert _matches_toast_accept_hotkey(
        Key.enter, "alt+enter", ctrl_held=False, alt_held=True, shift_held=False,
    )
    assert not _matches_toast_accept_hotkey(
        Key.enter, "alt+enter", ctrl_held=True, alt_held=True, shift_held=False,
    )


def test_pending_review_keeps_collecting_and_queues_next_sentence() -> None:
    buf = _Buffer()
    for char in "First sentense":
        buf.key_char(char)
    assert buf.trigger(".", min_words=2)

    # A visible review toast must leave the pending buffer writable.
    for char in " Next sentense":
        buf.key_char(char)
    second = buf.trigger(".", min_words=2)
    assert second and second[0] == "Next sentense"
    assert len(buf._queued_post_boundaries) == 1


def test_backspace_retracts_trigger_and_allows_fresh_toast_job() -> None:
    for trigger in ".!?,;:":
        buf = _Buffer()
        for char in "This is a mistke":
            buf.key_char(char)
        stale = buf.trigger_with_epoch(trigger, min_words=2)
        assert stale is not None

        # The first Backspace deletes the trigger from the real document. Vedium
        # must restore the source sentence and invalidate the stale correction.
        assert buf.key_backspace() is True
        assert buf.peek_sentence() == "This is a mistke"
        assert buf.epoch() > stale[-1]
        assert not buf.is_busy_or_pending()

        for _ in "mistke":
            assert buf.key_backspace() is False
        for char in "mistake":
            buf.key_char(char)
        fresh = buf.trigger_with_epoch(trigger, min_words=2)
        assert fresh is not None
        assert fresh[0] == "This is a mistake"


def test_short_edited_fragment_does_not_merge_into_next_sentence() -> None:
    buf = _Buffer()
    for char in "First wrong":
        buf.key_char(char)
    first = buf.trigger_with_epoch(".", min_words=2)
    assert first is not None

    # A one-word fragment is below the trigger threshold while the first job
    # is pending, but it still exists in the document and post buffer.
    for char in " mistake":
        buf.key_char(char)
    assert buf.trigger_with_epoch(".", min_words=2) is None
    buf.finalize(replaced=False, expected_epoch=first[-1])

    for char in " This is another mistke":
        buf.key_char(char)
    next_job = buf.trigger_with_epoch(".", min_words=2)
    assert next_job is not None
    assert next_job[0] == "This is another mistke"
    assert next_job[3] == " "


def test_backspace_retracts_newest_queued_sentence_boundary() -> None:
    buf = _Buffer()
    for char in "First wrong":
        buf.key_char(char)
    stale = buf.trigger_with_epoch(".", min_words=2)
    assert stale is not None
    for char in " Second wrong":
        buf.key_char(char)
    assert buf.trigger_with_epoch(".", min_words=2)

    assert buf.key_backspace() is True
    assert buf.peek_sentence() == " Second wrong"
    assert buf.epoch() > stale[-1]
    assert not buf.is_busy_or_pending()


def test_daemon_dismisses_visible_toast_when_trigger_is_deleted() -> None:
    app = daemon.SpellDaemon.__new__(daemon.SpellDaemon)
    app.cfg = {
        "enabled": True,
        "idle_reset_sec": 30.0,
        "understand_hotkey": "",
        "writing_hotkey": "",
        "mode_hotkeys": {},
        "toast_accept_hotkey": "ctrl+enter",
    }
    app.buf = _Buffer()
    for char in "This is wrong":
        app.buf.key_char(char)
    assert app.buf.trigger_with_epoch(".", min_words=2)

    class VisibleToast:
        ignored = False

        def trigger_ignore(self):
            self.ignored = True

    toast = VisibleToast()
    app._active_spell_toast_lock = threading.Lock()
    app._active_spell_toast = toast
    app._active_rephrase_lock = threading.Lock()
    app._active_rephrase_toast = None
    app._foreground_lock = threading.Lock()
    app._buffer_window = 101
    app._buffer_context = InputContext(101, 99999, (1,), False, True)
    app._ctrl_held = app._alt_held = app._shift_held = False

    with (
        mock.patch.object(daemon, "_active_window_token", return_value=101),
        mock.patch.object(daemon, "focused_context", return_value=app._buffer_context),
        mock.patch.object(daemon, "_window_process_id", return_value=99999),
    ):
        app._on_press_impl(Key.backspace)

    assert toast.ignored
    assert app.buf.peek_sentence() == "This is wrong"
    assert not app.buf.is_busy_or_pending()


def test_new_sentence_advances_past_untouched_toast_immediately() -> None:
    assert DEFAULT_TRIGGER_CHARS == ".!?,"
    app = daemon.SpellDaemon.__new__(daemon.SpellDaemon)
    app.cfg = {
        "enabled": True,
        "idle_reset_sec": 30.0,
        "understand_hotkey": "",
        "writing_hotkey": "",
        "mode_hotkeys": {},
        "toast_accept_hotkey": "ctrl+enter",
        "spell_check_mode": "Toast",
        "trigger_chars": DEFAULT_TRIGGER_CHARS,
        "min_word_count": 2,
    }
    app.buf = _Buffer()
    for char in "First wrong":
        app.buf.key_char(char)
    assert app.buf.trigger_with_epoch(".", min_words=2)
    for char in " Second wrong":
        app.buf.key_char(char)

    class UntouchedToast:
        ignored = False

        def trigger_ignore(self):
            self.ignored = True

    toast = UntouchedToast()
    app._active_spell_toast_lock = threading.Lock()
    app._active_spell_toast = toast
    app._active_rephrase_lock = threading.Lock()
    app._active_rephrase_toast = None
    app._foreground_lock = threading.Lock()
    app._buffer_window = 101
    app._buffer_context = InputContext(101, 99999, (1,), False, True)
    app._ctrl_held = app._alt_held = app._shift_held = False
    enqueued = []
    app._enqueue_sentence = lambda *args, **kwargs: enqueued.append((args, kwargs))

    with (
        mock.patch.object(daemon, "_active_window_token", return_value=101),
        mock.patch.object(daemon, "focused_context", return_value=app._buffer_context),
        mock.patch.object(daemon, "_window_process_id", return_value=99999),
    ):
        app._on_press_impl(KeyCode.from_char("."))

    assert enqueued and enqueued[0][0][0] == "Second wrong"
    assert toast.ignored


def test_spell_pipeline_does_not_block_typing_while_toast_is_opened() -> None:
    app = daemon.SpellDaemon.__new__(daemon.SpellDaemon)
    app.cfg = {
        "spell_check_mode": "Toast",
        "working_language": "English",
        "diacritics_only_auto": False,
        "trigger_chars": ".!?",
        "max_wait_sec": 12,
        "prompt_format": "Plain  (no template)",
    }
    app.buf = _Buffer()
    app._active_spell_toast_lock = threading.Lock()
    app._active_spell_toast = None

    for char in "First sentense":
        app.buf.key_char(char)
    first = app.buf.trigger_with_epoch(".", min_words=2)
    assert first is not None
    sentence, punct, raw_len, leading_ws, epoch = first
    observed = {}

    class FakeToast:
        def __init__(self, _original, _corrected, _accept, ignore, **_kwargs):
            observed["busy_when_shown"] = app.buf._busy
            for char in " Next sentense":
                app.buf.key_char(char)
            observed["second"] = app.buf.trigger(".", min_words=2)
            ignore()

        def show_model_switch(self, *_args):
            return None

    with (
        mock.patch.object(daemon, "_Toast", FakeToast),
        mock.patch.object(daemon, "spell_check_sentence", return_value="First sentence"),
        mock.patch.object(daemon, "is_same_language", return_value=True),
        mock.patch.object(
            daemon, "_safe_model_correction",
            side_effect=lambda _original, corrected, **_kwargs: corrected,
        ),
    ):
        app._pipeline(
            sentence, punct, raw_len, leading_ws,
            context_epoch=epoch,
        )

    assert observed["busy_when_shown"] is False
    assert observed["second"] and observed["second"][0] == "Next sentense"
    assert app.buf.is_busy_or_pending()  # the queued second sentence is now pending


def main() -> int:
    tests = [
        test_shortcut_normalization_and_matching,
        test_pending_review_keeps_collecting_and_queues_next_sentence,
        test_backspace_retracts_trigger_and_allows_fresh_toast_job,
        test_short_edited_fragment_does_not_merge_into_next_sentence,
        test_backspace_retracts_newest_queued_sentence_boundary,
        test_daemon_dismisses_visible_toast_when_trigger_is_deleted,
        test_new_sentence_advances_past_untouched_toast_immediately,
        test_spell_pipeline_does_not_block_typing_while_toast_is_opened,
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
