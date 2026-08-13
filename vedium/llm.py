# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""High-level LLM features: spell check, rephrase, and chat.

The heavy lifting lives in three sibling modules -- llm_state (the active
backend + stream-cancel event), llm_backends (clients, connect/detect,
cloud failover), and llm_postprocess (output cleanup and safety checks).
This module keeps the feature functions and re-exports the names that
consumers and the white-box tests rely on.
"""

import re
import time
import json
import threading

from vedium.constants import _REPHRASE_PROMPTS, _CJK_RE, _word_count, _is_answer_mode
from vedium.config import load_cfg
from vedium.language import detect_language, is_same_language
from vedium.prompt_builder import _build_prompt, _build_chat_prompt
from vedium import diagnostics
from vedium.cli_models import CLIClient, CLIModelError, provenance_footer

# Sibling modules kept reachable as attributes (tests patch their internals).
from vedium import llm_state
from vedium import llm_backends
from vedium import llm_postprocess

from vedium.llm_state import _ActiveBackend, _backend, _stream_cancel
from vedium.llm_backends import (
    _LlamaServerClient,
    _CloudClient,
    _prompt_to_messages,
    _chat_history_to_messages,
    _backend_unavailable_message,
    CLOUD_PROVIDERS,
    connect_cli,
    connect_cloud,
    connect_server,
    fetch_cloud_models,
    detect_local_server,
    load_model,
    _friendly_cloud_error,
    _is_cloud_quota_error,
    _is_cloud_server_error,
    _is_cloud_transient_error,
    _normalise_cloud_url,
    _cloud_favorite_fallbacks,
    _activate_cloud_fallback,
    _cloud_fallback_targets,
    _cloud_fallback_error,
    _record_cloud_failover,
    _cloud_complete_with_fallback,
    _cloud_stream_with_fallback,
    _cli_enabled_for_request,
    _record_cli_provenance,
    _complete_prompt,
    _stream_prompt,
)
from vedium.llm_postprocess import (
    _THINKING_BUDGET_AUTO,
    _strip_channels,
    _strip_reasoning,
    _maybe_flag_reasoning_model,
    _streaming_answer,
    _finish_is_length,
    _reasoning_starved_output,
    _flag_hidden_reasoning,
    _get_thinking_budget,
    _sanitize_bhs_diacritics,
    _match_case,
    _restore_common_bhs,
    _capitalize_sentences,
    _strip_echo_prefix,
    _clean_llm_output,
    _punct_counts,
    _looks_structurally_bad,
    _similarity,
    _word_count_ok,
    _looks_like_meta_response,
    _safe_model_correction,
)


def _language_instruction(working_language: str) -> str:
    """
    Return a prompt instruction for output language control.

    Auto  → keep the output in whatever language the input is in.
    Named → always output in that language, translating naturally if the
            input is in a different language.  The instruction is intentionally
            unconditional ("regardless of the input language") so the model
            doesn't second-guess itself and sometimes leave the text untranslated.
    """
    lang = (working_language or "Auto").strip()
    if lang == "Auto":
        return (
            "Keep the output in the same language as the input text. "
            "Do not translate unless the mode explicitly requires it. "
        )
    if lang == "BHS":
        return (
            "Always return the result in natural, fluent BHS (Bosnian/Croatian/Serbian, ijekavica standard), "
            "regardless of what language the input is written in. "
            "If the input is in a different language, translate it to BHS "
            "while also correcting any grammar, spelling, or punctuation errors. "
        )
    return (
        f"Always return the result in natural, fluent {lang}, "
        f"regardless of what language the input is written in. "
        f"If the input is in a different language, translate it to {lang} "
        f"while also correcting any grammar, spelling, or punctuation errors. "
    )


def _answer_language_instruction(working_language: str) -> str:
    """
    Language directive for answer modes (What's This About?, Define This, …).

    Unlike _language_instruction, this controls the language of the ANSWER, not
    of a rewritten text — so it must not mention translating or correcting the
    input.

    Auto  → answer in whatever language the passage is in. This asks the model
            to identify the language itself, which it can get wrong: on dense,
            Latinate English (philosophy, academic prose) small models have been
            observed answering in French, Spanish or German. A named language is
            the reliable escape hatch, which is why it must be honoured here —
            this argument was previously ignored, so the Working Language
            setting silently did nothing for every reading mode.
    Named → always answer in that language, whatever the passage is written in.
    """
    lang = (working_language or "Auto").strip()
    if lang == "Auto":
        return (
            "Write your complete answer in the same language as the text. "
            "Do not translate the answer into another language. "
        )
    if lang == "BHS":
        return (
            "Write your complete answer in natural, fluent BHS "
            "(Bosnian/Croatian/Serbian, ijekavica standard), regardless of what "
            "language the text is written in. "
        )
    return (
        f"Write your complete answer in natural, fluent {lang}, "
        f"regardless of what language the text is written in. "
    )


def _is_translation_mode(mode: str) -> bool:
    """Return True for explicit translation modes that manage their own output language."""
    return (mode or "").startswith("Translate ")


# ── correction-first spell prompt ────────────────────────────────────────────
# Used when the text is already in the working language (or the language is
# unknown), so it must be CORRECTED rather than translated. The body is shared;
# only the opening line and the optional BHS diacritic block vary, so a new
# language needs no new prompt.
_CORRECTION_RULES = (
    "Your ONLY job is to fix clear errors: misspelled words, wrong capitalization, "
    "missing or wrong punctuation, and broken spacing. "
)
_CORRECTION_PRESERVE = (
    "IMPORTANT: Preserve all brand names, camelCase words, code variables, URLs, paths, and email addresses exactly as written. Do NOT change their casing or spelling. "
    "Do NOT substitute any correctly spelled word with a synonym — even if you think "
    "a different word sounds better, leave it exactly as written. "
    "Do NOT restructure, rephrase, or reorder any part of the sentence. "
    "Do NOT add words, remove words, or change sentence structure. "
    "Make the smallest possible change to fix each error and leave everything else untouched. "
)
_CORRECTION_TAIL = (
    "Preserve existing commas unless they are clearly wrong. "
    "Ensure there is one space after sentence-ending punctuation before the next sentence. "
    "Return ONLY the corrected text. No explanation. No extra lines. "
    "If there are no errors, return the text exactly as given."
)
# BHS users routinely type without diacritics, so restoring them is part of
# ordinary correction rather than an optional extra. The explicit allow-list and
# the Latin-script line exist because small models otherwise invent accents
# (ǒ, ù, â) or switch to Cyrillic mid-sentence.
_BHS_DIACRITIC_RULES = (
    "The input may be written without some or all diacritics "
    "(e.g. 'c' instead of 'č'/'ć', 's' instead of 'š', 'z' instead of 'ž', 'd' instead of 'đ'). "
    "Restore the correct BHS diacritics where they are missing. "
    "IMPORTANT: The ONLY diacritical characters used in BHS are: š, č, ć, ž, đ "
    "(and their uppercase forms Š, Č, Ć, Ž, Đ). "
    "Do NOT introduce any other Unicode or accented characters (e.g. ǒ, ù, ó, à, â, etc.). "
    "Output BHS in LATIN script only — never use Cyrillic letters. "
    "Do NOT add diacritics to words that are already spelled correctly without them. "
    "Common BHS words that need diacritics: 'cao'->'ćao', 'ces'->'ćeš', 'hoces'->'hoćeš', "
    "'neces'->'nećeš', 'mozes'->'možeš', 'ides'->'ideš', 'jesi'->'jesi' (no change), "
    "'sta'->'šta', 'sto'->'što', 'vise'->'više', 'nase'->'naše', 'vase'->'vaše', "
    "'isao'->'išao', 'dosao'->'došao', 'nasao'->'našao', 'prosao'->'prošao', "
    "'cesto'->'često', 'cesce'->'češće'. "
)
# On Auto the language is unknown, so BHS gets a hint rather than the full block:
# undiacriticised BHS is otherwise indistinguishable from a stream of typos.
_AUTO_BHS_HINT = (
    "If the input appears to be Bosnian, Croatian, or Serbian written without diacritics "
    "(e.g. 'c' instead of 'č'/'ć', 's' instead of 'š', 'z' instead of 'ž', 'd' instead of 'đ'), "
    "restore the correct diacritics throughout the text. "
)


def _correction_sys_msg(lang: str) -> str:
    """Spell-correction prompt for text already written in `lang`.

    `lang` is "Auto" when the language is unknown, which additionally pins the
    output language so the model cannot drift.
    """
    if lang == "Auto":
        return (
            "You are a minimal spell and grammar corrector. "
            + _CORRECTION_RULES + _AUTO_BHS_HINT + _CORRECTION_PRESERVE
            + "Keep the output in the same language as the input text. "
            + _CORRECTION_TAIL
        )
    label = "BHS (Bosnian/Croatian/Serbian)" if lang == "BHS" else lang
    return (
        f"You are a minimal spell and grammar corrector for {label}. "
        + _CORRECTION_RULES
        + (_BHS_DIACRITIC_RULES if lang == "BHS" else "")
        + _CORRECTION_PRESERVE + _CORRECTION_TAIL
    )


def spell_check_sentence(text: str, fmt: str, working_language: str = "Auto", deterministic: bool = False,
                         diacritics_only: bool = False, cfg: dict | None = None, on_fallback=None) -> str | None:
    """
    Run the loaded LLM on `text` and return the grammar/spelling-corrected result.
    Returns None when the model was never consulted (no backend connected yet, or
    the active backend is an AI CLI, which never runs automatic spell-check —
    per-sentence requests are slow and spend subscription usage) — callers must skip the sentence
    entirely rather than treat it as "model saw no errors", otherwise the
    pipeline's deterministic capitalizer fabricates a phantom case-only
    suggestion from text the model never checked.
    deterministic=False uses temperature-based sampling (faster, slightly varied).
    deterministic=True forces temperature=0.0 (greedy decoding — always picks the
    single most probable token, so output is fully reproducible and consistent).

    When working_language is "Auto", the prompt keeps the input language. For a
    named language, positively matching text is corrected minimally; other text
    is translated to the selected language and corrected there.
    """
    with _backend.lock:
        if _backend.client is None:
            diagnostics.set_status(backend="None")
            diagnostics.record_throttled(
                "backend", "warning",
                "No AI backend is connected; correction skipped.",
                cooldown_sec=20.0,
            )
            return None
        if isinstance(_backend.client, CLIClient):
            diagnostics.record_throttled(
                "spell", "info",
                "Automatic spell-check is not available with AI CLI backends; correction skipped.",
                cooldown_sec=20.0,
            )
            return None

        lang = (working_language or "Auto").strip()
        # One predicate decides correct-vs-translate here AND drives the
        # auto-replace safety checks in daemon.py; they must not diverge.
        same_language = is_same_language(text, lang)

        if diacritics_only:
            # Single source of truth: the "Diacritics Only" mode prompt in
            # constants.py (including any user override applied by
            # _merge_custom_modes) drives both the rephrase mode and this
            # auto-correct path, so editing the mode in Settings affects both.
            sys_msg = _REPHRASE_PROMPTS["Diacritics Only"]
            user_msg = f"Restore diacritics only:\n{text}"
            prompt, stop = _build_prompt(fmt, sys_msg, user_msg)
            thinking = _get_thinking_budget()
            max_tok = max(32, _word_count(text) * 2 + 10) + thinking
            out = _complete_prompt(
                prompt, max_tokens=max_tok, temperature=0.0,
                repeat_penalty=1.3, stop=stop, echo=False, cfg=cfg,
                on_fallback=on_fallback,
                messages=[
                    {"role": "system", "content": sys_msg},
                    {"role": "user", "content": user_msg},
                ],
            )
            raw = out["choices"][0]["text"]
            _maybe_flag_reasoning_model(raw)
            raw = _strip_reasoning(raw)
            return raw.strip()

        # Give the model unambiguous spellings for the curated BHS words up
        # front. Output-only cleanup cannot recover a token the model deleted
        # after misreading "cesce" as "ćeš".
        prompt_text = _restore_common_bhs(text) if lang == "BHS" else text

        if lang == "Auto":
            sys_msg = _correction_sys_msg("Auto")
        elif same_language:
            # Correction-first: the text is already in the working language, so a
            # translation-first prompt ("translate German → German") confuses
            # small models and they return the text unchanged, leaving errors —
            # and, for BHS, missing diacritics — uncorrected.
            sys_msg = _correction_sys_msg(lang)
        else:
            # Translation-first prompt: language directive comes first so small
            # models do not prioritise the grammar constraints over translation.
            # Anti-literal instructions ("do not translate word-for-word",
            # "idiomatic phrasing") are essential — without them small models
            # default to word-for-word output even when told to be "fluent".
            sys_msg = (
                f"You are a professional translator and grammar corrector. "
                f"Always output in natural, fluent {lang}, regardless of what language the input is in. "
                f"If the input is in a different language, translate it into {lang}. "
                f"IMPORTANT: Preserve all brand names, camelCase words, code variables, URLs, paths, and email addresses exactly as written. Do NOT change their casing or spelling. "
                f"Do NOT translate word-for-word. Instead, find the natural, idiomatic way "
                f"a native {lang} speaker would express the same idea. "
                f"Use colloquial phrasing where the original is casual; use formal phrasing where the original is formal. "
                f"Preserve the tone, emotion, and nuance of the original — do not flatten or over-formalise it. "
                f"The input may be Bosnian, Croatian, or Serbian written without diacritics "
                f"(e.g. 'c' instead of 'č'/'ć', 's' instead of 'š', 'z' instead of 'ž', 'd' instead of 'đ') — interpret it correctly. "
                f"Always use correct diacritics (š, č, ć, ž, đ) in the output where applicable. "
                f"In all cases, also fix any grammar, spelling, capitalisation, and punctuation errors in the output. "
                f"Do not add, remove, or change the meaning of the text. "
                f"Return ONLY the final {lang} text. No explanation. No extra lines."
            )

        if same_language:
            user_msg = (
                f"Fix only clear spelling, grammar, punctuation, and diacritic errors. "
                f"Do not change any correctly spelled word, do not use synonyms, "
                f"do not rephrase:\n{prompt_text}"
            )
        else:
            user_msg = (
                f"Translate naturally into {lang} — not word-for-word, but how a native speaker would say it. "
                f"The input may be written without diacritics (š->s, č/ć->c, ž->z, đ->d) — interpret it correctly. "
                f"Fix any grammar, spelling, and punctuation in the result:\n{prompt_text}"
            )
        prompt, stop = _build_prompt(fmt, sys_msg, user_msg)
        # Budget: for pure grammar correction the output length ≈ input length,
        # so we cap dynamically.  For translation the target language may be more
        # verbose (e.g. English articles/prepositions vs Bosnian), so add extra slack.
        # Reasoning models (DeepSeek-R1, QwQ, etc.) emit a <think> block before
        # their answer — _get_thinking_budget() adds headroom for that so the
        # actual answer is never crowded out.
        slack = 20 if same_language else 40
        thinking = _get_thinking_budget()
        max_tok = max(32, _word_count(text) * 3 + slack) + thinking
        out = _complete_prompt(
            prompt, max_tokens=max_tok, temperature=0.0 if deterministic else 0.05,
            repeat_penalty=1.3, stop=stop, echo=False, cfg=cfg,
            on_fallback=on_fallback,
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
        )
        raw = out["choices"][0]["text"]
        _maybe_flag_reasoning_model(raw)
        raw = _strip_reasoning(raw)
        result = raw.strip()
        if lang == "BHS":   # script/diacritic clean-up is BHS-specific
            result = _sanitize_bhs_diacritics(result)
            result = _restore_common_bhs(result)
        result = _capitalize_sentences(result)
        return result


def _calc_rephrase_max_tokens(text: str, sys_msg: str = "") -> int:
    """
    Compute a dynamic max_tokens budget for rephrase/translate calls.

    Sizing strategy
    ───────────────
    • Base   : proportional to input word count (×3), floored at 256 so very
               short inputs still get a reasonable budget.  No hard ceiling is
               applied — the model is allowed as many tokens as it needs for
               any mode.
    • Context clamp: the prompt itself also consumes tokens.  We estimate the
               overhead as the system-prompt token count (derived from sys_msg
               when provided, floored at 175 for safety) + input_words × 1.3
               (roughly one token per word plus sub-word splits).  If
               overhead + max_tok would exceed n_ctx we clamp max_tok down so
               the total fits, with a hard floor of 32 to avoid nonsense output.

    The sys_msg parameter should be the fully-constructed system prompt string
    (including any language directives appended by rephrase_sentence_stream).
    Passing it here is critical for custom rephrase modes whose prompts can be
    significantly longer than the 175-token default, which previously caused
    the context window to be overrun and output to be cut off mid-stream.

    The function is called inside _backend.lock in both rephrase_sentence and
    rephrase_sentence_stream, so _backend.client is guaranteed to be non-None here.
    """
    input_words = _word_count(text)

    # Proportional base — no mode-specific ceiling; every mode gets the same
    # headroom and the context-window clamp below is the only hard limit.
    # Floor raised to 512: rephrase/explain modes routinely produce output
    # longer than short inputs would suggest (256 was too tight).
    max_tok = max(512, input_words * 3)

    # Clamp against the live context window so prompt + output always fits.
    # thinking_budget is added to prompt_overhead because a reasoning model
    # consumes those tokens before it produces a single word of actual output —
    # failing to account for them squeezes the visible-answer budget to near zero
    # on short inputs (the original Bug 2).
    # Assigned outside the try: `total` below needs it even when n_ctx is
    # unreadable and the clamp is skipped.
    thinking = _get_thinking_budget()
    try:
        n_ctx = _backend.client.context_params.n_ctx
        # Estimate system-prompt token cost from the actual sys_msg when available.
        # Custom modes can have prompts far longer than 175 tokens; using the real
        # length prevents max_tok from being set higher than the context window can
        # actually accommodate, which caused mid-stream truncation (Bug: custom modes).
        if sys_msg:
            sys_tok_estimate = max(175, int(len(sys_msg.split()) * 1.4))
        else:
            sys_tok_estimate = 175
        prompt_overhead = sys_tok_estimate + int(input_words * 1.3) + thinking
        available = n_ctx - prompt_overhead
        if available < max_tok:
            max_tok = max(32, available)
    except Exception:
        pass  # If n_ctx is unreadable leave max_tok as calculated above

    # thinking tokens are OUTPUT tokens — the model must generate them before
    # producing visible output, so they must be in the generation budget.
    # prompt_overhead already reserves them for context-window safety;
    # adding them here ensures the answer is never crowded out by thinking.
    total = max_tok + thinking
    print(f"[Vedium] rephrase budget: input_words={input_words}, "
          f"max_tok={max_tok}, thinking={thinking}, total={total}", flush=True)
    return total


def rephrase_sentence_stream(text: str, fmt: str, mode: str, working_language: str = "Auto",
                             deterministic: bool = False, creativity: float = 0.35,
                             cfg: dict | None = None, on_fallback=None):
    """
    Run the loaded LLM on `text` and yield rephrased result as raw text chunks.
    The caller is still responsible for final cleanup/safety once all chunks
    have been collected. Falls back to yielding `text` unchanged if no model
    is loaded.

    _backend.lock is held for the entire call including the streaming loop.
    llama.cpp's inference state is not thread-safe: releasing the lock before
    iteration is complete would allow spell_check_sentence (or a concurrent
    chat stream) to call _backend.client(...) at the same time, corrupting model state.

    Early exit: each iteration checks _stream_cancel. When close_with() inside
    _Toast._show sets that event the generator returns immediately, releasing
    _backend.lock without waiting for the model to finish generating.  The pipelines
    call _stream_cancel.clear() before starting a new stream so a stale cancel
    from a previous toast never suppresses a fresh generation.
    """
    with _backend.lock:
        if _backend.client is None:
            diagnostics.set_status(backend="None")
            diagnostics.record_throttled(
                "backend", "warning",
                "No AI backend is connected; rephrase skipped.",
                cooldown_sec=20.0,
            )
            yield text
            return

        base_msg = _REPHRASE_PROMPTS.get(mode)
        if base_msg is None:
            # A stale mode name (hand-edited config, deleted custom mode still
            # referenced somewhere) must not silently run an unrelated prompt.
            diagnostics.record(
                "llm", "warning",
                f'Unknown assistant mode "{mode}" — using "Polish My Writing" instead.',
            )
            mode = "Polish My Writing"
            base_msg = _REPHRASE_PROMPTS[mode]
        lang = (working_language or "Auto").strip()
        if _is_answer_mode(mode):
            # Answer modes respond ABOUT the text, so the language directive
            # governs the answer itself ("explain this in my language") and the
            # user message must not ask for a rewrite.
            answer_lang = working_language
            if lang == "Auto":
                # "Answer in the same language as the text" makes the MODEL
                # identify the language, and small models get it wrong on dense
                # prose — gemma-3-4b answered an English Hegel passage in French
                # 5/5. Naming the language removes the guess. detect_language
                # returns None when unsure, which keeps the old wording.
                answer_lang = detect_language(text) or "Auto"
            answer_language = _answer_language_instruction(answer_lang)
            sys_msg  = answer_language + base_msg
            # Repeat the answer-language requirement next to the source text.
            # This gives smaller local models a clear, recent instruction and
            # prevents them from defaulting to English or another language.
            user_msg = f"Text:\n{text}\n\n{answer_language}"
        else:
            if _is_translation_mode(mode):
                sys_msg = base_msg
            elif lang == "Auto":
                sys_msg = base_msg + " " + _language_instruction(working_language)
            else:
                # Prepend the language directive so small models prioritise it over
                # the rephrase-mode constraints.  _language_instruction() handles the
                # BHS expansion ("BHS (Bosnian/Croatian/Serbian, ijekavica standard)")
                # centrally so there is only one place to update if the label changes.
                sys_msg = _language_instruction(working_language) + base_msg
            user_msg = f"Rewrite:\n{text}"
        prompt, stop = _build_prompt(fmt, sys_msg, user_msg)
        max_tok = _calc_rephrase_max_tokens(text, sys_msg)
        temp = 0.0 if deterministic else creativity
        # A strong repeat penalty makes small models shorten enumerations by
        # silently dropping the last item.  On gemma-3-4b, Explain It retained
        # all three source judgments in 20/20 runs at 1.1, but only 17/20 at
        # 1.2 and 1.3.  Reading answers are short and do not need the heavier
        # anti-loop setting used for open-ended rewrites.
        repeat_penalty = 1.1 if _is_answer_mode(mode) else 1.3

        _first_chunk = True
        for chunk in _stream_prompt(
            prompt, max_tokens=max_tok, temperature=temp, repeat_penalty=repeat_penalty,
            stop=stop, echo=False, cfg=cfg, on_fallback=on_fallback,
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
        ):
            if _stream_cancel.is_set():
                return
            try:
                piece = chunk["choices"][0].get("text", "")
            except Exception:
                piece = ""
            if piece:
                if _first_chunk:
                    # Auto-detect reasoning models from their opening <think> token.
                    # The flag is checked by _get_thinking_budget() on subsequent calls.
                    _maybe_flag_reasoning_model(piece)
                    _first_chunk = False
                yield piece


def chat_with_text_stream(text: str, user_instruction: str, fmt: str,
                          cfg: dict | None = None, on_fallback=None):
    """
    Apply a free-form user instruction to `text` via the loaded LLM.
    Streams raw text chunks back to the caller.

    Intentionally uses a minimal, unconstrained system prompt so the AI
    just answers whatever the user asks — no rephrase mode, no language
    directives, no inherited settings.

    Output token budget scales with input length and is clamped to whatever
    n_ctx space remains after the prompt, so large documents get a full
    rewrite budget when a large context window is configured — without any
    extra setting needed.

    If no model is loaded, yields `text` unchanged.

    _backend.lock is held for the entire call.  See rephrase_sentence_stream for
    the rationale.  The _stream_cancel event lets the _Toast close_with()
    callback stop an in-progress stream the moment the user dismisses the
    toast, so the lock is released promptly rather than after the model
    finishes generating its full response.
    """
    with _backend.lock:
        if _backend.client is None:
            diagnostics.set_status(backend="None")
            diagnostics.record_throttled(
                "backend", "warning",
                "No AI backend is connected; in-toast edit skipped.",
                cooldown_sec=20.0,
            )
            yield text
            return

        sys_msg = (
            "You are a helpful assistant. "
            "The user will show you a piece of text and ask you to do something with it. "
            "Just do what they ask. Return only the result — no labels, no explanations."
        )
        user_msg = (
            f"Text:\n{text}\n\n"
            f"Instruction: {user_instruction}"
        )
        prompt, stop = _build_prompt(fmt, sys_msg, user_msg)

        # Dynamic output budget: scale with input length (×3), then clamp so
        # prompt + output never exceeds the live context window.
        # prompt_overhead: ~200 tokens for the system/user template scaffolding
        # plus ~1.3 tokens per word for the input text itself.
        # Falls back to 2048 if n_ctx is unreadable (e.g. llama-server with
        # an older /props endpoint that doesn't expose n_ctx).
        input_words = _word_count(text)
        max_tok = max(256, input_words * 3)
        try:
            n_ctx = _backend.client.context_params.n_ctx
            prompt_overhead = 200 + int(input_words * 1.3)
            max_tok = max(64, min(max_tok, n_ctx - prompt_overhead))
        except Exception:
            max_tok = min(max_tok, 2048)

        for chunk in _stream_prompt(
            prompt, max_tokens=max_tok, temperature=0.35, repeat_penalty=1.3,
            stop=stop, echo=False, cfg=cfg, on_fallback=on_fallback,
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
        ):
            if _stream_cancel.is_set():
                return
            try:
                piece = chunk["choices"][0].get("text", "")
            except Exception:
                piece = ""
            if piece:
                yield piece


# ── free-form chat budgets ───────────────────────────────────────────────────
# How long a chat answer should be has nothing to do with how long the question
# was: "can you give an example" routinely deserves several hundred tokens.  So
# the reply gets a flat, generous allowance and only the context window trims it.
_CHAT_REPLY_TOKENS = 2048
# Context space kept free for that reply.  Once a conversation grows past it the
# oldest turns are dropped, rather than squeezing the answer to nothing or
# refusing the whole chat.
_CHAT_REPLY_RESERVE = 512


def _chat_prompt_tokens(history: list) -> int:
    """Estimate a chat prompt's token cost: ~200 for the system/template
    scaffolding plus ~1.5 tokens per word of conversation."""
    words = sum(_word_count(turn["content"]) for turn in history)
    return 200 + int(words * 1.5)


def _fit_chat_history(history: list, n_ctx: int, reserve: int) -> tuple:
    """Drop the oldest turns until `reserve` tokens stay free for the reply.

    Returns (turns_to_send, dropped_count) without mutating the caller's list —
    the chat window keeps showing the full transcript.  The newest turn is never
    dropped; if it alone does not fit, the caller reports the overflow.

    The result always starts on a user turn: _build_chat_prompt folds the system
    message into history[0] only when that turn is a user turn (Gemma, Mistral),
    and llama-server's native Gemma template rejects a conversation that does not
    start with `user`.  A dangling assistant reply at the head would therefore
    either silently lose the system prompt or fail the request outright.
    """
    kept = list(history)
    dropped = 0
    while len(kept) > 1 and _chat_prompt_tokens(kept) + reserve > n_ctx:
        kept.pop(0)
        dropped += 1
        while len(kept) > 1 and kept[0]["role"] != "user":
            kept.pop(0)
            dropped += 1
    return kept, dropped


def free_chat_stream(history: list, fmt: str, cfg: dict | None = None):
    """
    Free-form multi-turn chat with the loaded LLM.

    `history` is a list of {"role": "user"|"assistant", "content": "..."} dicts
    representing the full conversation so far.  The last entry must be a user
    message; this function streams the assistant's next reply.

    Uses _build_chat_prompt() so all supported model families work correctly.

    The reply gets a flat _CHAT_REPLY_TOKENS budget, clamped only by the space
    the conversation genuinely leaves free.  Once a conversation outgrows the
    context window the oldest turns are dropped (logged to diagnostics) so chat
    keeps working instead of hitting a wall.  Falls back to an empty stream if no
    model is loaded.

    _backend.lock is held for the entire streaming call — same rationale as
    rephrase_sentence_stream.  The _stream_cancel event lets the chat window
    stop an in-progress stream the moment the user closes it.
    """
    with _backend.lock:
        if _backend.client is None:
            diagnostics.set_status(backend="None")
            diagnostics.record_throttled(
                "backend", "warning",
                "No AI backend is connected; chat unavailable.",
                cooldown_sec=20.0,
            )
            yield _backend_unavailable_message(cfg, feature="chat")
            return

        sys_msg = (
            "You are a helpful, concise assistant. "
            "Answer the user's questions clearly and directly."
        )

        # Reply budget.  This was previously `input_words * 2` capped at 1024,
        # which tied answer length to question length: a short follow-up such as
        # "can you give an example" earned ~300 tokens and was cut off
        # mid-sentence while thousands of context tokens sat unused.
        max_tok = _CHAT_REPLY_TOKENS
        try:
            n_ctx = int(_backend.client.context_params.n_ctx)
        except Exception:
            n_ctx = 0

        if n_ctx <= 0:
            # n_ctx unreadable (e.g. a llama-server whose /props predates the
            # field): stay conservative rather than overflowing the KV cache.
            max_tok = min(max_tok, 1024)
        else:
            history, dropped = _fit_chat_history(history, n_ctx, _CHAT_REPLY_RESERVE)
            if dropped:
                diagnostics.record_throttled(
                    "chat", "info",
                    f"Chat history trimmed to fit the {n_ctx}-token context window.",
                    f"Dropped the {dropped} oldest turn(s).",
                    cooldown_sec=60.0,
                )
            free = n_ctx - _chat_prompt_tokens(history)
            if free < 64:
                # Nothing left to trim — a single message is too big to fit in
                # the KV cache at all.  Passing it on would raise inside
                # llama.cpp (caught silently downstream) and leave the chat
                # window frozen with no feedback, so say what happened instead.
                yield (
                    "\n\n[That message is too long for the current context window "
                    f"({n_ctx} tokens).  Send a shorter message, or raise "
                    "Context size in Settings → Model.]\n"
                )
                return
            max_tok = max(64, min(max_tok, free))

        if isinstance(_backend.client, CLIClient):
            messages = _chat_history_to_messages(sys_msg, history)
            text, provenance = _backend.client.complete(
                messages, enabled=_cli_enabled_for_request(_backend.client, cfg)
            )
            _record_cli_provenance(provenance)
            if not _stream_cancel.is_set():
                yield f"{text}\n\n{provenance_footer(provenance)}"
            return
        if isinstance(_backend.client, _CloudClient):
            messages = _chat_history_to_messages(sys_msg, history)
            # Chat has no toast to host the failover notice, so queue the
            # switch events here and yield them inline into the transcript
            # ahead of the fallback model's first output.
            pending_switches: list[tuple[str, str]] = []
            for chunk in _cloud_stream_with_fallback(
                messages, max_tokens=max_tok, temperature=0.7, cfg=cfg,
                on_fallback=lambda failed, nxt: pending_switches.append((failed, nxt)),
            ):
                while pending_switches:
                    failed, nxt = pending_switches.pop(0)
                    yield f"\n\n[{failed} hit a rate limit or quota — using {nxt} instead]\n\n"
                try:
                    piece = chunk["choices"][0].get("text", "")
                except Exception:
                    piece = ""
                if piece:
                    yield piece
            return
        if isinstance(_backend.client, _LlamaServerClient):
            messages = _chat_history_to_messages(sys_msg, history)
            stream = _backend.client.stream_messages(
                messages,
                max_tokens=max_tok,
                temperature=0.7,
                repeat_penalty=1.1,
            )
        else:
            prompt, stop = _build_chat_prompt(fmt, sys_msg, history)
            stream = _backend.client(
                prompt,
                max_tokens=max_tok,
                temperature=0.7,
                repeat_penalty=1.1,
                stop=stop,
                echo=False,
                stream=True,
            )
        for chunk in stream:
            if _stream_cancel.is_set():
                return
            try:
                piece = chunk["choices"][0].get("text", "")
            except Exception:
                piece = ""
            if piece:
                yield piece
