# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Regression tests for privacy boundaries and destructive replacement races.

Only fake editors, metadata and model output are used; no real keys, clipboard,
credentials or provider requests are touched.
"""
from contextlib import ExitStack, contextmanager
from pathlib import Path
import sys
import os
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pynput.keyboard import Key, KeyCode
from vedium import daemon, input_context, replacement
from vedium.input_context import InputContext
from privacy_buffer_tests import _bare_daemon
from replacement_guard_tests import _FakeEditor, _wired

FIELD = InputContext(101, 99999, (1,), False, True)
OTHER_FIELD = InputContext(101, 99999, (2,), False, True)
PASSWORD = InputContext(101, 99999, (3,), True, True)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.app = _bare_daemon()
        self.app.cfg.update(spell_check_mode="Toast", backend_mode="cloud")
        self.app._buffer_window = 101
        self.app._buffer_context = FIELD
        self.context = FIELD
        self.jobs = []
        self.app._enqueue_sentence = lambda *args, **kw: self.jobs.append(args)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(daemon, "focused_context", side_effect=lambda: self.context))
        self.stack.enter_context(mock.patch.object(daemon, "_active_window_token", return_value=101))
        self.stack.enter_context(mock.patch.object(daemon, "window_at_point", return_value=101))
        self.stack.enter_context(mock.patch.object(daemon, "_window_process_id", return_value=99999))

    def type(self, text):
        for char in text:
            self.app._on_press_impl(KeyCode.from_char(char))

    def test_password_never_enters_buffer_or_cloud_job(self):
        self.context = PASSWORD
        self.type("FAKE secret password.")
        self.assertEqual(self.app.buf.peek_sentence(), "")
        self.assertEqual(self.jobs, [])
        self.app._on_press_impl(Key.tab)
        self.context = FIELD
        self.type("hello there.")
        self.assertEqual(self.jobs[0][0], "hello there")

    def test_same_window_different_field_clears_before_next_character(self):
        self.type("first field")
        old_epoch = self.app.buf.epoch()
        self.context = OTHER_FIELD
        self.type("second field.")
        self.assertEqual(self.jobs[0][0], "second field")
        self.assertGreater(self.app.buf.epoch(), old_epoch)

    def test_tab_and_shortcuts_clear_even_when_editor_identity_is_reused(self):
        self.type("old tab")
        self.app._on_press_impl(Key.tab)
        self.type("new tab.")
        self.assertEqual(self.jobs[0][0], "new tab")
        self.app._on_press_impl(Key.ctrl)
        self.app._on_press_impl(KeyCode.from_char("f"))
        self.assertEqual(self.app.buf.peek_sentence(), "")
        self.assertFalse(self.app.buf.is_busy_or_pending())

    def test_unknown_and_unsupported_password_metadata_fail_closed(self):
        for context in (None, InputContext(101, 99999, (1,), None, True)):
            self.context = context
            self.type("must not capture.")
            self.assertEqual(self.app.buf.peek_sentence(), "")
        self.assertEqual(self.jobs, [])

    def test_external_click_and_physical_typing_invalidate_pending_job(self):
        self.type("old draft")
        epoch = self.app.buf.epoch()
        self.app._on_click(0, 0, None, True)
        self.assertFalse(self.app._replacement_is_current(epoch))
        epoch = self.app.buf.epoch()
        self.app.buf.set_busy(True)
        self.type("x")
        self.assertFalse(self.app._replacement_is_current(epoch))

    def test_clicking_review_card_does_not_cancel_source_job(self):
        self.type("draft words")
        epoch = self.app.buf.epoch()
        with mock.patch.object(daemon, "_window_process_id", return_value=os.getpid()):
            self.app._on_click(0, 0, None, True)
        self.assertEqual(self.app.buf.epoch(), epoch)

    def test_injected_copy_paste_does_not_invalidate_its_own_job(self):
        self.type("draft words")
        epoch = self.app.buf.epoch()
        self.app.buf.set_busy(True)
        self.app._on_press(Key.left, injected=True)
        self.app._on_press(KeyCode.from_char("v"), injected=True)
        self.app._on_release(Key.ctrl, injected=True)
        self.assertEqual(self.app.buf.epoch(), epoch)

    def test_reading_a_noneditable_field_keeps_job_valid(self):
        self.context = InputContext(101, 99999, (8,), False, False)
        self.app._sync_foreground_typing_context()
        epoch = self.app.buf.epoch()
        self.app.buf.set_busy(True)
        self.app._sync_foreground_typing_context()
        self.assertEqual(self.app.buf.epoch(), epoch)


class ReplacementTests(unittest.TestCase):
    def selected_editor(self, text):
        editor = _FakeEditor(text)
        editor.anchor = 0
        return editor

    def test_changed_or_missing_selection_is_not_overwritten(self):
        for text, selected in (("KEEP THIS SAFE", True), ("Original passage", False),
                               ("Original  passage", True)):
            editor = self.selected_editor(text)
            if not selected:
                editor.anchor = None
            with _wired(editor), self.assertRaises(RuntimeError):
                replacement._do_replace_selected_text(
                    "Improved passage", "", expected_window=101,
                    expected_original="Original passage",
                )
            self.assertEqual(editor.text, text)

    def test_matching_original_selection_with_inline_prefix_replaces(self):
        text = "formal: i wanna go"
        editor = self.selected_editor(text)
        with _wired(editor):
            replacement._do_replace_selected_text(
                "I would like to go", "old clipboard", expected_window=101,
                expected_original=text,
            )
        self.assertEqual(editor.text, "I would like to go")
        self.assertEqual(editor.clipboard, "old clipboard")

    def test_unavailable_window_is_not_treated_as_safe(self):
        with mock.patch.object(replacement, "_active_window_token", return_value=None):
            with self.assertRaises(RuntimeError):
                replacement._ensure_expected_window(101)

    def test_late_window_or_field_change_blocks_every_paste_path(self):
        for path in ("spell", "rephrase", "selected"):
            for change in ("window", "field", "disable"):
                with self.subTest(path=path, change=change):
                    original = "helo world?" if path == "spell" else "helo world"
                    editor = self.selected_editor(original)
                    if path != "selected":
                        editor.anchor = None
                    window, context, valid = [101], [FIELD], [True]
                    def set_clipboard(value):
                        editor.clipboard = value
                        if value.startswith("Hello world"):
                            if change == "window":
                                window[0] = 202
                            elif change == "field":
                                context[0] = OTHER_FIELD
                            else:
                                valid[0] = False
                    with _wired(editor), \
                            mock.patch.object(replacement, "_active_window_token", side_effect=lambda: window[0]), \
                            mock.patch.object(replacement, "focused_context", side_effect=lambda: context[0]), \
                            mock.patch.object(replacement, "_clipboard_set_text", side_effect=set_clipboard):
                        kwargs = dict(expected_window=101, expected_context=FIELD,
                                      validate=lambda: valid[0])
                        with self.assertRaises(RuntimeError):
                            if path == "spell":
                                replacement._do_replace("helo world", "Hello world", "?", "", 10, **kwargs)
                            elif path == "rephrase":
                                replacement._do_replace_rephrase("helo world", "Hello world", "", 10, **kwargs)
                            else:
                                replacement._do_replace_selected_text("Hello world", "", expected_original=original, **kwargs)
                    self.assertEqual(editor.text, original)
                    self.assertNotIn("Hello world", editor.clipboard)

    def test_focus_change_during_selection_stops_synthetic_keys(self):
        editor = _FakeEditor("helo world?")
        active = [101]
        presses = []
        def press(key):
            presses.append(key)
            editor.press(key)
            if key == Key.left:
                active[0] = 202
        with _wired(editor), \
                mock.patch.object(replacement, "_active_window_token", side_effect=lambda: active[0]), \
                mock.patch.object(replacement, "_kbctrl", SimpleNamespace(
                    pressed=editor.pressed, press=press, release=editor.release)):
            with self.assertRaises(RuntimeError):
                replacement._do_replace("helo world", "Hello world", "?", "", 10, expected_window=101)
        self.assertEqual(presses.count(Key.left), 1)
        self.assertNotIn("v", presses)
        self.assertEqual(editor.text, "helo world?")

    def test_late_focus_switch_never_pastes_into_second_editor(self):
        # Route real replacement keystrokes to whichever fake app has focus.
        # This reproduces the original cross-app data-loss bug, including a
        # switch after clipboard setup, on Ctrl-down, or during the final UIA
        # query. Both documents and the clipboard must survive every abort.
        for path in ("spell", "rephrase", "selected"):
            for stage in ("clipboard_delay", "modifier", "focus_probe"):
                with self.subTest(path=path, stage=stage):
                    original = "helo world?" if path == "spell" else "helo world"
                    corrected = "Hello world?" if path == "spell" else "Hello world"
                    source = self.selected_editor(original)
                    if path != "selected":
                        source.anchor = None
                    other = self.selected_editor("IMPORTANT OTHER APP TEXT")
                    editors, active = [source, other], [0]
                    clipboard = ["clipboard before"]
                    sent = []

                    def set_clipboard(value):
                        clipboard[0] = value
                        for editor in editors:
                            editor.clipboard = value

                    set_clipboard(clipboard[0])

                    class Router:
                        @contextmanager
                        def pressed(self, key):
                            for editor in editors:
                                editor._held.add(key)
                            try:
                                if (stage == "modifier" and key == replacement._CMD_KEY
                                        and clipboard[0] == corrected):
                                    active[0] = 1
                                yield
                            finally:
                                for editor in editors:
                                    editor._held.discard(key)

                        def press(self, key):
                            sent.append((active[0], key))
                            editors[active[0]].press(key)
                            if key == "c":
                                set_clipboard(editors[active[0]].clipboard)

                        def release(self, key):
                            editors[active[0]].release(key)

                    def sleep(seconds):
                        if stage == "clipboard_delay" and seconds == 0.05 and clipboard[0] == corrected:
                            active[0] = 1

                    def focus():
                        if stage == "focus_probe" and clipboard[0] == corrected:
                            active[0] = 1
                        # Deliberately return the stale snapshot: the final
                        # foreground-window check must catch this race too.
                        return FIELD

                    with _wired(source), \
                            mock.patch.object(replacement, "_kbctrl", Router()), \
                            mock.patch.object(replacement, "_active_window_token", side_effect=lambda: 101 if active[0] == 0 else 202), \
                            mock.patch.object(replacement, "focused_context", side_effect=focus), \
                            mock.patch.object(replacement, "_clipboard_get_text", side_effect=lambda: clipboard[0]), \
                            mock.patch.object(replacement, "_clipboard_set_text", side_effect=set_clipboard), \
                            mock.patch.object(replacement.time, "sleep", side_effect=sleep):
                        kwargs = dict(expected_window=101, expected_context=FIELD, validate=lambda: True)
                        with self.assertRaises(replacement.ReplacementAborted):
                            if path == "spell":
                                replacement._do_replace("helo world", "Hello world", "?", "", 10, **kwargs)
                            elif path == "rephrase":
                                replacement._do_replace_rephrase("helo world", "Hello world", "", 10, **kwargs)
                            else:
                                replacement._do_replace_selected_text(corrected, "clipboard before", expected_original=original, **kwargs)

                    self.assertEqual(active[0], 1, "test must actually switch apps")
                    self.assertEqual(source.text, original)
                    self.assertEqual(other.text, "IMPORTANT OTHER APP TEXT")
                    self.assertEqual(clipboard[0], "clipboard before")
                    self.assertFalse(any(key == "v" for _, key in sent))
                    self.assertFalse(source._held or other._held)


class MetadataTests(unittest.TestCase):
    def test_unsupported_password_property_is_not_defaulted_to_false(self):
        props = {30000: (1,), 30002: 99999, 30019: object(),
                 30003: 50004, 30008: True, 30010: True}
        element = SimpleNamespace(GetCachedPropertyValueEx=lambda p, ignore: props[p])
        automation = SimpleNamespace(GetFocusedElementBuildCache=lambda _cache: element)
        with mock.patch.object(input_context, "foreground_window", return_value=101):
            context = input_context._read_context(automation, None)
        self.assertIsNone(context.password)
        self.assertFalse(context.can_capture)

    def test_slow_provider_never_returns_stale_snapshot(self):
        probe = input_context._FocusProbe()
        def slow_worker():
            done, result = probe._requests.get()
            time.sleep(0.1)
            result.append(FIELD)
            done.set()
        with mock.patch.object(probe, "_run", slow_worker):
            start = time.monotonic()
            self.assertIsNone(probe.read(timeout=0.01))
            self.assertLess(time.monotonic() - start, 0.08)
            time.sleep(0.12)
            self.assertIsNone(probe.read(timeout=0.01))


class PipelineTests(unittest.TestCase):
    def test_auto_correction_accepts_document_capitalization_from_keyboard_capture(self):
        app = _bare_daemon()
        app.cfg.update(spell_check_mode="Auto", working_language="Auto")
        for char in "i am writing here":
            app.buf.key_char(char)
        sentence, punct, raw_len, leading_ws, epoch = app.buf.trigger_with_epoch(".", 2)
        editor = _FakeEditor("I am writing here.")
        with _wired(editor) as recorded, \
                mock.patch.object(replacement, "focused_context", return_value=FIELD), \
                mock.patch.object(daemon, "spell_check_sentence", return_value="I am writing here"), \
                mock.patch.object(daemon, "_surface_replacement_abort") as aborted:
            app._pipeline(sentence, punct, raw_len, leading_ws,
                          target_window=101, context_epoch=epoch, target_context=FIELD)
        aborted.assert_not_called()
        self.assertEqual(editor.text, "I am writing here.")
        self.assertIsNone(editor.anchor)
        self.assertFalse(app.buf.is_busy_or_pending())
        self.assertTrue(any("Sentence replaced automatically" in str(call)
                            for call in recorded.call_args_list))

    def test_selection_failures_explain_the_cause_without_claiming_focus_loss(self):
        for reason, expected in (("selection_unavailable", "did not return"),
                                 ("source_mismatch", "did not match")):
            with mock.patch.object(daemon, "_info_toast") as toast:
                daemon._surface_replacement_abort(
                    replacement.ReplacementAborted("Verification failed", reason=reason))
            message = toast.call_args.args[0]
            self.assertIn(expected, message)
            self.assertNotIn("focus", message)

    def test_review_acceptance_checks_selection_in_real_replacement_path(self):
        source = "formal: i wanna go"
        suffix = " KEEP THIS SAFE"
        for selection in ("original", "different", "none"):
            with self.subTest(selection=selection):
                app = _bare_daemon()
                app.cfg.update(prompt_format="Plain  (no template)")
                editor = _FakeEditor(source + suffix)
                editor.anchor, editor.caret = 0, len(source)

                class Toast:
                    def __init__(self, _text, accept, _ignore, **_kw):
                        self.accept = accept

                    def set_text(self, text, select=False):
                        if not select:
                            return
                        if selection == "different":
                            editor.anchor, editor.caret = len(source) + 1, len(editor.text)
                        elif selection == "none":
                            editor.anchor = None
                        self.accept(text)

                    def close(self):
                        pass

                    def show_model_switch(self, *_args):
                        pass

                with _wired(editor), \
                        mock.patch.object(replacement, "focused_context", return_value=FIELD), \
                        mock.patch.object(daemon, "_EditableToast", Toast), \
                        mock.patch.object(daemon, "rephrase_sentence_stream", return_value=iter(["I would like to go"])), \
                        mock.patch.object(daemon, "_surface_replacement_abort") as aborted:
                    app._selected_rephrase_pipeline(
                        "i wanna go", "old clipboard", mode_override="Polish My Writing",
                        target_window=101, target_context=FIELD, source_text=source,
                    )

                if selection == "original":
                    self.assertEqual(editor.text, "I would like to go" + suffix)
                    aborted.assert_not_called()
                else:
                    self.assertEqual(editor.text, source + suffix)
                    aborted.assert_called_once()
                self.assertFalse(app.buf.is_busy_or_pending())

    def run_selected(self, invalidate=False):
        app = _bare_daemon()
        app.cfg.update(prompt_format="Plain  (no template)")
        closed = []
        class Toast:
            def __init__(self, _text, accept, _ignore, **kw):
                self.accept = accept
            def set_text(self, text, select=False):
                if select:
                    self.accept(text)
            def close(self):
                closed.append(True)
            def show_model_switch(self, *args):
                pass
        def stream(*args, **kwargs):
            if invalidate:
                app.cfg["enabled"] = False
                app.buf.clear()
            yield "I would like to go"
        with mock.patch.object(daemon, "_EditableToast", Toast), \
                mock.patch.object(daemon, "rephrase_sentence_stream", stream), \
                mock.patch.object(daemon, "_do_replace_selected_text") as replace:
            app._selected_rephrase_pipeline(
                "i wanna go", "old clipboard", mode_override="Polish My Writing",
                target_window=101, target_context=FIELD, source_text="formal: i wanna go",
            )
        return replace, closed

    def test_selection_pipeline_passes_raw_source_to_verification(self):
        replace, _closed = self.run_selected()
        self.assertEqual(replace.call_args.kwargs["expected_original"], "formal: i wanna go")
        self.assertEqual(replace.call_args.kwargs["expected_context"], FIELD)

    def test_disabled_selected_job_cannot_paste(self):
        replace, closed = self.run_selected(invalidate=True)
        replace.assert_not_called()
        self.assertTrue(closed)


if __name__ == "__main__":
    unittest.main()
