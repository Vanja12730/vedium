# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Behavioural checks for the free-form chat token budget.

Regression cover for the cut-off bug: the reply budget used to be
``input_words * 2`` capped at 1024, so a short follow-up question earned only a
few hundred output tokens and the answer stopped mid-sentence while most of the
context window sat unused.

Run with: python tests/chat_budget_tests.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.cli_backend_wiring_tests import _load_isolated_llm


class _FakeServerClient:
    """Stands in for _LlamaServerClient: records the kwargs it is streamed with."""

    class _Ctx:
        def __init__(self, n_ctx):
            self.n_ctx = n_ctx

    def __init__(self, n_ctx=8192):
        self._n_ctx = n_ctx
        self.calls = []

    @property
    def context_params(self):
        return self._Ctx(self._n_ctx)

    def stream_messages(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        yield {"choices": [{"text": "ok"}]}


def _server_backed_llm(n_ctx=8192):
    llm, events = _load_isolated_llm()
    client = _FakeServerClient(n_ctx)
    # free_chat_stream dispatches on the real _LlamaServerClient class, so make
    # the fake pass that isinstance check.
    llm._LlamaServerClient = _FakeServerClient
    llm.llm_backends._LlamaServerClient = _FakeServerClient
    llm._backend.client = client
    llm._stream_cancel.clear()
    return llm, client, events


# The conversation that exposed the bug: a long passage, an answer, then a
# three-word follow-up whose reply was truncated mid-sentence.
_PASSAGE = " ".join(["word"] * 75)
_ANSWER = " ".join(["reply"] * 70)
_FOLLOWUP = "can u give an example"


def test_short_followup_still_gets_a_full_reply_budget():
    llm, client, _events = _server_backed_llm(n_ctx=8192)
    history = [
        {"role": "user", "content": _PASSAGE},
        {"role": "assistant", "content": _ANSWER},
        {"role": "user", "content": _FOLLOWUP},
    ]

    list(llm.free_chat_stream(history, "Plain", {}))

    max_tok = client.calls[0][1]["max_tokens"]
    assert max_tok == llm._CHAT_REPLY_TOKENS, (
        f"short follow-up got {max_tok} output tokens, "
        f"expected the full {llm._CHAT_REPLY_TOKENS}"
    )
    # The old heuristic produced input_words * 2 for this exact conversation.
    old_budget = sum(len(t["content"].split()) for t in history) * 2
    assert max_tok > old_budget, "budget is still tied to question length"


def test_budget_is_independent_of_question_length():
    """A one-word question and a long one get the same allowance."""
    budgets = []
    for question in ("why?", " ".join(["elaborate"] * 300)):
        llm, client, _events = _server_backed_llm(n_ctx=32768)
        list(llm.free_chat_stream(
            [{"role": "user", "content": question}], "Plain", {}
        ))
        budgets.append(client.calls[0][1]["max_tokens"])
    assert budgets[0] == budgets[1] == llm._CHAT_REPLY_TOKENS, budgets


def test_small_context_window_clamps_but_leaves_room_to_answer():
    llm, client, _events = _server_backed_llm(n_ctx=1024)
    list(llm.free_chat_stream(
        [{"role": "user", "content": " ".join(["word"] * 200)}], "Plain", {}
    ))
    max_tok = client.calls[0][1]["max_tokens"]
    assert 64 <= max_tok < llm._CHAT_REPLY_TOKENS, max_tok
    # prompt (200 + 200*1.5 = 500) + reply must fit inside n_ctx.
    assert 500 + max_tok <= 1024, max_tok


def test_long_conversation_drops_oldest_turns_instead_of_refusing():
    """The old code hard-failed with 'Conversation too long'; now it trims."""
    llm, client, events = _server_backed_llm(n_ctx=4096)
    history = []
    for i in range(40):
        history.append({"role": "user", "content": f"question {i} " + " ".join(["pad"] * 60)})
        history.append({"role": "assistant", "content": f"answer {i} " + " ".join(["pad"] * 60)})
    history.append({"role": "user", "content": "one final question"})

    out = "".join(llm.free_chat_stream(history, "Plain", {}))

    assert "too long" not in out, out
    assert out == "ok", out
    sent = client.calls[0][0]
    assert sent[-1]["content"] == "one final question", "newest turn must survive"
    assert len(sent) < len(history) + 1, "expected older turns to be dropped"
    assert llm._chat_prompt_tokens(sent[1:]) + llm._CHAT_REPLY_RESERVE <= 4096
    assert client.calls[0][1]["max_tokens"] >= llm._CHAT_REPLY_RESERVE
    assert any("trimmed" in str(a) for a, _k in events), "trim should be logged"


def test_trimmed_history_always_starts_on_a_user_turn():
    """Gemma/Mistral fold the system prompt into history[0] only when it is a
    user turn, and llama-server's Gemma template rejects a non-user opener."""
    turn = lambda role, i: {"role": role, "content": f"{role} {i} " + " ".join(["pad"] * 40)}
    for extra in range(4):  # exercise both parities of the drop boundary
        history = []
        for i in range(30):
            history.append(turn("user", i))
            history.append(turn("assistant", i))
        history.extend(turn("user", 900 + j) for j in range(extra))
        history.append({"role": "user", "content": "final"})

        for n_ctx in (1024, 2048, 4096):
            llm, _client, _events = _server_backed_llm(n_ctx=n_ctx)
            kept, dropped = llm._fit_chat_history(history, n_ctx, llm._CHAT_REPLY_RESERVE)
            assert dropped > 0, (extra, n_ctx)
            assert kept[0]["role"] == "user", (extra, n_ctx, kept[0]["role"])
            assert kept[-1]["content"] == "final"


def test_caller_history_is_not_mutated_by_trimming():
    llm, _client, _events = _server_backed_llm(n_ctx=2048)
    history = [
        {"role": "user", "content": " ".join(["pad"] * 400)},
        {"role": "assistant", "content": " ".join(["pad"] * 400)},
        {"role": "user", "content": "and now?"},
    ]
    before = len(history)
    list(llm.free_chat_stream(history, "Plain", {}))
    assert len(history) == before, "chat window's transcript must stay intact"


def test_single_oversized_message_reports_clearly():
    llm, _client, _events = _server_backed_llm(n_ctx=512)
    out = "".join(llm.free_chat_stream(
        [{"role": "user", "content": " ".join(["word"] * 5000)}], "Plain", {}
    ))
    assert "too long for the current context window" in out, out
    assert "Settings" in out, out


def test_unreadable_n_ctx_falls_back_to_a_safe_budget():
    llm, _client, _events = _server_backed_llm()

    class _NoCtx(_FakeServerClient):
        @property
        def context_params(self):
            raise AttributeError("no /props")

    client = _NoCtx()
    llm._backend.client = client
    list(llm.free_chat_stream([{"role": "user", "content": "hi"}], "Plain", {}))
    assert client.calls[0][1]["max_tokens"] == 1024


def main() -> int:
    tests = [
        test_short_followup_still_gets_a_full_reply_budget,
        test_budget_is_independent_of_question_length,
        test_small_context_window_clamps_but_leaves_room_to_answer,
        test_long_conversation_drops_oldest_turns_instead_of_refusing,
        test_trimmed_history_always_starts_on_a_user_turn,
        test_caller_history_is_not_mutated_by_trimming,
        test_single_oversized_message_reports_clearly,
        test_unreadable_n_ctx_falls_back_to_a_safe_budget,
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
