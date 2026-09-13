# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""
Regression checks for the Shift+Left selection guard in vedium.replacement.

The daemon counts the characters it watched the user type, then selects that
many characters backwards and pastes over them. Any host application that edits
the line on its own -- Windows 11 Notepad inserting an apostrophe, an editor
swapping straight quotes for curly ones -- desyncs that count, so the selection
covers the wrong span and the paste corrupts surrounding text. These tests drive
the real replacement code against a fake text field to prove it now refuses.
"""

import contextlib
from pathlib import Path
import sys
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pynput.keyboard import Key

from vedium import replacement
from vedium.constants import _CMD_KEY


class _FakeEditor:
    """A minimal text field: caret at the end, Shift+Left selection, Ctrl+C/V."""

    def __init__(self, text: str, copy_works: bool = True):
        self.text = text
        self.caret = len(text)
        self.anchor: int | None = None
        self.clipboard = ""
        self.copy_works = copy_works
        self._held: set = set()

    # -- selection ---------------------------------------------------------
    def _bounds(self) -> tuple[int, int]:
        if self.anchor is None:
            return self.caret, self.caret
        return min(self.caret, self.anchor), max(self.caret, self.anchor)

    @property
    def selected(self) -> str:
        start, end = self._bounds()
        return self.text[start:end]

    def _replace_selection(self, new: str):
        start, end = self._bounds()
        self.text = self.text[:start] + new + self.text[end:]
        self.caret = start + len(new)
        self.anchor = None

    # -- pynput Controller surface ----------------------------------------
    @contextlib.contextmanager
    def pressed(self, key):
        self._held.add(key)
        try:
            yield
        finally:
            self._held.discard(key)

    def press(self, key):
        shifted = Key.shift in self._held
        ctrl = _CMD_KEY in self._held
        if key == Key.left:
            if shifted and self.anchor is None:
                self.anchor = self.caret
            self.caret = max(0, self.caret - 1)
            if not shifted:
                self.anchor = None
        elif key == Key.right:
            if self.anchor is not None and not shifted:
                self.caret = max(self.caret, self.anchor)   # collapse right
                self.anchor = None
            else:
                self.caret = min(len(self.text), self.caret + 1)
        elif key == 'c' and ctrl:
            if self.copy_works:
                self.clipboard = self.selected
        elif key == 'v' and ctrl:
            self._replace_selection(self.clipboard)

    def release(self, key):
        pass


@contextlib.contextmanager
def _wired(editor: _FakeEditor):
    """Point vedium.replacement at the fake editor instead of the real desktop."""
    elapsed = [0.0]
    with (
        mock.patch.object(replacement, '_kbctrl', editor),
        mock.patch.object(replacement, '_active_window_token', return_value=101),
        mock.patch.object(replacement, '_clipboard_get_text',
                          lambda: editor.clipboard),
        mock.patch.object(replacement, '_clipboard_set_text',
                          lambda text: setattr(editor, 'clipboard', text)),
        mock.patch.object(replacement.time, 'sleep',
                          lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)),
        mock.patch.object(replacement.time, 'monotonic', lambda: elapsed[0]),
        mock.patch.object(replacement.diagnostics, 'record') as recorded,
    ):
        yield recorded


def test_host_autocorrect_desync_is_refused_not_pasted() -> None:
    """The live Win11 Notepad repro: autocorrect added an apostrophe we never saw."""
    # The user typed "doesnt this look good" then "?"; Notepad rewrote the word.
    observed_raw = "doesnt this look good"
    document = "doesn't this look good?"
    editor = _FakeEditor(document)

    with _wired(editor) as recorded:
        try:
            replacement._do_replace(
                observed_raw, "Doesn't this look good", "?", "",
                raw_len=len(observed_raw),
            )
            raise AssertionError("expected the guard to refuse the paste")
        except RuntimeError as exc:
            assert "refusing to paste" in str(exc).lower(), exc

    # The document must be exactly as the user left it -- no "dDoesn't ..." mangling.
    assert editor.text == document, editor.text
    assert any(call.args[1] == "warning" for call in recorded.call_args_list)


def test_refused_replacement_leaves_no_dangling_selection() -> None:
    """A live selection would be destroyed by the user's very next keystroke."""
    editor = _FakeEditor("doesn't this look good?")

    with _wired(editor):
        try:
            replacement._do_replace(
                "doesnt this look good", "Doesn't this look good", "?", "",
                raw_len=len("doesnt this look good"),
            )
        except RuntimeError:
            pass

    assert editor.anchor is None, "selection was left active after refusing"
    assert editor.caret == len(editor.text), editor.caret


def test_matching_document_still_replaces() -> None:
    """The guard must not block the ordinary, correctly-tracked case."""
    raw = "helo world"
    editor = _FakeEditor(raw + "?")

    with _wired(editor):
        replacement._do_replace(raw, "Hello world", "?", "", raw_len=len(raw))

    assert editor.text == "Hello world?", editor.text


def test_captured_capitalization_difference_does_not_cancel_plain_sentence() -> None:
    # Windows key translation can report lowercase while the document holds
    # uppercase (Caps Lock or Shift state observed after the key event).
    raw = "i am writing here"
    editor = _FakeEditor("I am writing here.")
    with _wired(editor):
        replacement._do_replace(raw, "I am writing here", ".", "", len(raw))
    assert editor.text == "I am writing here."
    assert editor.anchor is None


def test_smart_quotes_with_same_selection_span_do_not_cancel() -> None:
    raw = "i can't go there"
    editor = _FakeEditor("I can’t go there.")
    with _wired(editor):
        replacement._do_replace(raw, "I cannot go there", ".", "", len(raw))
    assert editor.text == "I cannot go there."


def test_cosmetic_source_match_keeps_later_typing_unchanged() -> None:
    raw, post = "i am writing here", " Next DRAFT"
    editor = _FakeEditor("I am writing here." + post)
    with _wired(editor):
        replacement._do_replace(raw, "I am writing here", ".", post, len(raw))
    assert editor.text == "I am writing here." + post


def test_cosmetic_match_does_not_allow_overwriting_different_later_typing() -> None:
    raw, captured_post = "i am writing here", " next draft"
    document = "I am writing here. Next DRAFT"
    editor = _FakeEditor(document)
    with _wired(editor):
        try:
            replacement._do_replace(raw, "I am writing here", ".", captured_post, len(raw))
            raise AssertionError("later typing must not be changed by source normalization")
        except replacement.ReplacementAborted:
            pass
    assert editor.text == document


def test_explicit_selection_still_requires_exact_capitalization() -> None:
    editor = _FakeEditor("KEEP THIS")
    editor.anchor = 0
    with _wired(editor):
        try:
            replacement._do_replace_selected_text(
                "Changed text", "", expected_window=101, expected_original="keep this")
            raise AssertionError("explicit selections must still match exactly")
        except replacement.ReplacementAborted:
            pass
    assert editor.text == "KEEP THIS"


def test_trailing_text_after_the_trigger_is_carried_along() -> None:
    raw = "helo world"
    editor = _FakeEditor(raw + "? and more")

    with _wired(editor):
        replacement._do_replace(raw, "Hello world", "?", " and more",
                                raw_len=len(raw))

    assert editor.text == "Hello world? and more", editor.text


def test_whitespace_only_differences_do_not_block() -> None:
    """Leading/trailing whitespace in the span is not reconstructable; ignore it."""
    raw = "  helo world "          # buffer kept the surrounding spaces
    sentence = raw.strip()
    editor = _FakeEditor(raw + "?")

    with _wired(editor):
        replacement._do_replace(sentence, "Hello world", "?", "",
                                raw_len=len(raw), leading_ws="  ")

    assert editor.text == "  Hello world?", editor.text


def test_unreadable_selection_is_refused() -> None:
    """Never overwrite text when its selection cannot be verified."""
    raw = "helo world"
    editor = _FakeEditor(raw + "?", copy_works=False)

    with _wired(editor):
        try:
            replacement._do_replace(raw, "Hello world", "?", "", raw_len=len(raw))
            raise AssertionError("unreadable selection must be refused")
        except RuntimeError:
            pass

    assert editor.text == raw + "?", editor.text
    assert editor.anchor is None


def test_rephrase_inline_trigger_prefix_is_not_a_false_mismatch() -> None:
    """`original` has the mode trigger stripped; the document still holds it."""
    typed = "formal: i wanna go"
    cleaned = "i wanna go"          # what resolve_mode() handed the pipeline
    editor = _FakeEditor(typed)

    with _wired(editor):
        replacement._do_replace_rephrase(
            cleaned, "I would like to go", "", raw_len=len(typed),
            from_toast=False, expected_original=typed,
        )

    assert editor.text == "I would like to go", editor.text


def test_rephrase_desync_is_refused() -> None:
    editor = _FakeEditor("I can't go there")     # host inserted the apostrophe

    with _wired(editor):
        try:
            replacement._do_replace_rephrase(
                "I cant go there", "I am unable to attend", "",
                raw_len=len("I cant go there"), from_toast=False,
            )
            raise AssertionError("expected the guard to refuse the paste")
        except RuntimeError:
            pass

    assert editor.text == "I can't go there", editor.text


def test_delayed_editor_copy_is_verified_before_replacement() -> None:
    """A response later than the old 120ms cutoff is not a text mismatch."""
    raw = "i am writing here"
    editor = _FakeEditor(raw + ".", copy_works=False)
    editor.clipboard = "previous clipboard"
    pending = []
    copies = []
    original_press = editor.press

    def press(key):
        original_press(key)
        if key == 'c' and _CMD_KEY in editor._held:
            copies.append(key)
            pending.append((replacement.time.monotonic() + 0.25, editor.selected))

    def clipboard_get():
        if pending and replacement.time.monotonic() >= pending[0][0]:
            editor.clipboard = pending.pop(0)[1]
        return editor.clipboard

    with _wired(editor), mock.patch.object(editor, 'press', side_effect=press), \
            mock.patch.object(replacement, '_clipboard_get_text', side_effect=clipboard_get):
        replacement._do_replace(raw, "I am writing here", ".", "", len(raw))

    assert editor.text == "I am writing here."
    assert editor.clipboard == "previous clipboard"
    assert copies == ['c'], "wait for the response instead of repeating copy keys"


def test_unreadable_and_mismatched_selections_have_distinct_reasons() -> None:
    for copy_works, document, reason in (
        (False, "i am writing here.", "selection_unavailable"),
        (True, "i am writing more.", "source_mismatch"),
    ):
        editor = _FakeEditor(document, copy_works=copy_works)
        editor.clipboard = "previous clipboard"
        with _wired(editor) as recorded:
            try:
                replacement._do_replace("i am writing here", "I am writing here", ".", "", 17)
                raise AssertionError("verification must block this replacement")
            except replacement.ReplacementAborted as exc:
                assert exc.reason == reason
        assert editor.text == document
        assert editor.anchor is None
        assert editor.clipboard == "previous clipboard"
        log = str(recorded.call_args_list)
        assert "autocorrect" not in log
        assert document not in log and "previous clipboard" not in log
        if copy_works:
            assert "expected=" in log and "selected=" in log


def test_focus_change_while_waiting_for_copy_aborts_and_restores_clipboard() -> None:
    editor = _FakeEditor("i am writing here.", copy_works=False)
    editor.clipboard = "previous clipboard"
    sent = []

    def guard():
        if replacement.time.monotonic() >= 0.15:
            raise replacement.ReplacementAborted("Focus moved; refusing to paste.")

    with _wired(editor), mock.patch.object(editor, 'press', side_effect=lambda key: sent.append(key)):
        try:
            replacement._copy_selected_text(guard=guard)
            raise AssertionError("a focus change must cancel the copy wait")
        except replacement.ReplacementAborted:
            pass
    assert sent == ['c']
    assert editor.clipboard == "previous clipboard"
    assert editor.text == "i am writing here."


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:
                failures += 1
                print(f"FAIL {name}: {exc!r}")
    sys.exit(1 if failures else 0)
