# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Behavioral tests for the assistant-mode prompt wiring.

Unlike refactor_smoke_tests.py (zero-install source assertions), these import
the real package, so run them with an interpreter that has the app's
dependencies installed:

    py -3.12 tests/prompt_tests.py

No model is loaded and no network is touched — the LLM entry points are
exercised with the backend faked out, capturing the system prompt each call
would have sent.
"""

import contextlib
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@contextlib.contextmanager
def _fake_backend(captured: list, events: list | None = None):
    """Patch vedium.llm so generation calls capture (fmt, system, user)
    instead of running a model. Diagnostics land in `events` as
    (component, severity, message) tuples. Restores everything on exit."""
    import vedium.llm as llm

    # The llm layer spans sibling modules; diagnostics must be stubbed in each
    # module that calls it, since every module binds its own name at import.
    diag_hosts = (llm, llm.llm_backends, llm.llm_postprocess)
    orig = (llm._backend.client, llm._build_prompt, llm._complete_prompt,
            llm._stream_prompt, llm._get_thinking_budget,
            tuple(m.diagnostics for m in diag_hosts))

    def fake_build(fmt, system, user):
        captured.append((fmt, system, user))
        return "PROMPT", []

    def fake_stream(*a, **k):
        yield {"choices": [{"text": "ok"}]}

    events = events if events is not None else []
    llm._backend.client = object()  # non-None: "a backend is connected"
    llm._build_prompt = fake_build
    llm._complete_prompt = lambda *a, **k: {"choices": [{"text": "ok"}]}
    llm._stream_prompt = fake_stream
    llm._get_thinking_budget = lambda: 0
    fake_diag = types.SimpleNamespace(
        record=lambda comp, sev, msg, **k: events.append((comp, sev, msg)),
        record_throttled=lambda comp, sev, msg, **k: events.append((comp, sev, msg)),
        set_status=lambda **k: None,
    )
    for m in diag_hosts:
        m.diagnostics = fake_diag
    try:
        yield llm
    finally:
        (llm._backend.client, llm._build_prompt, llm._complete_prompt,
         llm._stream_prompt, llm._get_thinking_budget, orig_diags) = orig
        for m, d in zip(diag_hosts, orig_diags):
            m.diagnostics = d


def test_diacritics_spell_check_uses_live_mode_prompt() -> None:
    """The auto-correct diacritics path and the 'Diacritics Only' rephrase mode
    must share one prompt: the live mode prompt, including user overrides."""
    import vedium.constants as c
    from vedium.config import _merge_custom_modes

    captured: list = []
    fmt = "Plain  (no template)"
    with _fake_backend(captured) as llm:
        # Default state: the factory mode prompt drives the spell-check path.
        llm.spell_check_sentence("neces valjda", fmt, diacritics_only=True)
        assert captured[-1][1] == c._FACTORY_PROMPTS["Diacritics Only"]

        # A user override on the mode must drive the same path.
        marker = "OVERRIDDEN DIACRITICS PROMPT (prompt_tests marker)"
        _merge_custom_modes({"custom_rephrase_modes": {},
                             "builtin_prompt_overrides": {"Diacritics Only": marker}})
        try:
            llm.spell_check_sentence("neces valjda", fmt, diacritics_only=True)
            assert captured[-1][1] == marker
        finally:
            _merge_custom_modes({"custom_rephrase_modes": {},
                                 "builtin_prompt_overrides": {}})

        # Reset restores the factory prompt.
        llm.spell_check_sentence("neces valjda", fmt, diacritics_only=True)
        assert captured[-1][1] == c._FACTORY_PROMPTS["Diacritics Only"]


def test_unknown_rephrase_mode_falls_back_visibly() -> None:
    """A stale/unknown mode name must fall back to 'Polish My Writing' and
    record a diagnostics warning — not silently run diacritics restoration."""
    captured: list = []
    events: list = []
    fmt = "Plain  (no template)"
    with _fake_backend(captured, events) as llm:
        llm._stream_cancel.clear()
        list(llm.rephrase_sentence_stream("some text to rewrite", fmt,
                                          mode="Deleted Custom Mode"))
        sys_msg = captured[-1][1]
        assert "Polish the following writing" in sys_msg
        assert "diacritics" not in sys_msg.lower()
        assert any(sev == "warning" and "Unknown assistant mode" in msg
                   for _comp, sev, msg in events)

        # Known modes are untouched and record nothing.
        events.clear()
        list(llm.rephrase_sentence_stream("some text to rewrite", fmt,
                                          mode="More Casual"))
        assert "relaxed, conversational tone" in captured[-1][1]
        assert not events


def test_light_writing_modes_have_guardrails() -> None:
    """'More Casual', 'Simpler Language', and 'More Formal (Academic)' must
    carry the same don't-invent / preserve-details fences as the other writing
    modes — small models pad or invent without them."""
    from vedium.constants import _FACTORY_PROMPTS

    required = {
        "More Casual": [
            "Preserve every claim",
            "Do NOT add facts, opinions, jokes",
        ],
        "Simpler Language": [
            "Simplify the wording, not the content",
            "Do NOT add facts, examples, or explanations",
        ],
        "More Formal (Academic)": [
            "Preserve every argument",
            "Do NOT add claims, citations",
            "Do NOT pad the text with filler",
        ],
    }
    for mode, phrases in required.items():
        prompt = _FACTORY_PROMPTS[mode]
        for phrase in phrases:
            assert phrase in prompt, f"{mode}: missing fence {phrase!r}"
        assert "Return ONLY the rewritten text" in prompt, f"{mode}: missing return-only clause"


def test_explain_it_is_source_bounded() -> None:
    """The general explanation mode must clarify the selected passage without
    inviting a small model to supply the argument or context the author has not
    given yet."""
    from vedium.constants import _FACTORY_PROMPTS

    prompt = _FACTORY_PROMPTS["Explain It"]
    for phrase in (
        "conservative reading-aid paraphraser",
        "without interpreting, expanding, or commenting",
        "Do not omit or merge",
        "Copy every distinct evaluative term",
        "'superfluous,' 'inappropriate,' and 'counterproductive,'",
        "'previous or contemporary'",
        "Add no background, definitions, examples, reasons",
        "Keep vague or abstract wording equally general",
        "do not describe the nature of X",
        "Keep incomplete reasoning incomplete",
        "'seems' must remain 'seems'",
        "Do not quote, cite, label, use bullets",
    ):
        assert phrase in prompt, f"Explain It: missing fidelity fence {phrase!r}"
    assert "underlying point or stakes" not in prompt
    assert "anchor unfamiliar concepts in concrete, real-world terms" not in prompt


def test_answer_modes_use_a_gentler_repeat_penalty() -> None:
    """Short reading answers must not lose the end of a source enumeration;
    writing rewrites retain the stronger anti-loop setting."""
    captured: list = []
    generation_calls: list = []

    with _fake_backend(captured) as llm:
        def capture_stream(*args, **kwargs):
            generation_calls.append(kwargs)
            yield {"choices": [{"text": "ok"}]}

        llm._stream_prompt = capture_stream
        llm._stream_cancel.clear()
        list(llm.rephrase_sentence_stream(
            "A difficult source passage.", "Plain  (no template)",
            mode="Explain It", working_language="English",
        ))
        list(llm.rephrase_sentence_stream(
            "A draft to improve.", "Plain  (no template)",
            mode="Polish My Writing", working_language="English",
        ))

    assert generation_calls[0]["repeat_penalty"] == 1.1
    assert generation_calls[1]["repeat_penalty"] == 1.3


def test_extract_writing_style_is_a_selected_text_answer_mode() -> None:
    """Style extraction analyses a selection and returns the name/profile contract."""
    import vedium.constants as c

    assert "Extract Writing Style" in c._BUILTIN_MODES
    assert c._is_answer_mode("Extract Writing Style")

    captured: list = []
    with _fake_backend(captured) as llm:
        llm._stream_cancel.clear()
        list(llm.rephrase_sentence_stream(
            "A representative passage with deliberate rhythm.",
            "Plain  (no template)",
            mode="Extract Writing Style",
        ))
    _fmt, system, user = captured[-1]
    assert "STYLE NAME:" in system
    assert "STYLE PROFILE:" in system
    assert "not what it is about" in system
    assert user.startswith("Text:\n")
    assert not user.startswith("Rewrite:\n")


def test_generated_style_can_be_edited_and_saved_as_a_writing_mode() -> None:
    """The structured AI result becomes a guarded custom mode selected for F9."""
    import vedium.constants as c
    from vedium.config import (
        _add_generated_style_mode,
        _merge_custom_modes,
        _parse_generated_style,
    )

    generated = (
        "**STYLE NAME:** Measured Reflective Prose\n"
        "**STYLE PROFILE:**\n"
        "- Uses restrained, precise diction.\n"
        "- Alternates compact claims with longer reflective sentences."
    )
    name, profile = _parse_generated_style(generated)
    assert name == "Measured Reflective Prose"
    assert profile.startswith("- Uses restrained")

    cfg = {"custom_rephrase_modes": {}, "builtin_prompt_overrides": {}}
    _add_generated_style_mode(cfg, name, profile)
    try:
        assert cfg["rephrase_writing_mode"] == name
        assert name in cfg["custom_rephrase_modes"]
        prompt = cfg["custom_rephrase_modes"][name]
        assert profile in prompt
        assert "Preserve the target text's meaning" in prompt
        assert "Do NOT copy distinctive phrases" in prompt
        assert "Return ONLY the rewritten text" in prompt
        assert not c._is_answer_mode(name)

        try:
            _add_generated_style_mode(cfg, name, profile)
            raise AssertionError("duplicate style name should fail")
        except ValueError as exc:
            assert "already exists" in str(exc)
    finally:
        _merge_custom_modes({"custom_rephrase_modes": {},
                             "builtin_prompt_overrides": {}})


def test_answer_modes_honour_the_working_language_setting() -> None:
    """A named Working Language must govern reading-mode answers.

    Regression: _answer_language_instruction used to ignore its argument
    entirely, so the setting silently did nothing for every answer mode. With
    Auto the model has to identify the language itself and gets it wrong on
    dense Latinate prose — gemma-3-4b answered a Hegel passage in French 5 times
    out of 5 — so the named-language escape hatch has to actually work.
    """
    from vedium.llm import _answer_language_instruction

    auto = _answer_language_instruction("Auto")
    assert "same language as the text" in auto
    assert _answer_language_instruction("") == auto      # empty behaves as Auto

    named = _answer_language_instruction("English")
    assert "English" in named
    assert "same language as the text" not in named, "named language must not defer to the text"

    bhs = _answer_language_instruction("BHS")
    assert "Bosnian/Croatian/Serbian" in bhs, "BHS must expand to the full label"

    # End to end: the directive has to reach the prompt the backend is handed,
    # both in the system message and next to the passage itself.
    captured: list = []
    with _fake_backend(captured) as llm:
        llm._stream_cancel.clear()
        list(llm.rephrase_sentence_stream(
            "Eine dichte Passage.", "Plain  (no template)",
            mode="What's This About?", working_language="German",
        ))
    _fmt, system, user = captured[-1]
    assert "German" in system, system[:200]
    assert "German" in user, user[:200]
    assert "same language as the text" not in system

    # Auto keeps the previous behaviour for readers of non-English text.
    captured.clear()
    with _fake_backend(captured) as llm:
        llm._stream_cancel.clear()
        list(llm.rephrase_sentence_stream(
            "Une passage dense.", "Plain  (no template)",
            mode="What's This About?", working_language="Auto",
        ))
    _fmt, system, _user = captured[-1]
    assert "same language as the text" in system


def test_auto_answers_name_the_detected_language() -> None:
    """On Auto, the app identifies the language so the model doesn't have to.

    Asking the model to "answer in the same language as the text" is what let
    gemma-3-4b answer an English Hegel passage in French 5/5 (other models drifted
    to Spanish and German). Detection must be fail-safe: when it isn't confident
    the old wording has to come back, since naming the WRONG language would be
    worse than leaving the model to guess.
    """
    import vedium.constants as c
    from vedium.language import detect_language

    english = (
        "Therefore, from the representation of inversion, which constitutes the essence "
        "of one aspect of the supersensible world, the sensuous representation of the "
        "attachment of the differences in diverse elements of stable existence must be "
        "detached, and this absolute concept of difference is to be grasped as inner difference."
    )
    french = (
        "La conscience de soi ne parvient à sa satisfaction que dans une autre conscience "
        "de soi, et ce mouvement de reconnaissance réciproque constitue le fondement de "
        "toute la vie éthique de la communauté."
    )
    assert detect_language(english) == "English"
    assert detect_language(french) == "French"
    # Too little text to be sure -> None, never a guess.
    for weak in (
        "", "ok", "Hello there", "getUserByIndx", "α", "я", "і", "это", "це",
        "och", "af", "hva",
    ):
        assert detect_language(weak) is None, weak

    # Closely related Scandinavian languages need distinctive evidence rather
    # than being rejected because their common function words overlap.
    scandinavian = {
        "Swedish": (
            "Regeringen har publicerat rapporten, och ledamöterna i utskottet diskuterar "
            "vad det betyder för samhället. Det är inte det slutliga beslutet, men det "
            "blir en grund för en fortsatt debatt som de andra också kan delta i."
        ),
        "Danish": (
            "Regeringen har offentliggjort rapporten, og medlemmerne af udvalget taler "
            "om, hvad det betyder for samfundet. Det er ikke den endelige beslutning, "
            "men den bliver et grundlag for en videre debat."
        ),
        "Norwegian": (
            "Regjeringen har offentliggjort rapporten, og medlemmene i utvalget snakker "
            "om hva det betyr for samfunnet. Det er ikke den endelige beslutningen, men "
            "den blir et grunnlag for en videre debatt."
        ),
    }
    for expected, passage in scandinavian.items():
        assert detect_language(passage) == expected, expected
    assert detect_language(
        "Det er en rapport som de har skrevet, og den er for alle som ikke har lest den."
    ) is None

    # Anything the detector can return must be selectable in Settings, or the
    # prompt would name a language the rest of the app does not know.
    for sample in (english, french):
        assert detect_language(sample) in c.WORKING_LANGUAGE_OPTIONS

    captured: list = []
    with _fake_backend(captured) as llm:
        llm._stream_cancel.clear()
        list(llm.rephrase_sentence_stream(
            english, "Plain  (no template)", mode="What's This About?",
            working_language="Auto",
        ))
    _fmt, system, _user = captured[-1]
    assert "English" in system, system[:200]
    assert "same language as the text" not in system

    # An unidentifiable selection falls back to the original wording.
    captured.clear()
    with _fake_backend(captured) as llm:
        llm._stream_cancel.clear()
        list(llm.rephrase_sentence_stream(
            "getUserByIndx", "Plain  (no template)", mode="Define This",
            working_language="Auto",
        ))
    _fmt, system, _user = captured[-1]
    assert "same language as the text" in system, system[:200]

    # Rewrite modes must NOT be touched by detection: naming a language there
    # turns spell-check and rephrase into translation.
    captured.clear()
    with _fake_backend(captured) as llm:
        llm._stream_cancel.clear()
        list(llm.rephrase_sentence_stream(
            english, "Plain  (no template)", mode="Polish My Writing",
            working_language="Auto",
        ))
    _fmt, system, _user = captured[-1]
    assert "same language as the input text" in system
    assert "regardless of what language" not in system


def test_spell_check_corrects_rather_than_translating_its_own_language() -> None:
    """Text already in the working language must be corrected, not translated.

    "Translate German -> German" makes small models rewrite instead of correct
    (gemma-3-4b turned "ein sehr schoner Tag" into "ein wunderschoener Tag"),
    which is destructive on the silent auto-replace path. This used to be
    special-cased for BHS alone via a set literally named _SLAVIC_LANGS.
    """
    import vedium.constants as c
    from vedium.llm import _correction_sys_msg
    from vedium.language import is_same_language

    assert c._NATIVE_TYPING_LANGS == {"BHS"}

    german = ("Das Bewusstsein ist für sich selbst der Begriff, und die Bewegung des "
              "Begriffs ist das, was in der Erfahrung sich darstellt.")
    english = "The consciousness is for itself the concept and the movement of the concept."

    assert is_same_language(german, "German") is True      # correct, don't translate
    assert is_same_language(english, "German") is False    # translate, as configured
    assert is_same_language(english, "Auto") is True
    assert is_same_language(english, "BHS") is True        # always native
    # Detection failure must keep the old translate-first behaviour, or
    # translate-as-you-type silently stops working on short sentences.
    assert is_same_language("kratko", "German") is False
    # Spell-check receives short sentences, not the paragraphs used above. A
    # small amount of target-specific evidence must still select correction.
    for text, lang in (
        ("ein sehr schoner Tag", "German"),
        ("This sentnce has an eror", "English"),
        ("To jest piekny dzien", "Polish"),
        ("To je krasny den", "Czech"),
    ):
        assert is_same_language(text, lang) is True, (text, lang)
    assert is_same_language("The weather is beautiful today", "German") is False

    captured: list = []
    with _fake_backend(captured) as llm:
        llm.spell_check_sentence(
            "ein sehr schoner Tag", "Plain  (no template)", working_language="German",
        )
    _fmt, system, user = captured[-1]
    assert "corrector for German" in system
    assert "professional translator" not in system
    assert "Bosnian, Croatian, or Serbian" not in system
    assert user.startswith("Fix only clear spelling")

    captured.clear()
    with _fake_backend(captured) as llm:
        llm.spell_check_sentence(
            "cesce idem u skolu", "Plain  (no template)", working_language="BHS",
        )
    assert "češće idem u skolu" in captured[-1][2]

    # The BHS diacritic block is BHS-only; every other language shares one body.
    bhs = _correction_sys_msg("BHS")
    assert "ONLY diacritical characters" in bhs
    assert "Bosnian/Croatian/Serbian" in bhs
    for lang in ("German", "Polish", "Czech"):
        msg = _correction_sys_msg(lang)
        assert f"corrector for {lang}." in msg
        assert "diacritical characters" not in msg, f"{lang} must not inherit BHS rules"
        assert "Make the smallest possible change" in msg

    auto = _correction_sys_msg("Auto")
    assert "same language as the input text" in auto, "Auto must pin the output language"

    from vedium.llm_postprocess import _restore_common_bhs
    assert _restore_common_bhs("cesce") == "češće"
    assert _restore_common_bhs("Ćešce") == "Češće"
    assert _restore_common_bhs("najcesce") == "najčešće"


def test_only_wrapping_quotes_are_stripped_from_answers() -> None:
    """A quotation the answer opens with must survive into the saved journal."""
    from vedium.llm_postprocess import _clean_llm_output, _strip_wrapping_quotes

    keep = [
        '"Liminal" describes something at a boundary.',   # opens with a quotation
        '"Hello," he said, "goodbye."',                   # alternating speech
        "'tis a fine day",                                # leading apostrophe
        '"',                                              # lone quote
    ]
    for text in keep:
        assert _strip_wrapping_quotes(text) == text, text

    # Quotes the model wrapped around the whole answer still come off, however
    # many layers deep.
    assert _strip_wrapping_quotes('"The rewritten sentence."') == "The rewritten sentence."
    assert _strip_wrapping_quotes("'The rewritten sentence.'") == "The rewritten sentence."
    assert _strip_wrapping_quotes('"\'The rewritten sentence.\'"') == "The rewritten sentence."
    assert _strip_wrapping_quotes('""') == ""

    # End to end: the real Define This result for "liminal" used to reach the
    # vocabulary journal as 'Liminal" describes …', missing its opening quote.
    assert _clean_llm_output('"Liminal" describes something at a boundary.') == (
        '"Liminal" describes something at a boundary.'
    )
    assert _clean_llm_output('"A rewritten sentence."') == "A rewritten sentence."


class _FakeResp:
    """Stands in for urllib's response: .read() for JSON, iteration for SSE."""

    def __init__(self, body: bytes = b"", lines: list | None = None):
        self._body = body
        self._lines = lines or []

    def read(self):
        return self._body

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass


@contextlib.contextmanager
def _cloud_client_with_responses(make_response):
    """A _CloudClient whose _request is faked; records payloads in .calls.
    Saves/restores the module-level reasoning flag and diagnostics."""
    import vedium.llm as llm

    client = llm._CloudClient("https://example.invalid", "key", "thinking-model")
    client.calls = []

    def fake_request(payload):
        client.calls.append(payload)
        return make_response(len(client.calls))

    client._request = fake_request
    diag_hosts = (llm, llm.llm_backends, llm.llm_postprocess)
    orig_flag = llm._backend.reasoning_detected
    orig_diags = tuple(m.diagnostics for m in diag_hosts)
    llm._backend.reasoning_detected = False
    fake_diag = types.SimpleNamespace(
        record=lambda *a, **k: None,
        record_throttled=lambda *a, **k: None,
        set_status=lambda **k: None,
    )
    for m in diag_hosts:
        m.diagnostics = fake_diag
    try:
        yield client, llm
    finally:
        llm._backend.reasoning_detected = orig_flag
        for m, d in zip(diag_hosts, orig_diags):
            m.diagnostics = d


def test_cloud_hidden_reasoning_retry_nonstream() -> None:
    """An empty completion with finish_reason=length (server-side thinking model
    burned the budget) must retry once with the thinking budget added."""
    import json

    def responses(n):
        if n == 1:
            body = {"choices": [{"message": {"content": None}, "finish_reason": "length"}]}
        else:
            body = {"choices": [{"message": {"content": "fixed"}, "finish_reason": "stop"}]}
        return _FakeResp(json.dumps(body).encode())

    with _cloud_client_with_responses(responses) as (client, llm):
        out = client._call_messages([{"role": "user", "content": "hi"}], max_tokens=50)
        assert out == {"choices": [{"text": "fixed"}]}
        assert len(client.calls) == 2
        assert client.calls[1]["max_tokens"] == 50 + llm._THINKING_BUDGET_AUTO
        assert llm._backend.reasoning_detected is True


def test_cloud_hidden_reasoning_retry_stream() -> None:
    """Same signature on the streaming path: no visible text + finish_reason
    length → one retry with the bigger budget, whose chunks stream through."""

    def responses(n):
        if n == 1:
            return _FakeResp(lines=[
                b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
                b"data: [DONE]",
            ])
        return _FakeResp(lines=[
            b'data: {"choices":[{"delta":{"content":"fix"},"finish_reason":null}]}',
            b'data: {"choices":[{"delta":{"content":"ed"},"finish_reason":"stop"}]}',
            b"data: [DONE]",
        ])

    with _cloud_client_with_responses(responses) as (client, llm):
        chunks = list(client._call_messages([{"role": "user", "content": "hi"}],
                                            max_tokens=50, stream=True))
        assert [c["choices"][0]["text"] for c in chunks] == ["fix", "ed"]
        assert len(client.calls) == 2
        assert client.calls[1]["max_tokens"] == 50 + llm._THINKING_BUDGET_AUTO
        assert llm._backend.reasoning_detected is True


def test_cloud_hidden_reasoning_retries_only_once() -> None:
    """If the retry is also empty, give up — never loop."""

    def responses(n):
        return _FakeResp(lines=[
            b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
            b"data: [DONE]",
        ])

    with _cloud_client_with_responses(responses) as (client, _llm):
        chunks = list(client._call_messages([{"role": "user", "content": "hi"}],
                                            max_tokens=50, stream=True))
        assert chunks == []
        assert len(client.calls) == 2


def test_cloud_inline_thought_truncation_retries() -> None:
    """Inline thinkers (gemma-4-style <thought> in content): a length-capped
    response containing reasoning markers is untrustworthy — cut mid-thought
    (strips to nothing) or mid-answer (silently loses trailing words) — and
    must retry with the thinking budget. Plain truncated text without markers
    must NOT retry."""
    import json

    for first_content in ("<thought>The user wants me to",              # cut mid-thought
                          "<thought>ok</thought>Hoću da vidim hoće li"):  # cut mid-answer
        def responses(n, _first=first_content):
            if n == 1:
                body = {"choices": [{"message": {"content": _first},
                                     "finish_reason": "length"}]}
            else:
                body = {"choices": [{"message": {"content": "<thought>ok</thought>full answer"},
                                     "finish_reason": "stop"}]}
            return _FakeResp(json.dumps(body).encode())

        with _cloud_client_with_responses(responses) as (client, llm):
            out = client._call_messages([{"role": "user", "content": "hi"}], max_tokens=50)
            assert out["choices"][0]["text"].endswith("full answer")
            assert len(client.calls) == 2
            assert llm._backend.reasoning_detected is True

    # Negative: length-capped but no reasoning markers → plain model with a
    # small budget; keep the old behavior (no retry, text passed through).
    def responses(n):
        body = {"choices": [{"message": {"content": "just a plain truncated answ"},
                             "finish_reason": "length"}]}
        return _FakeResp(json.dumps(body).encode())

    with _cloud_client_with_responses(responses) as (client, llm):
        out = client._call_messages([{"role": "user", "content": "hi"}], max_tokens=50)
        assert out == {"choices": [{"text": "just a plain truncated answ"}]}
        assert len(client.calls) == 1
        assert llm._backend.reasoning_detected is False


def test_cloud_smoke_test_flags_but_never_retries() -> None:
    """retry_on_empty=False (the connect smoke test) must still flag a hidden-
    reasoning model but make exactly one request — empty content is a pass."""
    import json

    def responses(n):
        body = {"choices": [{"message": {"content": None}, "finish_reason": "length"}]}
        return _FakeResp(json.dumps(body).encode())

    with _cloud_client_with_responses(responses) as (client, llm):
        out = client._call_messages([{"role": "user", "content": "hi"}],
                                    max_tokens=1, retry_on_empty=False)
        assert out == {"choices": [{"text": ""}]}
        assert len(client.calls) == 1
        assert llm._backend.reasoning_detected is True


@contextlib.contextmanager
def _cloud_failover_env(dispatch):
    """Cloud backend with two favorites: 'flaky-model' (current) and
    'stable-model'. `dispatch(model, payload)` fakes each network call.
    Records the models called, in order, in the yielded list."""
    import vedium.llm as llm

    base = "https://api.example.invalid/v1"
    calls: list = []

    def fake_request(self, payload):
        calls.append(payload["model"])
        return dispatch(payload["model"], payload)

    cfg = {
        "cloud_provider": "TestProv",
        "cloud_api_keys": {"TestProv": "key"},
        "cloud_favorites": [
            {"provider": "TestProv", "base_url": base, "model": "flaky-model"},
            {"provider": "TestProv", "base_url": base, "model": "stable-model"},
        ],
    }
    diag_hosts = (llm, llm.llm_backends, llm.llm_postprocess)
    saved = (llm._backend.client, llm._backend.path, llm._backend.label,
             tuple(m.diagnostics for m in diag_hosts),
             llm._CloudClient._request, llm._backend.reasoning_detected)
    llm._backend.client = llm._CloudClient(base, "key", "flaky-model")
    llm._CloudClient._request = fake_request
    fake_diag = types.SimpleNamespace(
        record=lambda *a, **k: None,
        record_throttled=lambda *a, **k: None,
        set_status=lambda **k: None,
    )
    for m in diag_hosts:
        m.diagnostics = fake_diag
    try:
        yield llm, cfg, calls
    finally:
        (llm._backend.client, llm._backend.path, llm._backend.label, orig_diags,
         llm._CloudClient._request, llm._backend.reasoning_detected) = saved
        for m, d in zip(diag_hosts, orig_diags):
            m.diagnostics = d


def _http_error(code: int, body: dict):
    import io, json, urllib.error
    return urllib.error.HTTPError(
        "https://api.example.invalid/v1/chat/completions", code, "err",
        None, io.BytesIO(json.dumps(body).encode()))


def test_cloud_failover_on_server_error_nonstream() -> None:
    """A 503/500 from the provider must fail over to the next favorite,
    activate it, and notify via on_fallback — same as a quota error."""
    import json

    def dispatch(model, payload):
        if model == "flaky-model":
            raise _http_error(503, {"error": {"message": "high demand", "status": "UNAVAILABLE"}})
        body = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]}
        return _FakeResp(json.dumps(body).encode())

    with _cloud_failover_env(dispatch) as (llm, cfg, calls):
        switches = []
        out = llm._cloud_complete_with_fallback(
            [{"role": "user", "content": "hi"}], max_tokens=50, temperature=0.0,
            cfg=cfg, on_fallback=lambda a, b: switches.append((a, b)))
        assert out == {"choices": [{"text": "hello"}]}
        assert calls == ["flaky-model", "stable-model"]
        assert switches == [("flaky-model", "stable-model")]
        assert cfg["cloud_model"] == "stable-model"


def test_cloud_failover_on_server_error_stream() -> None:
    """Same for the streaming path, before any output has been produced."""

    def dispatch(model, payload):
        if model == "flaky-model":
            raise _http_error(500, {"error": {"message": "Internal error encountered.", "status": "INTERNAL"}})
        return _FakeResp(lines=[
            b'data: {"choices":[{"delta":{"content":"he"},"finish_reason":null}]}',
            b'data: {"choices":[{"delta":{"content":"llo"},"finish_reason":"stop"}]}',
            b"data: [DONE]",
        ])

    with _cloud_failover_env(dispatch) as (llm, cfg, calls):
        chunks = list(llm._cloud_stream_with_fallback(
            [{"role": "user", "content": "hi"}], max_tokens=50, temperature=0.0, cfg=cfg))
        assert [c["choices"][0]["text"] for c in chunks] == ["he", "llo"]
        assert calls == ["flaky-model", "stable-model"]
        assert cfg["cloud_model"] == "stable-model"


def test_cloud_failover_skipped_for_permanent_errors() -> None:
    """A rejected API key would fail on every model — surface it immediately,
    without burning requests on the other favorites."""

    def dispatch(model, payload):
        raise _http_error(401, {"error": {"message": "API key not valid."}})

    with _cloud_failover_env(dispatch) as (llm, cfg, calls):
        try:
            llm._cloud_complete_with_fallback(
                [{"role": "user", "content": "hi"}], max_tokens=50,
                temperature=0.0, cfg=cfg)
            raise AssertionError("expected RuntimeError")
        except RuntimeError as exc:
            assert "API key" in str(exc)
        assert calls == ["flaky-model"]


def main() -> int:
    tests = [
        test_diacritics_spell_check_uses_live_mode_prompt,
        test_unknown_rephrase_mode_falls_back_visibly,
        test_light_writing_modes_have_guardrails,
        test_explain_it_is_source_bounded,
        test_answer_modes_use_a_gentler_repeat_penalty,
        test_extract_writing_style_is_a_selected_text_answer_mode,
        test_generated_style_can_be_edited_and_saved_as_a_writing_mode,
        test_answer_modes_honour_the_working_language_setting,
        test_auto_answers_name_the_detected_language,
        test_spell_check_corrects_rather_than_translating_its_own_language,
        test_only_wrapping_quotes_are_stripped_from_answers,
        test_cloud_hidden_reasoning_retry_nonstream,
        test_cloud_hidden_reasoning_retry_stream,
        test_cloud_hidden_reasoning_retries_only_once,
        test_cloud_inline_thought_truncation_retries,
        test_cloud_smoke_test_flags_but_never_retries,
        test_cloud_failover_on_server_error_nonstream,
        test_cloud_failover_on_server_error_stream,
        test_cloud_failover_skipped_for_permanent_errors,
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
