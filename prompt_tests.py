"""Behavioral tests for the assistant-mode prompt wiring.

Unlike refactor_smoke_tests.py (zero-install source assertions), these import
the real package, so run them with an interpreter that has the app's
dependencies installed:

    py -3.12 prompt_tests.py

No model is loaded and no network is touched — the LLM entry points are
exercised with the backend faked out, capturing the system prompt each call
would have sent.
"""

import contextlib
import types


@contextlib.contextmanager
def _fake_backend(captured: list, events: list | None = None):
    """Patch spell_daemon.llm so generation calls capture (fmt, system, user)
    instead of running a model. Diagnostics land in `events` as
    (component, severity, message) tuples. Restores everything on exit."""
    import spell_daemon.llm as llm

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
    import spell_daemon.constants as c
    from spell_daemon.config import _merge_custom_modes

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
    from spell_daemon.constants import _FACTORY_PROMPTS

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
    import spell_daemon.llm as llm

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
    import spell_daemon.llm as llm

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
