import time
import threading
from .constants import _is_mid_token, _word_count

class _Buffer:
    def __init__(self):
        self._lk        = threading.Lock()
        self._sentence  : list[str] = []   # chars of the current sentence
        self._post      : list[str] = []   # chars typed after trigger, while LLM runs
        self._pending   = False            # True while LLM is processing
        self._cursor_mv = False            # True if cursor moved mid-pending
        self._busy      = False            # True during keyboard replacement (ignore all keys)
        self._queued_post_boundaries: list[int] = []  # post-buffer offsets after queued trigger chars
        self._last_completed: str = ""    # most recent finished sentence, including punctuation
        self._last_key_time: float = 0.0  # timestamp of the last registered keypress
        self._enters_while_busy: int = 0  # Enter presses that arrived while _busy=True; re-emitted after replacement

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
                    if idx >= 0 and idx < len(self._sentence) - 1 and self._sentence[idx] in ".!?":
                        sentence_before = "".join(self._sentence[:idx])
                        punct = self._sentence[idx]
                        if _is_mid_token(sentence_before, punct):
                            self._sentence = []
                self._sentence.append(c)

    def key_backspace(self, idle_sec: float = 0.0):
        with self._lk:
            if self._busy:
                return
            self._idle_reset(idle_sec)
            target = self._post if self._pending else self._sentence
            if target:
                target.pop()

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

    def trigger(self, punct: str, min_words: int):
        with self._lk:
            if self._busy:
                return None
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
            else:
                self._pending   = True
                self._cursor_mv = False
                self._sentence  = []
                self._post      = []
                self._queued_post_boundaries = []
            return sentence, punct, len(raw), leading_ws

    def trigger_rephrase(self, min_words: int):
        with self._lk:
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
            return sentence, raw_len

    def drain_queued_enters(self) -> int:
        with self._lk:
            n = self._enters_while_busy
            self._enters_while_busy = 0
            return n

    def finalize(self, replaced: bool, completed_text: str | None = None):
        with self._lk:
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
                self._pending   = True
                self._cursor_mv = False
                return
            if not replaced:
                self._sentence = list(self._post)
            else:
                self._sentence        = []
                self._last_completed  = ""
                self._cursor_mv       = False
            self._post = []

    def is_busy_or_pending(self) -> bool:
        with self._lk:
            return self._busy or self._pending

    def cursor_moved(self) -> bool:
        with self._lk:
            return self._cursor_mv

    def post_text(self) -> str:
        with self._lk:
            return "".join(self._post)

    def set_busy(self, v: bool):
        with self._lk:
            self._busy = v

    def snapshot_post_and_set_busy(self) -> str:
        with self._lk:
            self._busy = True
            return "".join(self._post)
