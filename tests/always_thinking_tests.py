# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Regression checks for always-thinking (LFM2-style) model handling.

LFM2's chat template exposes a `preserve_thinking` variable that only controls
whether PRIOR thoughts stay in the context; it has no branch that stops the
current turn from thinking. Standard `enable_thinking=false` kwargs are ignored,
so the pipeline must (a) keep the thinking budget reserved even when
`server_disable_thinking` is on, and (b) fall back to `reasoning_content` when
the model runs out of budget still inside its <think> block.
"""

import io
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vedium.llm_backends import _LlamaServerClient
from vedium.llm_postprocess import _THINKING_BUDGET_AUTO, _get_thinking_budget
from vedium.llm_state import _backend


class _FakeUrlopenResponse:
    def __init__(self, body: bytes = b"", lines=None):
        self._body = body
        self._lines = list(lines or [])

    def read(self):
        return self._body

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass

    # For `with urllib.request.urlopen(...) as response:` style calls.
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _fresh_client() -> _LlamaServerClient:
    """Instantiate without triggering the real HTTP _probe()."""
    client = object.__new__(_LlamaServerClient)
    client.base_url = "http://example.invalid"
    client.supports_chat_template_kwargs = True
    client._n_ctx = 8192
    client._think_disable_suffix = ""
    client.always_thinking = False
    return client


def test_probe_detects_lfm2_preserve_thinking_template(monkeypatch=None) -> None:
    """A `<think>` template that uses `preserve_thinking` (LFM2) flips both
    the client flag and the shared backend flag; nothing else touches config."""
    lfm2_props = json.dumps({
        "n_ctx": 16384,
        "chat_template": (
            "{{- bos_token -}}"
            "{%- set preserve_thinking = preserve_thinking | default(false) -%}"
            "{%- if message.thinking is defined %}<think>{{ message.thinking }}</think>{%- endif -%}"
        ),
        "default_generation_settings": {},
    }).encode()

    saved = (_backend.reasoning_detected, _backend.always_thinking)
    try:
        import vedium.llm_backends as lb
        original_urlopen = lb.urllib.request.urlopen
        lb.urllib.request.urlopen = lambda *a, **k: _FakeUrlopenResponse(body=lfm2_props)
        try:
            client = _fresh_client()
            client._probe()
        finally:
            lb.urllib.request.urlopen = original_urlopen

        assert client.always_thinking is True
        assert _backend.always_thinking is True
        assert _backend.reasoning_detected is True
        assert client._n_ctx == 16384
    finally:
        _backend.reasoning_detected, _backend.always_thinking = saved


def test_probe_leaves_qwen_style_thinker_alone() -> None:
    """A Qwen-style `enable_thinking` template is still detected as reasoning
    but NOT tagged always_thinking — its template does have a disable branch."""
    qwen_props = json.dumps({
        "n_ctx": 32768,
        "chat_template": (
            "{%- if enable_thinking -%}<think>{%- endif -%}"
        ),
    }).encode()

    saved = (_backend.reasoning_detected, _backend.always_thinking)
    try:
        import vedium.llm_backends as lb
        original_urlopen = lb.urllib.request.urlopen
        lb.urllib.request.urlopen = lambda *a, **k: _FakeUrlopenResponse(body=qwen_props)
        try:
            client = _fresh_client()
            client._probe()
        finally:
            lb.urllib.request.urlopen = original_urlopen

        assert client.always_thinking is False
        assert _backend.always_thinking is False
        assert _backend.reasoning_detected is True
    finally:
        _backend.reasoning_detected, _backend.always_thinking = saved


def test_complete_messages_falls_back_to_reasoning_content_when_always_thinking() -> None:
    """LFM2 that runs out of budget inside <think>: content is empty but
    reasoning_content has the trace. Vedium must surface the trace instead
    of an empty card."""
    body = json.dumps({
        "choices": [{
            "message": {
                "content": "",
                "reasoning_content": "Working through the passage now...",
            },
            "finish_reason": "length",
        }]
    }).encode()

    import vedium.llm_backends as lb
    original_urlopen = lb.urllib.request.urlopen
    lb.urllib.request.urlopen = lambda *a, **k: _FakeUrlopenResponse(body=body)
    try:
        client = _fresh_client()
        client.always_thinking = True
        out = client.complete_messages(
            [{"role": "user", "content": "hi"}],
            max_tokens=64, temperature=0.05,
        )
        assert out["choices"][0]["text"] == "Working through the passage now..."
        assert out["choices"][0]["finish_reason"] == "length"
    finally:
        lb.urllib.request.urlopen = original_urlopen


def test_complete_messages_does_not_fall_back_when_not_always_thinking() -> None:
    """Non-LFM2 models keep the historical behaviour: empty content stays
    empty. This guards against silently changing behaviour for gemma / qwen."""
    body = json.dumps({
        "choices": [{
            "message": {
                "content": "",
                "reasoning_content": "some inner thoughts",
            },
            "finish_reason": "length",
        }]
    }).encode()

    import vedium.llm_backends as lb
    original_urlopen = lb.urllib.request.urlopen
    lb.urllib.request.urlopen = lambda *a, **k: _FakeUrlopenResponse(body=body)
    try:
        client = _fresh_client()
        client.always_thinking = False  # explicit default
        out = client.complete_messages(
            [{"role": "user", "content": "hi"}],
            max_tokens=64, temperature=0.05,
        )
        assert out["choices"][0]["text"] == ""
    finally:
        lb.urllib.request.urlopen = original_urlopen


def test_stream_emits_reasoning_when_content_never_arrives() -> None:
    """Streaming Explain It on LFM2: the deltas carry only reasoning_content
    until the token budget runs out. Vedium must emit ONE final chunk with the
    buffered reasoning so the reader sees the model's actual output."""
    lines = [
        b'data: {"choices":[{"delta":{"reasoning_content":"First "},"finish_reason":null}]}',
        b'data: {"choices":[{"delta":{"reasoning_content":"pass."},"finish_reason":null}]}',
        b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
        b"data: [DONE]",
    ]

    import vedium.llm_backends as lb
    original_urlopen = lb.urllib.request.urlopen
    lb.urllib.request.urlopen = lambda *a, **k: _FakeUrlopenResponse(lines=lines)
    try:
        client = _fresh_client()
        client.always_thinking = True
        chunks = list(client._iter_chat_stream(req=None))
        assert [c["choices"][0]["text"] for c in chunks] == ["First pass."]
    finally:
        lb.urllib.request.urlopen = original_urlopen


def test_stream_does_not_leak_reasoning_when_content_arrives() -> None:
    """When the model does emit visible content, reasoning_content must not
    be appended on top — the user only sees the polished answer."""
    lines = [
        b'data: {"choices":[{"delta":{"reasoning_content":"pondering..."},"finish_reason":null}]}',
        b'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}',
        b'data: {"choices":[{"delta":{"content":" world"},"finish_reason":"stop"}]}',
        b"data: [DONE]",
    ]

    import vedium.llm_backends as lb
    original_urlopen = lb.urllib.request.urlopen
    lb.urllib.request.urlopen = lambda *a, **k: _FakeUrlopenResponse(lines=lines)
    try:
        client = _fresh_client()
        client.always_thinking = True
        chunks = list(client._iter_chat_stream(req=None))
        assert [c["choices"][0]["text"] for c in chunks] == ["Hello", " world"]
    finally:
        lb.urllib.request.urlopen = original_urlopen


def test_thinking_budget_stays_reserved_for_always_thinking_models() -> None:
    """The `_get_thinking_budget` shortcut that returns 0 when
    server_disable_thinking is True must be skipped for LFM2, because its
    template ignores the enable_thinking kwarg — thinking still happens."""
    saved = (
        _backend.reasoning_detected,
        _backend.always_thinking,
        _backend.server_disable_thinking,
        _backend.client,
    )
    try:
        _backend.reasoning_detected = True
        _backend.server_disable_thinking = True
        _backend.client = types.SimpleNamespace(supports_chat_template_kwargs=True)

        _backend.always_thinking = False
        assert _get_thinking_budget(cfg=None) == 0  # historical shortcut

        _backend.always_thinking = True
        assert _get_thinking_budget(cfg=None) == _THINKING_BUDGET_AUTO
    finally:
        (_backend.reasoning_detected, _backend.always_thinking,
         _backend.server_disable_thinking, _backend.client) = saved


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"[FAIL] {fn.__name__}: {exc}")
        except Exception as exc:  # pragma: no cover
            failures += 1
            print(f"[ERROR] {fn.__name__}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
