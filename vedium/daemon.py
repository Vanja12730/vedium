# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

import os
import sys
import re
import threading
import time
import queue
import functools
import pystray
from pynput.keyboard import Key, KeyCode
import pynput.keyboard as _pynput_kb

from vedium import diagnostics
from vedium.constants import (
    _OS_RESERVED_HOTKEYS,
    _CTRL_KEYS,
    _rephrase_history,
    FORMATS,
    _is_mid_token,
    _resolve_trigger_mode,
    _is_answer_mode,
)
from vedium.config import load_cfg, save_cfg, resolve_prompt_format
from vedium.language import is_same_language
from vedium.buffer import _Buffer
from vedium.replacement import (
    _kbctrl,
    _active_window_token,
    _copy_selected_text,
    _do_replace,
    _do_replace_rephrase,
    _do_replace_selected_text,
)
from vedium.llm import (
    _stream_cancel,
    rephrase_sentence_stream,
    _streaming_answer,
    _clean_llm_output,
    _safe_model_correction,
    _capitalize_sentences,
    _similarity,
    _word_count,
    spell_check_sentence,
    load_model,
    connect_server,
    detect_local_server,
    connect_cloud,
    connect_cli,
)
from vedium.cli_models import CLIModelError
from vedium.managed_server import stop_managed_server
from vedium.ui_toasts import (
    _Toast,
    _EditableToast,
    _save_generated_style,
    _open_recent_rephrases,
    _info_toast,
)
from vedium.ui_thread import _ensure_ui_thread, _ui_call
from vedium.ui_settings import _open_settings
from vedium.ui_chat import _open_chat_window
from vedium.ui_tray import _make_icon


def _surface_cli_error(exc: Exception) -> None:
    if isinstance(exc, CLIModelError):
        _info_toast(f"AI CLI error: {exc}")


@functools.lru_cache(maxsize=None)
def _str_to_key(s: str):
    """Convert a hotkey string to the corresponding pynput Key or KeyCode, or None.

    Only non-printable special keys are accepted as hotkeys (f1-f12, insert, pause, …);
    they are stored by their pynput Key enum name, e.g. 'f8'.
    The KeyCode.from_char branch below is kept for back-compat with configs that may
    have a single printable character saved from an older version, but the Settings UI
    no longer allows setting such keys (they pass through to the active app).

    lru_cache makes repeated calls (every keypress in _on_press) free after the
    first resolution — no getattr + try/except overhead on the hot path.
    """
    if not s:
        return None
    s = s.strip().lower()
    # Special key — matches a pynput Key enum attribute (f1, insert, pause, etc.)
    try:
        return getattr(Key, s)
    except AttributeError:
        pass
    # Single printable character
    if len(s) == 1:
        try:
            return KeyCode.from_char(s)
        except Exception:
            pass
    return None


# Curly quotes the model may swap for straight ones (or vice versa) while
# changing nothing else — cosmetic noise, not a correction.
_QUOTE_TRANS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def _noise_normalized(s: str) -> str:
    """Normalise trivial LLM output noise so a no-op correction compares equal:
    strips the ends, collapses whitespace runs, and maps curly quotes to
    straight ones. Any difference that survives this is a real correction."""
    return re.sub(r"\s+", " ", s.strip()).translate(_QUOTE_TRANS)


class SpellDaemon:
    def __init__(self):
        self.cfg       = load_cfg()
        diagnostics.set_status(
            enabled=self.cfg.get("enabled", True),
            spell_check_mode=self.cfg.get("spell_check_mode", "Auto"),
            backend_mode=self.cfg.get("backend_mode", "local"),
        )
        diagnostics.record("app", "info", "Daemon initialized.")
        self.buf       = _Buffer()
        self._icon     = None
        self._listener = None
        self._active_rephrase_toast = None             # _EditableToast currently shown, or None
        self._active_rephrase_lock  = threading.Lock() # guards _active_rephrase_toast
        self._active_spell_toast    = None             # _Toast currently shown, or None
        self._active_spell_toast_lock = threading.Lock() # guards _active_spell_toast
        self._ctrl_held             = False            # True while Ctrl/Cmd is held down
        self._rephrase_hotkey_lock  = threading.Lock() # prevents concurrent rephrase threads
        self._sentence_queue        = queue.Queue()
        self._sentence_worker       = None
        self._stop_sentence_worker  = threading.Event()

    def _dismiss_rephrase_toast(self, toast):
        """Dismiss a failed generation without clearing a newer active toast."""
        if toast is not None:
            toast.close()
        with self._active_rephrase_lock:
            if self._active_rephrase_toast is toast:
                self._active_rephrase_toast = None

    def _start_sentence_worker(self):
        """Start the single spell-check worker that drains sentence jobs in order."""
        if self._sentence_worker and self._sentence_worker.is_alive():
            return
        self._sentence_worker = threading.Thread(
            target=self._sentence_worker_loop,
            daemon=True,
        )
        self._sentence_worker.start()

    def _sentence_worker_loop(self):
        """Drain queued sentence corrections sequentially."""
        while not self._stop_sentence_worker.is_set():
            try:
                job = self._sentence_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                if job is None:
                    return
                self._pipeline(*job)
            except Exception:
                # Keep the queue worker alive even if one LLM/replacement run fails.
                pass
            finally:
                self._sentence_queue.task_done()

    def _enqueue_sentence(self, sentence: str, punct: str, raw_len: int,
                          leading_ws: str = "", target_window: int | None = None):
        """Queue one completed sentence for the sequential spell-check pipeline."""
        self._sentence_queue.put((sentence, punct, raw_len, leading_ws, target_window))

    # ── Keyboard listener ─────────────────────────────────────────────────────

    def _on_press(self, key):
        # pynput stops the listener thread if a callback raises, which would
        # silently kill ALL keyboard handling until the app is restarted.
        # Catch everything, log it, and keep listening.
        try:
            self._on_press_impl(key)
        except Exception as exc:
            diagnostics.record_throttled(
                "keyboard", "error", "Key handler failed.", str(exc),
                cooldown_sec=5.0,
            )

    def _on_press_impl(self, key):
        if not self.cfg.get("enabled", True):
            return
        idle_sec = self.cfg.get("idle_reset_sec", 30.0)

        # Understand is deliberately selection-only and can never replace the
        # source. Improve Writing works on a selection or the typing buffer.
        understand_key = _str_to_key(self.cfg.get("understand_hotkey", "f8"))
        if understand_key is not None and key == understand_key:
            threading.Thread(
                target=self._handle_rephrase_hotkey,
                kwargs={"action": "understand"},
                daemon=True,
            ).start()
            return

        writing_key = _str_to_key(self.cfg.get("writing_hotkey", "f9"))
        if writing_key is not None and key == writing_key:
            threading.Thread(
                target=self._handle_rephrase_hotkey,
                kwargs={"action": "write"},
                daemon=True,
            ).start()
            return

        # Check per-mode hotkeys: each mode can have its own dedicated key that
        # triggers a rephrase with that mode forced, bypassing the default mode.
        # _str_to_key is lru_cache'd so repeated calls per keypress are free
        # after the first resolution for each unique hotkey string.
        for mode_name, hk_str in self.cfg.get("mode_hotkeys", {}).items():
            if hk_str.strip().lower() in _OS_RESERVED_HOTKEYS:
                continue
            mk = _str_to_key(hk_str)
            if mk is not None and key == mk:
                threading.Thread(
                    target=self._handle_rephrase_hotkey,
                    kwargs={
                        "action": "understand" if _is_answer_mode(mode_name) else "write",
                        "mode_override": mode_name,
                    },
                    daemon=True,
                ).start()
                return

        try:
            c = key.char
            if c is None:
                return
            if ord(c) < 32:  # drop ASCII control characters (Ctrl+C → \x03, etc.)
                return
            if c in self.cfg.get("trigger_chars", ".!?"):
                # Peek at the buffer before committing to a trigger.
                # If the punctuation looks mid-token (URL, path, code span)
                # treat it as a regular character instead.
                if _is_mid_token(self.buf.peek_sentence(), c):
                    self.buf.key_char(c, idle_sec)
                elif self.cfg.get("spell_check_mode", "Auto") == "Off":
                    # Spell check disabled: record the character normally so the
                    # buffer keeps accumulating text for Improve Writing,
                    # but skip the correction pipeline entirely.
                    diagnostics.record_throttled(
                        "spell", "info",
                        "Spell check is off; sentence correction skipped.",
                        cooldown_sec=20.0,
                    )
                    self.buf.key_char(c, idle_sec)
                elif self.cfg.get("backend_mode") == "cli":
                    # Automatic spell-check is not offered on the AI CLI
                    # backend: per-sentence requests are slow (many seconds)
                    # and each one spends subscription usage. Manual hotkeys
                    # and Chat still work; skip the correction pipeline.
                    diagnostics.record_throttled(
                        "spell", "info",
                        "Automatic spell-check is not available with AI CLI backends; sentence skipped.",
                        cooldown_sec=20.0,
                    )
                    self.buf.key_char(c, idle_sec)
                else:
                    result = self.buf.trigger(c, self.cfg.get("min_word_count", 2))
                    if result:
                        sentence, punct, raw_len, leading_ws = result
                        self._enqueue_sentence(
                            sentence, punct, raw_len, leading_ws,
                            target_window=_active_window_token(),
                        )
                    else:
                        diagnostics.record_throttled(
                            "spell", "info",
                            "Sentence was shorter than the minimum word count; correction skipped.",
                            cooldown_sec=20.0,
                        )
            else:
                self.buf.key_char(c, idle_sec)
        except AttributeError:
            # ── Modifier tracking ──────────────────────────────────────────────
            # Must come first: Ctrl/Cmd key-down sets _ctrl_held so the Enter
            # handler below can detect Ctrl+Enter for the spell toast dispatch.
            if key in _CTRL_KEYS:
                self._ctrl_held = True
                return

            nav = {Key.left, Key.right, Key.up, Key.down,
                   Key.home, Key.end, Key.page_up, Key.page_down}
            if key in nav:
                self.buf.key_nav()
            elif key == Key.esc:
                # Escape dismisses an active spell toast via the global listener
                # so it works even when Electron apps hold keyboard focus.
                with self._active_spell_toast_lock:
                    t = self._active_spell_toast
                if t is not None:
                    t.trigger_ignore()
            elif key == Key.backspace:
                self.buf.key_backspace(idle_sec)
            elif key == Key.space:
                self.buf.key_char(" ", idle_sec)
            elif key == Key.enter:
                # Ctrl+Enter accepts an active spell toast via the global listener
                # so it works even when Electron apps hold keyboard focus.
                # Return immediately WITHOUT calling key_enter — the Enter was
                # intended for the toast, not for the document; calling key_enter
                # here would queue an unwanted newline into _enters_while_busy.
                if self._ctrl_held:
                    with self._active_spell_toast_lock:
                        t = self._active_spell_toast
                    if t is not None:
                        t.trigger_accept()
                        return
                self.buf.key_enter(idle_sec)

    def _on_release(self, key):
        """Clear _ctrl_held when the Ctrl/Cmd key is released."""
        # Same guard as _on_press: an exception here would stop the listener.
        try:
            if not self.cfg.get("enabled", True):
                return
            if key in _CTRL_KEYS:
                self._ctrl_held = False
        except Exception as exc:
            diagnostics.record_throttled(
                "keyboard", "error", "Key handler failed.", str(exc),
                cooldown_sec=5.0,
            )

    def _handle_rephrase_hotkey(self, action: str, mode_override: str | None = None):
        """
        Run one explicit assistant action (or a dedicated per-mode hotkey):
          - understand: require selected text and show an answer card.
          - write: improve selected text, otherwise the current/last buffer.

        mode_override: if provided (by a per-mode hotkey), forces that rephrase mode
        for this single call and skips inline trigger-prefix detection entirely.

        If a review toast is already open when the hotkey fires again, the existing
        toast is dismissed and a fresh rephrase starts — replacing rather than ignoring.
        If the LLM is still running (no toast yet), the second press is ignored.

        The lock is held only during the LLM inference phase (before the toast appears).
        Once the toast is visible the lock is released via on_toast_ready so that a
        subsequent action-key press can close the toast and restart the pipeline.
        """
        # Non-blocking acquire: if the LLM is still generating (pre-toast phase),
        # drop this keypress rather than queuing a second concurrent operation.
        if not self._rephrase_hotkey_lock.acquire(blocking=False):
            return

        lock_released = [False]

        def release_lock():
            if not lock_released[0]:
                lock_released[0] = True
                self._rephrase_hotkey_lock.release()

        try:
            self._handle_rephrase_hotkey_impl(action, on_toast_ready=release_lock,
                                              mode_override=mode_override)
        finally:
            # Safety net: release the lock here if on_toast_ready was never called
            # (e.g. early-exit paths where no toast is shown).
            release_lock()

    def _handle_rephrase_hotkey_impl(self, action: str, on_toast_ready=None,
                                     mode_override: str | None = None):
        if action not in ("understand", "write"):
            raise ValueError(f"Unknown assistant action: {action}")

        if self.buf.is_busy_or_pending():
            # Dismiss a visible review toast so the new press replaces it.
            with self._active_rephrase_lock:
                toast = self._active_rephrase_toast
            if toast is None:
                return  # LLM still running — cannot interrupt
            toast.close()
            # Wait up to 3 s for the pipeline thread to finalize after toast closes.
            deadline = time.time() + 3.0
            while self.buf.is_busy_or_pending() and time.time() < deadline:
                time.sleep(0.05)
            if self.buf.is_busy_or_pending():
                return  # still busy — give up

        selected_text = None
        previous_clipboard = ""
        triggers = self.cfg.get("mode_triggers", {})
        target_window = _active_window_token()
        is_understand = action == "understand"
        default_mode = self.cfg.get(
            "rephrase_reading_mode" if is_understand else "rephrase_writing_mode",
            "What's This About?" if is_understand else "Polish My Writing",
        )
        # Invalid hand-edited config must not cross the safety boundary.
        if _is_answer_mode(default_mode) != is_understand:
            default_mode = "What's This About?" if is_understand else "Polish My Writing"

        def resolve_mode(text: str) -> tuple[str, str]:
            if mode_override and _is_answer_mode(mode_override) == is_understand:
                return mode_override, text
            triggered_mode, clean_text = _resolve_trigger_mode(text, triggers)
            if triggered_mode and _is_answer_mode(triggered_mode) == is_understand:
                return triggered_mode, clean_text
            return default_mode, text

        try:
            # Both actions prefer an explicit selection. Understand stops here
            # if there is none; Improve Writing may fall back to the buffer.
            selected_text, previous_clipboard = _copy_selected_text()
            if selected_text and selected_text.strip():
                resolved_mode, clean_selected = resolve_mode(selected_text)
                # A deliberate selection is valid even when it is one word.
                if _word_count(clean_selected) >= 1:
                    self._selected_rephrase_pipeline(
                        clean_selected, previous_clipboard,
                        on_toast_ready=on_toast_ready,
                        mode_override=resolved_mode,
                        target_window=target_window,
                    )
                    return

            if is_understand:
                diagnostics.record(
                    "understand", "info",
                    "Nothing to understand; select text first.",
                )
                _info_toast("Select some text first, then press the Understand hotkey.")
                return

            result = self.buf.trigger_rephrase(self.cfg.get("min_word_count", 2))
            if result:
                sentence, raw_len = result
                resolved_mode, clean_sentence = resolve_mode(sentence)
                self._rephrase_pipeline(
                    clean_sentence, raw_len,
                    on_toast_ready=on_toast_ready,
                    mode_override=resolved_mode,
                    target_window=target_window,
                )
            else:
                diagnostics.record(
                    "rephrase", "info",
                    "Nothing to improve; select text or type a sentence first.",
                )
                _info_toast("Nothing to improve — select text or type a sentence first.")
        finally:
            selected_text = None
            previous_clipboard = ""

    # ── Spell-check pipeline ──────────────────────────────────────────────────

    def _pipeline(self, sentence: str, punct: str, raw_len: int,
                  leading_ws: str = "", target_window: int | None = None):
        """
        Runs in a background thread. Spell-checks `sentence`, then either:
          - silently replaces (spell_check_mode=Auto, cursor didn't move, fast enough), or
          - shows a toast and waits for the user to accept/ignore.
        raw_len is the unstripped character count of sentence as it sits in the
        document, used by _do_replace for the exact Shift+Left selection.

        A try/except wraps the entire body so that any exception from the LLM
        (model crash, OOM, llama.cpp error) still calls buf.finalize(), preventing
        _pending from being stuck True forever and silently halting all processing.
        """
        t0 = time.time()
        diagnostics.set_status(busy=True)

        try:
            working_lang   = self.cfg.get("working_language", "Auto")
            # Same predicate spell_check_sentence uses to pick its prompt: if
            # these disagree the text gets corrected while the safety net is
            # relaxed as though it had been translated.
            is_translation = not is_same_language(sentence, working_lang)
            diacr_only = self.cfg.get("diacritics_only_auto", False)
            # Spell-check is a single non-streaming completion, so a cloud
            # quota failover happens before any toast exists. Collect the
            # switches here and show them on the toast once it is up, so the
            # user knows the suggestion came from a different model than the
            # one they selected.
            cloud_switches: list[tuple[str, str]] = []
            corrected = spell_check_sentence(
                sentence, resolve_prompt_format(self.cfg), working_lang,
                diacritics_only=diacr_only, cfg=self.cfg,
                on_fallback=lambda failed, nxt: cloud_switches.append((failed, nxt)),
            )
            if corrected is None:
                # Model never consulted (backend still connecting, or CLI
                # spell-check gate) — skip entirely so the capitalizer below
                # can't fabricate a phantom case-only suggestion.
                self.buf.finalize(replaced=False)
                return
            corrected = _safe_model_correction(
                sentence, corrected,
                is_translation=is_translation,
            )
            # Capitalize the first letter of each sentence deterministically here
            # too: _safe_model_correction may have fallen back to the (lowercase)
            # original when the model refused or returned a meta-response, in which
            # case spell_check_sentence's own capitalization was discarded. This
            # guarantees capitalization regardless of model behaviour. Skipped for
            # diacritics-only mode, which must make zero structural changes.
            if not diacr_only:
                corrected = _capitalize_sentences(corrected)

            # Strip trailing sentence-ending punctuation the model sometimes echoes
            # (avoids double dots). Do NOT strip commas/semicolons/colons here; they
            # may be intentional internal punctuation.
            triggers  = self.cfg.get("trigger_chars", ".!?")
            corrected = corrected.rstrip(triggers)

            # Compare against the original to decide whether a replacement is
            # actually needed.  spell_check_sentence() capitalizes the first letter
            # of each sentence, so a lowercase-only sentence comes back differing
            # only in case.
            sentence_cmp = sentence

            # A change that is ONLY capitalization (identical text ignoring case,
            # after collapsing whitespace) must bypass the similarity gate below:
            # _similarity() lowercases both sides, so a first-letter capitalization
            # scores 1.0 and would be wrongly treated as "no change". This is why
            # "word word word." was never capitalized.
            def _collapse_ws(s: str) -> str:
                return re.sub(r"\s+", " ", s.strip())
            _co, _se = _collapse_ws(corrected), _collapse_ws(sentence_cmp)
            case_only_fix = (_co != _se and _co.lower() == _se.lower())

            # Nothing changed — no replacement needed.
            # "No change" means the texts are identical after normalising the
            # trivial noise models emit on already-correct input (whitespace
            # runs, curly↔straight quotes) — see _noise_normalized.  A flat
            # similarity threshold (≥0.97) sat here before, but a one-typo fix
            # in a sentence of ~9 words or more scores above any flat cutoff
            # ("recieved"→"received" in 9 words is 0.978), so the most common
            # genuine corrections were silently discarded as noise.
            sc_mode = self.cfg.get("spell_check_mode", "Auto")
            if not case_only_fix and (
                _noise_normalized(corrected) == _noise_normalized(sentence_cmp)
            ):
                diagnostics.record("spell", "info", "No correction needed.")
                self.buf.finalize(replaced=False)
                return

            # Toast-mode safety: if the model returned something that looks
            # nothing like the original sentence (e.g. a meta-response like
            # "Please provide the text you would like me to translate and
            # correct."), treat it as unchanged and suppress the toast.
            # _safe_model_correction already applies a similarity floor for
            # non-translation mode, but translation mode skips that check.
            # We also apply this guard in Toast mode for all modes, since
            # showing a garbage suggestion is worse than showing nothing.
            if sc_mode == "Toast" and _similarity(sentence_cmp, corrected) < 0.40:
                diagnostics.record("spell", "warning", "Suggestion hidden because it looked unsafe.")
                self.buf.finalize(replaced=False)
                return

            elapsed        = time.time() - t0
            post           = self.buf.post_text()
            cursor_mv      = self.buf.cursor_moved()
            max_wait       = self.cfg.get("max_wait_sec", 12)
            too_slow       = elapsed > max_wait
            if too_slow:
                diagnostics.record(
                    "spell", "warning",
                    "Correction took too long; showing review toast instead of replacing automatically.",
                    f"{elapsed:.1f}s > {max_wait}s",
                )
            if cursor_mv:
                diagnostics.record(
                    "spell", "warning",
                    "Cursor moved during correction; showing review toast instead of replacing automatically.",
                )

            # case_only_fix only bypasses the no-change gate above; in Toast
            # mode every correction — including capitalization-only — must go
            # through the review toast, never a silent replace.
            if sc_mode == "Auto" and not cursor_mv and not too_slow:
                # Silent replace — re-read post atomically with set_busy so that a
                # space (or any char) typed between our earlier post_text() snapshot
                # and the selection cannot shift the cursor and corrupt the replace.
                post = self.buf.snapshot_post_and_set_busy()
                _do_replace(
                    sentence, corrected, punct, post, raw_len, leading_ws,
                    expected_window=target_window,
                )
                queued_enters = self.buf.drain_queued_enters()
                self.buf.finalize(replaced=True, completed_text=corrected + punct)
                diagnostics.record("replacement", "info", "Sentence replaced automatically.")
                for _ in range(queued_enters):
                    _kbctrl.press(Key.enter)
                    _kbctrl.release(Key.enter)
            else:
                # Show toast — let the user decide
                ev             = threading.Event()
                _accepted      = [False]
                _accepted_text = [""]

                def accept(final_text: str = ""):
                    _accepted_text[0] = final_text
                    _accepted[0] = True
                    ev.set()

                def ignore():
                    ev.set()

                # Set busy BEFORE the toast appears so that the Ctrl+Enter keystroke
                # that accepts the toast is routed to _enters_while_busy by key_enter
                # instead of appending a phantom "\n" to _post.  The toast grabs
                # keyboard focus immediately, so no real typing can land in _post after
                # this point anyway.  drain_queued_enters() re-emits held Enters once
                # the replacement is done — the same pattern used by the silent path.
                self.buf.set_busy(True)
                toast = _Toast(sentence + punct, corrected + punct, accept, ignore,
                               fmt=resolve_prompt_format(self.cfg), cfg=self.cfg)
                if cloud_switches:
                    # First model that failed → model that actually answered.
                    toast.show_model_switch(cloud_switches[0][0], cloud_switches[-1][1])
                with self._active_spell_toast_lock:
                    self._active_spell_toast = toast
                # timeout=65: 5 s grace over the 9 s auto-close (which hover-protection
                # can restart indefinitely).  Matches the _EditableToast/ev.wait pattern
                # and prevents the pipeline from exiting while the toast is still alive.
                ev.wait(timeout=65)
                with self._active_spell_toast_lock:
                    self._active_spell_toast = None

                if _accepted[0]:
                    # Use the text the user actually accepted.  If they used the in-toast
                    # AI chat panel, _accepted_text[0] holds the AI-refined version;
                    # otherwise it holds the original LLM suggestion (corrected + punct).
                    # Strip the trailing punct so _do_replace receives the same
                    # (corrected, punct) pair it always expects.
                    full_accepted = _accepted_text[0] or (corrected + punct)
                    if punct and full_accepted.endswith(punct):
                        corrected = full_accepted[:-len(punct)]
                    else:
                        corrected = full_accepted.rstrip(self.cfg.get("trigger_chars", ".!?"))
                    # Use the atomic snapshot+busy helper for the same reason as
                    # the silent-replace path: a character typed between set_busy()
                    # and post_text() would be missed, making the selection too short.
                    # (set_busy was already called above, so this is a no-op on the
                    # flag itself — it just provides the atomic post snapshot.)
                    post2 = self.buf.snapshot_post_and_set_busy()
                    # from_toast=True: the toast window had keyboard focus; pass the
                    # flag so _do_replace uses the longer focus-return delay and clears
                    # the clipboard afterwards rather than restoring the old Ctrl+C copy.
                    _do_replace(sentence, corrected, punct, post2, raw_len, leading_ws,
                                from_toast=True,
                                expected_window=target_window)
                    queued_enters = self.buf.drain_queued_enters()
                    self.buf.finalize(replaced=True, completed_text=corrected + punct)
                    diagnostics.record("replacement", "info", "Toast suggestion accepted.")
                    for _ in range(queued_enters):
                        _kbctrl.press(Key.enter)
                        _kbctrl.release(Key.enter)
                else:
                    diagnostics.record("replacement", "info", "Toast suggestion ignored.")
                    self.buf.finalize(replaced=False)

        except Exception as exc:
            # LLM crash, OOM, or any other unexpected error — release the pending
            # lock so the daemon can continue processing future sentences.
            diagnostics.record("spell", "error", "Spell-check pipeline failed.", str(exc))
            _surface_cli_error(exc)
            self.buf.finalize(replaced=False)
            raise
        finally:
            diagnostics.set_status(busy=False)

    # ── Rephrase pipeline ─────────────────────────────────────────────────────

    def _rephrase_pipeline(self, sentence: str, raw_len: int, *, on_toast_ready=None,
                             mode_override: str | None = None,
                             target_window: int | None = None):
        """
        Runs in a background thread when Improve Writing is pressed.
        Always shows a toast so the user can review before committing.
        raw_len is the unstripped character count of sentence as it sits in the
        document, used by _do_replace_rephrase for the exact Shift+Left selection.

        mode_override: if set (by an inline trigger prefix), overrides the
        configured writing mode for this single call only.

        A try/except wraps the entire body so that any exception from the LLM
        (model crash, OOM, llama.cpp error) still calls buf.finalize(), preventing
        _pending from being stuck True forever and silently halting all processing.

        ev.wait uses timeout=65 (5 s grace over the _EditableToast 60 s auto-close)
        so the thread cannot hang forever if _show() raises silently as a fire-and-
        forget _ui_call — without a timeout, ev.set() would never be called in that
        case and _pending would remain True indefinitely.

        on_toast_ready: optional callable invoked once the toast is fully populated
        and waiting for user input.  The caller uses this to release the rephrase
        hotkey lock so a subsequent action-key press can close the toast and restart.
        """
        toast = None
        diagnostics.set_status(busy=True)
        try:
            mode      = mode_override or self.cfg.get("rephrase_writing_mode", "Polish My Writing")
            is_answer = _is_answer_mode(mode)
            fmt       = resolve_prompt_format(self.cfg)
            ev        = threading.Event()
            _accepted = [False]
            _edited   = ["Generating suggestion..."]

            def accept(edited: str):
                _edited[0] = edited
                _accepted[0] = True
                ev.set()

            def ignore():
                ev.set()

            label = mode if is_answer else f"Improve Writing  ({mode})"
            toast = _EditableToast(_edited[0], accept, ignore, label=label, original=sentence, fmt=fmt,
                                   answer_only=is_answer, cfg=self.cfg)
            with self._active_rephrase_lock:
                self._active_rephrase_toast = toast

            chunks = []
            last_visible = [None]
            _stream_cancel.clear()
            for chunk in rephrase_sentence_stream(
                    sentence, fmt, mode, self.cfg.get("working_language", "Auto"),
                    creativity=self.cfg.get("rephrase_creativity", 0.35), cfg=self.cfg,
                    on_fallback=toast.show_model_switch):
                chunks.append(chunk)
                # Hide chain-of-thought while streaming: show "Thinking…"
                # until the answer emerges, then render the answer-so-far.
                visible, thinking = _streaming_answer("".join(chunks))
                shown = visible if visible else "💭 Thinking…"
                if shown != last_visible[0]:
                    last_visible[0] = shown
                    toast.set_text(shown)

            corrected = "".join(chunks).strip()
            corrected = _clean_llm_output(corrected)

            # For the diacritics-only mode, be conservative and reject structural damage.
            # Creative rephrase modes are allowed to rewrite more freely.
            if mode == "Diacritics Only":
                corrected = _safe_model_correction(
                    sentence, corrected,
                )
            # The model handles capitalization itself; compare directly against
            # the original to decide whether anything actually changed.
            # Answer modes skip this: their output is an answer about the text,
            # so "output == input" is not a meaningful no-op signal.
            sentence_cmp = sentence
            if not is_answer and corrected == sentence_cmp:
                toast.close()
                with self._active_rephrase_lock:
                    self._active_rephrase_toast = None
                diagnostics.record("rephrase", "info", "No rephrase change needed.")
                self.buf.finalize(replaced=False)
                return

            # Save to in-memory history so the user can recover a dismissed toast
            # via the tray "Recent Assistant Results…" menu entry.
            _rephrase_history.appendleft({
                "text":   corrected,
                "mode":   mode,
                "source": sentence[:60].strip(),
            })

            toast.set_text(corrected, select=True)
            # LLM is done and the toast is fully populated — release the hotkey lock
            # so a subsequent action-key press can close this toast and start fresh.
            if on_toast_ready:
                on_toast_ready()
            # timeout=65: 5 s grace window over the _EditableToast 60 s auto-close.
            # Prevents an infinite hang if _show() raises internally as a fire-and-
            # forget _ui_call (error silently dropped → ev.set() never called).
            ev.wait(timeout=65)
            with self._active_rephrase_lock:
                self._active_rephrase_toast = None

            if _accepted[0]:
                final_text = _edited[0]
                # Use the atomic snapshot+busy helper so that a keypress arriving
                # between set_busy(True) and post_text() cannot be missed — the
                # same race that was fixed in _pipeline's silent-replace path.
                post = self.buf.snapshot_post_and_set_busy()
                # from_toast=True is the default; explicit here for clarity.
                # The _EditableToast had focus when Replace was clicked, so the
                # longer focus-return delay and clipboard-clear behaviour apply.
                _do_replace_rephrase(
                    sentence, final_text, post, raw_len,
                    from_toast=True,
                    expected_window=target_window,
                )
                queued_enters = self.buf.drain_queued_enters()
                self.buf.finalize(replaced=True, completed_text=final_text)
                diagnostics.record("rephrase", "info", "Rephrase accepted.")
                for _ in range(queued_enters):
                    _kbctrl.press(Key.enter)
                    _kbctrl.release(Key.enter)
                final_text = ""
            else:
                diagnostics.record("rephrase", "info", "Rephrase ignored.")
                self.buf.finalize(replaced=False)

        except Exception as exc:
            # LLM crash, OOM, or any other unexpected error — release the pending
            # lock so the daemon can continue processing future sentences.
            self._dismiss_rephrase_toast(toast)
            diagnostics.record("rephrase", "error", "Rephrase pipeline failed.", str(exc))
            _surface_cli_error(exc)
            self.buf.finalize(replaced=False)
            raise
        finally:
            diagnostics.set_status(busy=False)

    # ── Selected-text rephrase pipeline

    def _selected_rephrase_pipeline(self, selected_text: str, previous_clipboard: str,
                                     *, on_toast_ready=None,
                                     mode_override: str | None = None,
                                     target_window: int | None = None):
        """
        Process currently selected text, show a review box, and replace the
        selection only if accepted. Selected/replacement text is kept only in
        memory during this function and never saved to config or disk.

        mode_override: if set (by an inline trigger prefix), overrides the
        configured reading mode for this single call only.

        on_toast_ready: optional callable invoked once the toast is fully populated
        and waiting for user input.  The caller uses this to release the rephrase
        hotkey lock so a subsequent action-key press can close the toast and restart.
        """
        corrected = ""
        toast = None
        diagnostics.set_status(busy=True)
        try:
            mode = mode_override or self.cfg.get("rephrase_reading_mode", "What's This About?")
            is_answer = _is_answer_mode(mode)
            fmt  = resolve_prompt_format(self.cfg)
            ev        = threading.Event()
            _accepted = [False]
            _edited   = ["Generating suggestion..."]

            def accept(edited: str):
                _edited[0] = edited
                _accepted[0] = True
                ev.set()

            def ignore():
                ev.set()

            label = mode if is_answer else f"Improve selected text  ({mode})"
            on_save_style = (
                (lambda generated, parent: _save_generated_style(self.cfg, generated, parent))
                if mode == "Extract Writing Style" else None
            )
            toast = _EditableToast(
                _edited[0], accept, ignore, label=label, original=selected_text, fmt=fmt,
                answer_only=is_answer, cfg=self.cfg, on_save_style=on_save_style,
            )
            with self._active_rephrase_lock:
                self._active_rephrase_toast = toast
            # Mark the buffer busy NOW — before the LLM starts — so that
            # is_busy_or_pending() returns True for the entire duration of this
            # pipeline (LLM inference + user review window).  Without this,
            # _handle_rephrase_hotkey_impl's guard is bypassed on a second action-key press
            # press (the selected-text path never calls trigger_rephrase(), so
            # _pending is never set), allowing a second pipeline to spawn
            # concurrently and leaving the first toast orphaned.  Both pipelines
            # would then attempt _do_replace_selected_text, corrupting the document.
            # The finally block always calls set_busy(False) to release the flag.
            self.buf.set_busy(True)

            chunks = []
            last_visible = [None]
            _stream_cancel.clear()
            for chunk in rephrase_sentence_stream(
                    selected_text, fmt, mode, self.cfg.get("working_language", "Auto"),
                    creativity=self.cfg.get("rephrase_creativity", 0.35), cfg=self.cfg,
                    on_fallback=toast.show_model_switch):
                chunks.append(chunk)
                # Hide chain-of-thought while streaming: show "Thinking…"
                # until the answer emerges, then render the answer-so-far.
                visible, thinking = _streaming_answer("".join(chunks))
                shown = visible if visible else "💭 Thinking…"
                if shown != last_visible[0]:
                    last_visible[0] = shown
                    toast.set_text(shown)

            corrected = "".join(chunks).strip()
            corrected = _clean_llm_output(corrected)

            if mode == "Diacritics Only":
                corrected = _safe_model_correction(
                    selected_text, corrected,
                )

            # Save to in-memory history so the user can recover a dismissed toast
            # via the tray "Recent Assistant Results…" menu entry.
            _rephrase_history.appendleft({
                "text":   corrected,
                "mode":   mode,
                "source": selected_text[:60].strip(),
            })

            toast.set_text(corrected, select=True)
            # LLM is done and the toast is fully populated — release the hotkey lock
            # so a subsequent action-key press can close this toast and start fresh.
            if on_toast_ready:
                on_toast_ready()
            ev.wait(timeout=65)
            with self._active_rephrase_lock:
                self._active_rephrase_toast = None

            if _accepted[0]:
                final_text = _edited[0]
                # _busy was already set at pipeline entry (before the LLM loop).
                # No need to call set_busy(True) again here.
                _do_replace_selected_text(
                    final_text, previous_clipboard,
                    expected_window=target_window,
                )
                diagnostics.record("rephrase", "info", "Selected text rephrase accepted.")
                final_text = ""
            else:
                diagnostics.record("rephrase", "info", "Selected text rephrase ignored.")
        except Exception as exc:
            self._dismiss_rephrase_toast(toast)
            message = (
                "Selected-text CLI request failed."
                if isinstance(exc, CLIModelError)
                else "Selected-text rephrase failed."
            )
            diagnostics.record("rephrase", "error", message, str(exc))
            _surface_cli_error(exc)
            raise
        finally:
            selected_text = ""
            corrected = ""
            previous_clipboard = ""
            self.buf.set_busy(False)
            diagnostics.set_status(busy=False)


    # ── Tray icon callbacks ───────────────────────────────────────────────────

    def _toggle(self, icon, item):
        self.cfg["enabled"] = not self.cfg.get("enabled", True)
        save_cfg(self.cfg)
        diagnostics.set_status(enabled=self.cfg["enabled"])
        diagnostics.record(
            "app", "info",
            "App enabled." if self.cfg["enabled"] else "App disabled.",
        )
        icon.icon  = _make_icon(self.cfg["enabled"])
        icon.title = f"Vedium — {'ON' if self.cfg['enabled'] else 'OFF'}"

    def _open_settings(self, icon, item):
        def _on_cfg_saved(new_cfg: dict):
            # Update the running daemon's config in place so changes take
            # effect immediately — no restart needed.
            # _str_to_key is lru_cache'd, so new hotkey strings are resolved
            # automatically on the next keypress — no explicit cache flush needed.
            self.cfg.update(new_cfg)
            diagnostics.set_status(
                enabled=self.cfg.get("enabled", True),
                spell_check_mode=self.cfg.get("spell_check_mode", "Auto"),
                backend_mode=self.cfg.get("backend_mode", "local"),
            )
            diagnostics.record("config", "info", "Settings updated.")
        _open_settings(self.cfg, _on_cfg_saved, load_model)

    def _recent_rephrases(self, icon, item):
        """Tray callback: open the recent Assistant-results recovery popup."""
        _open_recent_rephrases()

    def _open_chat(self, icon, item):
        """Tray callback: open the standalone AI chat window."""
        _open_chat_window(self.cfg)

    def _quit(self, icon, item):
        self._stop_sentence_worker.set()
        self._sentence_queue.put(None)
        stop_managed_server()
        icon.stop()
        if self._listener:
            self._listener.stop()
        os._exit(0)

    def _record_startup_readiness(self):
        """Record plain-language startup hints for the Settings Status tab."""
        if not self.cfg.get("enabled", True):
            diagnostics.record("status", "warning", "App is disabled from the tray.")

        if self.cfg.get("spell_check_mode", "Auto") == "Off":
            diagnostics.record("status", "warning", "Spell check is off.")

        mode = self.cfg.get("backend_mode", "local")
        if mode == "cloud":
            if not self.cfg.get("cloud_api_key", "").strip():
                diagnostics.record("backend", "warning", "Cloud API key is missing.")
            if not self.cfg.get("cloud_model", "").strip():
                diagnostics.record("backend", "warning", "Cloud model is not selected.")
        elif mode == "cli":
            if not self.cfg.get("cli_models_enabled", False):
                diagnostics.record("backend", "warning", "Command-line models are switched off.")
            if not self.cfg.get("cli_model", "").strip():
                diagnostics.record("backend", "warning", "Command-line model is not selected.")
        elif mode == "local":
            path = self.cfg.get("model_path", "")
            if not path:
                diagnostics.record("backend", "warning", "Local model path is empty.")
            elif not os.path.isfile(path):
                diagnostics.record("backend", "warning", "Local model file was not found.", path)
        elif mode == "server" and not self.cfg.get("server_url", "").strip():
            diagnostics.record("backend", "info", "No server URL set; auto-detect may connect if a local server is running.")

    def _startup_backend(self):
        """Pick and initialise the LLM backend at startup, honouring the user's
        explicit `backend_mode` choice instead of letting auto-detect override it.

          • "cloud"  → connect to the configured Cloud API. On failure, open
                       Settings rather than silently switching to a local backend.
          • "local"  → load the configured GGUF model. An explicitly-chosen local
                       model is NOT overridden by a running llama-server. (If no
                       local model is configured — e.g. a legacy config that
                       defaulted to "local" — fall through to server/auto-detect
                       so those users keep working.)
          • "server" / other → explicit server_url, then auto-detect, then local.

        Runs on a background thread; each step is best-effort and logs failures.
        """
        n_ctx      = self.cfg.get("n_ctx", 4096)
        server_url = self.cfg.get("server_url", "").strip()
        path       = self.cfg.get("model_path", "")
        mode       = self.cfg.get("backend_mode", "local")
        have_local = bool(path and os.path.isfile(path))
        auto       = self.cfg.get("auto_detect_server", True)
        diagnostics.set_status(backend_mode=mode, backend="None")
        diagnostics.record("backend", "info", f"Starting backend setup: {mode}.")

        def _connect_server_url() -> bool:
            if not server_url:
                return False
            try:
                backend = connect_server(server_url, cfg_n_ctx=n_ctx)
                diagnostics.set_status(backend=backend)
                return True
            except Exception as exc:
                diagnostics.record("backend", "warning", "Configured server unreachable.", str(exc))
                print(f"[Vedium] Configured server {server_url} unreachable: {exc}",
                      file=sys.stderr)
            return False

        def _connect_auto() -> bool:
            if not auto:
                return False
            detected = detect_local_server()
            if detected:
                try:
                    backend = connect_server(detected, cfg_n_ctx=n_ctx)
                    diagnostics.set_status(backend=backend)
                    return True
                except Exception as exc:
                    diagnostics.record("backend", "warning", "Auto-detected server failed.", str(exc))
                    print(f"[Vedium] Auto-detected server {detected} failed: {exc}",
                          file=sys.stderr)
            return False

        def _load_local() -> bool:
            if not have_local:
                return False
            try:
                backend = load_model(path)
                diagnostics.set_status(backend=backend)
                return True
            except Exception as exc:
                diagnostics.record("backend", "warning", "Local model load failed.", str(exc))
                print(f"[Vedium] Local model load failed: {exc}", file=sys.stderr)
            return False

        def _settings():
            _open_settings(self.cfg, lambda c: self.cfg.update(c), load_model)

        # AI CLI selection is detection-only and spends no subscription request.
        if mode == "cli":
            try:
                backend = connect_cli(
                    self.cfg.get("cli_model", ""),
                    bool(self.cfg.get("cli_models_enabled", False)),
                    self.cfg.get("cli_reasoning_effort", ""),
                )
                diagnostics.set_status(backend=backend)
                return
            except Exception as exc:
                diagnostics.record("backend", "error", "AI CLI selection failed.", str(exc))
                print(f"[Vedium] AI CLI selection failed: {exc}", file=sys.stderr)
            # The selected CLI never falls back to another backend.
            _settings()
            return

        # 1. Cloud — only when explicitly selected (keeps the app offline-first).
        if mode == "cloud":
            try:
                backend = connect_cloud(
                    self.cfg.get("cloud_base_url", ""),
                    self.cfg.get("cloud_api_key", ""),
                    self.cfg.get("cloud_model", ""),
                    cfg_n_ctx=max(n_ctx, 8192),
                )
                diagnostics.set_status(backend=backend)
                return
            except Exception as exc:
                diagnostics.record("backend", "error", "Cloud API connection failed.", str(exc))
                print(f"[Vedium] Cloud API connection failed: {exc}", file=sys.stderr)
            # Don't silently fall back to a different backend — the user chose
            # cloud, so surface Settings so they can fix the key/model/network.
            _settings()
            return

        # 2. Explicit local choice wins over any running server (no auto-detect hijack).
        if mode == "local" and have_local:
            if _load_local():
                return
            # Configured model failed to load → fall through to the options below.

        # 3. Server-first; also the fallback path for "local" configs with no
        #    usable local model (e.g. legacy configs that defaulted to "local").
        if _connect_server_url(): return
        if _connect_auto():       return
        if _load_local():         return

        # 4. Nothing available — open Settings (thread-safe via _ui_call).
        diagnostics.record("backend", "warning", "No backend is connected.")
        _settings()

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self):
        diagnostics.record("app", "info", "Daemon starting.")
        self._record_startup_readiness()
        # Start the single Tk UI thread before any settings/popup/clipboard work.
        _ensure_ui_thread()

        # Now that the UI thread is live, warn about any reserved hotkeys that
        # were saved in an older config (before the UI guard was introduced).
        # Doing this here — not in __init__ — guarantees _UI_ROOT is ready.
        reserved_in_use = [
            (name, hk)
            for name, hk in self.cfg.get("mode_hotkeys", {}).items()
            if hk.strip().lower() in _OS_RESERVED_HOTKEYS
        ]
        if reserved_in_use:
            names = ", ".join(f'"{n}" ({hk.upper()})' for n, hk in reserved_in_use)
            _info_toast(
                f"⚠ Reserved key in use: {names}. "
                "Open Settings → mode → Change hotkey to fix."
            )

        self._start_sentence_worker()

        # Load model or connect to server in background if already configured;
        # otherwise open Settings so the user can pick one. Detection/connection
        # runs in the background so the tray icon comes up immediately.
        threading.Thread(target=self._startup_backend, daemon=True).start()

        # Start global keyboard listener
        self._listener = _pynput_kb.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.start()

        # Build and run system-tray icon (blocks until Quit)
        enabled = self.cfg.get("enabled", True)
        menu = pystray.Menu(
            pystray.MenuItem(
                lambda item: ("✓  Enabled" if self.cfg.get("enabled") else "   Disabled"),
                self._toggle,
                default=False,
            ),
            pystray.MenuItem("Settings…",          self._open_settings),
            pystray.MenuItem("Chat with AI…",       self._open_chat),
            pystray.MenuItem("Recent Assistant Results…", self._recent_rephrases),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit",               self._quit),
        )
        self._icon = pystray.Icon(
            name  = "Vedium",
            icon  = _make_icon(enabled),
            title = f"Vedium — {'ON' if enabled else 'OFF'}",
            menu  = menu,
        )
        self._icon.run()   # blocks until _quit() calls icon.stop()


# ── 12. ENTRY POINT ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    SpellDaemon().run()
