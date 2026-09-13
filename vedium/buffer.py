# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

import time
import threading
from .constants import DEFAULT_TRIGGER_CHARS, _is_mid_token, _word_count

class _Buffer:
    def __init__(self):
        self._lk        = threading.Lock()
        self._sentence  : list[str] = []   # chars of the current sentence
        self._post      : list[str] = []   # chars typed after trigger, while LLM runs
        self._pending   = False            # True while LLM is processing
        self._cursor_mv = False            # True if cursor moved mid-pending
        self._busy      = False            # True during keyboard replacement (ignore all keys)
        self._queued_post_boundaries: list[int] = []  # post-buffer offsets after queued trigger chars
        self._queued_sources: list[tuple[str, str]] = []  # (raw sentence, trigger) for queued jobs
        self._active_source_raw: str = ""  # source text owned by the current pending job
        self._active_trigger: str | None = None  # None for manual rephrase jobs
        self._seen_trigger_chars: set[str] = set(DEFAULT_TRIGGER_CHARS)
        self._last_completed: str = ""    # most recent finished sentence, including punctuation
        self._last_key_time: float = 0.0  # timestamp of the last registered keypress
        self._enters_while_busy: int = 0  # Enter presses that arrived while _busy=True; re-emitted after replacement
        # Incremented whenever the typing context is deliberately discarded.
        # Background model jobs keep the epoch they started with, so a late
        # response cannot mutate or replace text belonging to a newer window.
        self._epoch: int = 0

    def clear(self) -> int:
        """Discard every piece of captured typing state and invalidate old jobs."""
        with self._lk:
            self._epoch += 1
            self._sentence = []
            self._post = []
            self._pending = False
            self._cursor_mv = False
            self._busy = False
            self._queued_post_boundaries = []
            self._queued_sources = []
            self._active_source_raw = ""
            self._active_trigger = None
            self._last_completed = ""
            self._last_key_time = 0.0
            self._enters_while_busy = 0
            return self._epoch

    def epoch(self) -> int:
        with self._lk:
            return self._epoch

    def is_epoch_current(self, expected_epoch: int | None) -> bool:
        with self._lk:
            return expected_epoch is None or expected_epoch == self._epoch

    def _idle_reset(self, idle_sec: float):
        now = time.time()
        if (idle_sec > 0 and self._sentence and not self._pending and not self._busy
                and (now - self._last_key_time) > idle_sec):
            self._sentence = []
        self._last_key_time = now

    def key_char(self, c: str, idle_sec: float = 0.0):
        with self._lk:
            if self._busy:
                return
            self._idle_reset(idle_sec)
            if self._pending:
                self._post.append(c)
            else:
                if self._sentence and not c.isspace():
                    idx = len(self._sentence) - 1
                    while idx >= 0 and self._sentence[idx].isspace():
                        idx -= 1
                    if (idx >= 0 and idx < len(self._sentence) - 1
                            and self._sentence[idx] in self._seen_trigger_chars):
                        sentence_before = "".join(self._sentence[:idx])
                        punct = self._sentence[idx]
                        if not _is_mid_token(sentence_before, punct):
                            # The previous completed/short fragment is already
                            # behind the caret. Keep only its trailing whitespace
                            # as the leading whitespace of the new sentence.
                            self._sentence = self._sentence[idx + 1:]
                self._sentence.append(c)

    def _cancel_pending_edit_locked(self, restored: str) -> None:
        """Invalidate stale jobs and resume capture from edited source text."""
        self._epoch += 1
        self._sentence = list(restored)
        self._post = []
        self._pending = False
        self._cursor_mv = False
        self._busy = False
        self._queued_post_boundaries = []
        self._queued_sources = []
        self._active_source_raw = ""
        self._active_trigger = None
        self._last_completed = ""
        self._enters_while_busy = 0

    def key_backspace(self, idle_sec: float = 0.0) -> bool:
        """Apply Backspace and report whether it invalidated pending jobs.

        If Backspace crosses a spell-check trigger boundary, the punctuation
        has disappeared from the real document. The job created by that
        punctuation is now stale, so cancel it and restore the editable source
        sentence instead of leaving the buffer permanently out of sync.
        """
        with self._lk:
            if self._busy:
                return False
            self._idle_reset(idle_sec)
            if not self._pending:
                if self._sentence:
                    self._sentence.pop()
                return False

            # A queued boundary ends with its trigger punctuation. Deleting that
            # punctuation retracts the newest queued job and makes every older
            # offset unsafe; invalidate the chain and resume from that sentence.
            if (self._queued_post_boundaries
                    and len(self._post) == self._queued_post_boundaries[-1]):
                if self._queued_sources:
                    restored = self._queued_sources[-1][0]
                else:  # defensive fallback for configs/state from older builds
                    previous = (
                        self._queued_post_boundaries[-2]
                        if len(self._queued_post_boundaries) > 1 else 0
                    )
                    restored = "".join(self._post[previous:-1])
                self._cancel_pending_edit_locked(restored)
                return True

            if self._post:
                self._post.pop()
                return False

            # No post-trigger text remains: Backspace deleted the trigger that
            # launched the active spell job (or the final source character of a
            # manual buffered rephrase). Restore exactly what remains on screen.
            if self._active_source_raw:
                restored = self._active_source_raw
                if self._active_trigger is None:
                    restored = restored[:-1]
                self._cancel_pending_edit_locked(restored)
                return True
            return False

    def key_nav(self):
        with self._lk:
            self._cursor_mv = True

    def key_enter(self, idle_sec: float = 0.0):
        with self._lk:
            if self._busy:
                self._enters_while_busy += 1
                return
            self._idle_reset(idle_sec)
            if self._pending:
                self._cursor_mv = True
                self._post.append("\n")
            else:
                self._sentence.append("\n")

    def peek_sentence(self) -> str:
        with self._lk:
            return "".join(self._sentence)

    def _trigger_locked(self, punct: str, min_words: int):
        if self._busy:
            return None
        self._seen_trigger_chars.add(punct)
        if self._pending:
            start = self._queued_post_boundaries[-1] if self._queued_post_boundaries else 0
            raw   = "".join(self._post[start:])
        else:
            raw   = "".join(self._sentence)
        sentence   = raw.strip()
        word_count = _word_count(sentence)
        if word_count < min_words:
            if self._pending:
                self._post.append(punct)
            else:
                self._sentence = []
            return None
        leading_ws           = raw[: len(raw) - len(raw.lstrip())]
        self._last_completed = sentence + punct
        if self._pending:
            self._post.append(punct)
            self._queued_post_boundaries.append(len(self._post))
            self._queued_sources.append((raw, punct))
        else:
            self._pending   = True
            self._cursor_mv = False
            self._sentence  = []
            self._post      = []
            self._queued_post_boundaries = []
            self._queued_sources = []
            self._active_source_raw = raw
            self._active_trigger = punct
        return sentence, punct, len(raw), leading_ws

    def trigger(self, punct: str, min_words: int):
        with self._lk:
            return self._trigger_locked(punct, min_words)

    def trigger_with_epoch(self, punct: str, min_words: int):
        """Trigger and atomically attach the current typing-context epoch."""
        with self._lk:
            result = self._trigger_locked(punct, min_words)
            return (*result, self._epoch) if result else None

    def _trigger_rephrase_locked(self, min_words: int):
        if self._busy or self._pending:
            return None

        current_raw = "".join(self._sentence)
        current     = current_raw.strip()

        if current:
            sentence = current
            raw_len  = len(current_raw)
            suffix   = ""
        else:
            sentence = self._last_completed.strip()
            raw_len  = len(self._last_completed)
            suffix   = current_raw

        word_count = _word_count(sentence)
        if word_count < min_words:
            return None

        self._pending   = True
        self._cursor_mv = False
        self._sentence  = []
        self._post      = list(suffix)
        self._queued_post_boundaries = []
        self._queued_sources = []
        self._active_source_raw = current_raw if current else sentence
        self._active_trigger = None
        return sentence, raw_len

    def trigger_rephrase(self, min_words: int):
        with self._lk:
            return self._trigger_rephrase_locked(min_words)

    def trigger_rephrase_with_epoch(self, min_words: int):
        """Start a buffered rephrase and atomically attach its context epoch."""
        with self._lk:
            result = self._trigger_rephrase_locked(min_words)
            return (*result, self._epoch) if result else None

    def drain_queued_enters(self, expected_epoch: int | None = None) -> int:
        with self._lk:
            if expected_epoch is not None and expected_epoch != self._epoch:
                return 0
            n = self._enters_while_busy
            self._enters_while_busy = 0
            return n

    def finalize(self, replaced: bool, completed_text: str | None = None,
                 expected_epoch: int | None = None) -> bool:
        with self._lk:
            if expected_epoch is not None and expected_epoch != self._epoch:
                return False
            self._pending = False
            self._busy    = False
            self._enters_while_busy = 0
            if self._queued_post_boundaries:
                if completed_text:
                    self._last_completed = completed_text
                boundary = self._queued_post_boundaries.pop(0)
                self._post = self._post[boundary:]
                self._queued_post_boundaries = [
                    max(0, old_boundary - boundary)
                    for old_boundary in self._queued_post_boundaries
                ]
                if self._queued_sources:
                    self._active_source_raw, self._active_trigger = self._queued_sources.pop(0)
                else:
                    self._active_source_raw, self._active_trigger = "", None
                self._pending   = True
                self._cursor_mv = False
                return True
            if not replaced:
                self._sentence = list(self._post)
            else:
                self._sentence        = []
                self._last_completed  = ""
                self._cursor_mv       = False
            self._post = []
            self._queued_sources = []
            self._active_source_raw = ""
            self._active_trigger = None
            return True

    def is_busy_or_pending(self) -> bool:
        with self._lk:
            return self._busy or self._pending

    def is_busy(self) -> bool:
        with self._lk:
            return self._busy

    def cursor_moved(self, expected_epoch: int | None = None) -> bool:
        with self._lk:
            if expected_epoch is not None and expected_epoch != self._epoch:
                return True
            return self._cursor_mv

    def post_text(self, expected_epoch: int | None = None) -> str:
        with self._lk:
            if expected_epoch is not None and expected_epoch != self._epoch:
                return ""
            return "".join(self._post)

    def set_busy(self, v: bool, expected_epoch: int | None = None) -> bool:
        with self._lk:
            if expected_epoch is not None and expected_epoch != self._epoch:
                return False
            self._busy = v
            return True

    def snapshot_post_and_set_busy(self, expected_epoch: int | None = None) -> str | None:
        with self._lk:
            if expected_epoch is not None and expected_epoch != self._epoch:
                return None
            self._busy = True
            return "".join(self._post)
