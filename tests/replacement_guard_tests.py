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
    with (
        mock.patch.object(replacement, '_kbctrl', editor),
        mock.patch.object(replacement, '_clipboard_get_text',
                          lambda: editor.clipboard),
        mock.patch.object(replacement, '_clipboard_set_text',
                          lambda text: setattr(editor, 'clipboard', text)),
        mock.patch.object(replacement.time, 'sleep', lambda *_a, **_k: None),
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


def test_unreadable_selection_falls_back_to_replacing() -> None:
    """Apps that refuse Ctrl+C must behave exactly as they did before the guard."""
    raw = "helo world"
    editor = _FakeEditor(raw + "?", copy_works=False)

    with _wired(editor):
        replacement._do_replace(raw, "Hello world", "?", "", raw_len=len(raw))

    assert editor.text == "Hello world?", editor.text


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
