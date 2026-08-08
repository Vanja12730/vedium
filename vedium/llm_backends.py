# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""LLM backend clients and connection management.

One client per transport (local llama-cpp, llama.cpp server,
OpenAI-compatible cloud, CLI runners), the connect_* / detect_* functions
that smoke-test a client and install it into llm_state._backend, and the
cloud favorite-failover machinery.
"""

import os
import sys
import re
import threading
import time
import urllib.request
import urllib.error
import json

from vedium.config import load_cfg
from vedium import diagnostics
from vedium.managed_server import (
    ManagedServerError,
    find_managed_server,
    start_managed_server,
    stop_managed_server,
)
from vedium.cli_models import (
    CLIClient,
    CLIModelError,
    provenance_footer,
    selectable_models,
)
from vedium.llm_state import _backend, _stream_cancel
from vedium.llm_postprocess import (
    _THINKING_BUDGET_AUTO,
    _finish_is_length,
    _flag_hidden_reasoning,
    _reasoning_starved_output,
)


class _LlamaServerClient:
    """
    Internal client for a running llama-server (llama.cpp HTTP server).

    The __call__ interface retains Vedium's historical completion shape while
    native chat methods use each model's embedded template.

    llama-server quick-start:
        llama-server -m your_model.gguf --port 8080
    Default URL: http://127.0.0.1:8080
    """

    def __init__(self, base_url: str, cfg_n_ctx: int = 4096):
        self.base_url = base_url.rstrip("/")
        self.supports_chat_template_kwargs = True
        self._n_ctx   = cfg_n_ctx     # use the user-configured value; updated by _probe() if server exposes it
        # Control string that, appended to the prompt, makes a "thinking" model
        # skip its chain-of-thought and answer directly. Extracted from the
        # model's own chat template (the enable_thinking=false branch) by
        # _probe(); empty for non-reasoning models. Without this, a reasoning
        # model spends the whole (small) spell-check token budget on hidden
        # reasoning and never emits an answer — so no correction appears.
        self._think_disable_suffix = ""
        self._probe()

    # ── context_params shim (used by _calc_rephrase_max_tokens) ──────────────
    class _ContextParams:
        def __init__(self, n_ctx: int):
            self.n_ctx = n_ctx

    @property
    def context_params(self) -> "_LlamaServerClient._ContextParams":
        return self._ContextParams(self._n_ctx)

    # ── server probe ──────────────────────────────────────────────────────────
    def _probe(self) -> None:
        """Read n_ctx and thinking capability from /props if the server supports it.
        Also detects reasoning models (Qwen3, DeepSeek-R1, QwQ, etc.) from the
        chat template so _calc_rephrase_max_tokens reserves the thinking budget
        from the very first call rather than waiting for the first <think> token.
        """
        import urllib.request, json as _json, re as _re, codecs as _codecs
        # A newly selected model must not inherit the previous model's flag if
        # /props fails or exposes a non-reasoning template.
        _backend.reasoning_detected = False
        try:
            resp = urllib.request.urlopen(f"{self.base_url}/props", timeout=5)
            data = _json.loads(resp.read())
            if "n_ctx" in data:
                self._n_ctx = int(data["n_ctx"])
            # Detect thinking models from the chat template exposed by /props.
            # Different families mark reasoning differently: <think> (Qwen/QwQ/
            # DeepSeek), <|think|> / <|channel>thought (gpt-oss-style, seen with
            # some Gemma builds), or an "enable_thinking" template flag.
            template = str(data.get("chat_template", ""))
            template_str = template + str(data.get("default_generation_settings", ""))
            is_reasoning = any(m in template_str for m in
                               ("<think>", "<|think|>", "enable_thinking", "<|channel>thought"))

            # If the template exposes an enable_thinking=false branch, capture the
            # literal control string it injects so we can disable thinking on the
            # raw /completion endpoint (which never applies the template itself).
            idx = template.find("not enable_thinking")
            if idx != -1:
                seg = template[idx:idx + 300]
                mm = _re.search(r"'([^']*)'", seg)
                if mm:
                    try:
                        self._think_disable_suffix = _codecs.decode(mm.group(1), "unicode_escape")
                    except Exception:
                        self._think_disable_suffix = mm.group(1)

            if is_reasoning:
                # The budget is used only if the user enables thinking. Native
                # chat requests pass enable_thinking=false through the embedded
                # template, so disabled mode does not waste output capacity.
                _backend.reasoning_detected = True
                if _backend.server_disable_thinking:
                    print("[Vedium] Reasoning model detected — thinking disabled by "
                          "the native chat template.", flush=True)
                else:
                    print("[Vedium] Reasoning model detected via /props — thinking budget active.", flush=True)
        except Exception:
            pass

    # ── inference ─────────────────────────────────────────────────────────────
    def __call__(
        self,
        prompt:         str,
        *,
        max_tokens:     int   = 128,
        temperature:    float = 0.1,
        repeat_penalty: float = 1.0,
        stop                  = None,
        echo:           bool  = False,
        stream:         bool  = False,
    ):
        import urllib.request, json as _json

        # Make reasoning models answer directly instead of burning the token
        # budget on hidden chain-of-thought (see _think_disable_suffix). The
        # suffix is the model's own enable_thinking=false control string, so it
        # goes right where generation begins — the end of the built prompt.
        # Gated by the live cfg flag so the user can re-enable thinking.
        if _backend.server_disable_thinking and self._think_disable_suffix:
            prompt = prompt + self._think_disable_suffix

        payload = {
            "prompt":         prompt,
            "n_predict":      max_tokens,
            "temperature":    temperature,
            "repeat_penalty": repeat_penalty,
            "stop":           stop or [],
            "stream":         stream,
        }
        data = _json.dumps(payload).encode()
        req  = urllib.request.Request(
            f"{self.base_url}/completion",
            data    = data,
            headers = {"Content-Type": "application/json"},
            method  = "POST",
        )

        if not stream:
            try:
                resp   = urllib.request.urlopen(req, timeout=120)
                result = _json.loads(resp.read())
                return {"choices": [{"text": result.get("content", "")}]}
            except urllib.error.HTTPError as exc:
                raise RuntimeError(
                    f"llama-server returned HTTP {exc.code} for {self.base_url}/completion "
                    f"— is a model loaded?  ({exc.reason})"
                ) from exc

        return self._iter_stream(req)

    def _chat_payload(
        self,
        messages: list[dict],
        *,
        max_tokens: int,
        temperature: float,
        repeat_penalty: float = 1.0,
        stop=None,
        stream: bool,
    ) -> dict:
        """Build a native llama-server chat request.

        Using the model's embedded Jinja template is essential for newer
        architectures such as Qwen3.5 and Gemma 4. Raw, hand-built ChatML is
        not sufficient to control their reasoning mode reliably.
        """
        return {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "repeat_penalty": repeat_penalty,
            "stop": stop or [],
            "stream": stream,
            "chat_template_kwargs": {
                "enable_thinking": not _backend.server_disable_thinking,
            },
        }

    def complete_messages(
        self,
        messages: list[dict],
        *,
        max_tokens: int,
        temperature: float,
        repeat_penalty: float = 1.0,
        stop=None,
    ) -> dict:
        """Complete semantic chat messages through /v1/chat/completions."""
        payload = self._chat_payload(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            repeat_penalty=repeat_penalty,
            stop=stop,
            stream=False,
        )
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"llama-server returned HTTP {exc.code} for chat completion "
                f"at {self.base_url} ({exc.reason})"
            ) from exc
        choices = result.get("choices") or []
        message = choices[0].get("message", {}) if choices else {}
        return {
            "choices": [{
                "text": message.get("content") or "",
                "finish_reason": choices[0].get("finish_reason") if choices else None,
            }]
        }

    def stream_messages(
        self,
        messages: list[dict],
        *,
        max_tokens: int,
        temperature: float,
        repeat_penalty: float = 1.0,
        stop=None,
    ):
        """Stream semantic chat messages through the OpenAI-compatible route."""
        payload = self._chat_payload(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            repeat_penalty=repeat_penalty,
            stop=stop,
            stream=True,
        )
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._iter_chat_stream(req)

    def _iter_chat_stream(self, req):
        response = urllib.request.urlopen(req, timeout=120)
        try:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content") or ""
                if text:
                    yield {"choices": [{"text": text}]}
                if choices[0].get("finish_reason"):
                    break
        finally:
            response.close()

    def _iter_stream(self, req):
        """Yield Vedium-compatible chunk dictionaries from the SSE stream.

        llama-server ends the stream with a JSON chunk where "stop" is true,
        then closes the connection.  It does NOT send 'data: [DONE]' (that is
        an OpenAI-API convention used only on /v1/chat/completions).  We check
        the "stop" field explicitly so we break cleanly instead of relying on
        the socket closing.  The try/finally ensures resp is always closed —
        even if the caller abandons the generator mid-stream — preventing
        connection leaks to the server.
        """
        import urllib.request, json as _json
        resp = urllib.request.urlopen(req, timeout=120)
        try:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if not line.startswith("data: "):
                    continue
                try:
                    chunk = _json.loads(line[6:])
                    text  = chunk.get("content", "")
                    if text:
                        yield {"choices": [{"text": text}]}
                    if chunk.get("stop"):
                        break
                except _json.JSONDecodeError:
                    pass
        finally:
            resp.close()


# ── Cloud API backend (OpenAI-compatible) ────────────────────────────────────
# Most hosted LLM APIs speak the OpenAI /chat/completions protocol, so a single
# client covers Gemini, Grok, OpenAI, Groq, OpenRouter, DeepSeek, and any other
# compatible endpoint. The user picks a provider preset (which fills the base
# URL) or enters a custom one, plus an API key and a model name.
CLOUD_PROVIDERS = {
    # label:            base_url
    "Google Gemini":   "https://generativelanguage.googleapis.com/v1beta/openai",
    "Grok (xAI)":      "https://api.x.ai/v1",
    "OpenAI":          "https://api.openai.com/v1",
    "Groq":            "https://api.groq.com/openai/v1",
    "OpenRouter":      "https://openrouter.ai/api/v1",
    "DeepSeek":        "https://api.deepseek.com/v1",
    "Custom":          "",
}

# Chat-template scaffolding the local prompt builders emit. Cloud chat APIs want
# clean prose in real messages, so we strip it before sending. Each alternative
# also consumes the role word that rides with the marker (ChatML's "<|im_start|>
# system", Llama-3's "<|start_header_id|>user<|end_header_id|>", Gemma's
# "<start_of_turn>model", Alpaca's "### Instruction:") so no stray role words
# leak into the message text.
_TEMPLATE_TOKEN_RE = re.compile(
    r"<\|im_start\|>[ \t]*(?:system|user|assistant)?[ \t]*\n?"      # ChatML open (+role)
    r"|<\|im_end\|>"
    r"|<\|start_header_id\|>[ \t]*\w+[ \t]*<\|end_header_id\|>"     # Llama-3 header block
    r"|<\|eot_id\|>|<\|begin_of_text\|>|<\|end_of_text\|>"
    r"|<\|begin_of_sentence\|>|<\|end_of_sentence\|>"
    r"|<start_of_turn>[ \t]*(?:user|model)?[ \t]*\n?"              # Gemma open (+role)
    r"|<end_of_turn>"
    r"|\[/?INST\]|</?s>"
    r"|<\|(?:system|user|assistant)\|>"                            # Zephyr-style role tags
    r"|<\|(?:START_OF_TURN_TOKEN|END_OF_TURN_TOKEN|SYSTEM_TOKEN|"
    r"USER_TOKEN|CHATBOT_TOKEN|BOS_TOKEN)\|>"                      # Command-R
    r"|^[ \t]*#{2,}[ \t]*(?:system|instruction|response)[ \t]*:?[ \t]*$",  # Alpaca headers
    re.IGNORECASE | re.MULTILINE,
)

# Plain / DeepSeek builders use "System: …\n\nUser: …\n\nAssistant:" — a clean,
# colon-delimited shape we can safely split into real system/user roles.
_PLAIN_SPLIT_RE = re.compile(
    r"(?is)^\s*system\s*:\s*(.*?)\n\s*user\s*:\s*(.*)$"
)
_TRAILING_CUE_RE = re.compile(r"(?is)\b(?:assistant|model)\s*:?\s*$")


def _prompt_to_messages(prompt: str) -> list:
    """Convert a locally-built prompt string into OpenAI chat messages.

    The local builders wrap a system instruction + user text in model-specific
    template tokens. We strip that scaffolding to clean prose, splitting into
    real system/user roles when the prompt uses the colon-delimited Plain shape,
    and otherwise sending a single user message. The full instruction text is
    preserved either way, so behaviour matches the local path.
    """
    text = _TEMPLATE_TOKEN_RE.sub("", prompt).strip()

    m = _PLAIN_SPLIT_RE.match(text)
    if m:
        sys_part = m.group(1).strip()
        user_part = _TRAILING_CUE_RE.sub("", m.group(2)).strip()
        msgs = []
        if sys_part:
            msgs.append({"role": "system", "content": sys_part})
        msgs.append({"role": "user", "content": user_part or text})
        return msgs

    # Single clean user message (instructions intact); drop any trailing cue.
    text = _TRAILING_CUE_RE.sub("", text).strip()
    return [{"role": "user", "content": text}]


def _chat_history_to_messages(system: str, history: list) -> list:
    """Convert chat history into OpenAI-compatible role/content messages."""
    messages = []
    if system.strip():
        messages.append({"role": "system", "content": system.strip()})
    for turn in history:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        messages.append({"role": role, "content": str(turn.get("content", ""))})
    return messages


def _backend_unavailable_message(cfg: dict | None = None, feature: str = "chat") -> str:
    """Return a plain-language message explaining why a backend is unavailable."""
    cfg = cfg or {}
    mode = (cfg.get("backend_mode") or "local").strip().lower()
    action = "Chat is unavailable right now."

    if mode == "cloud":
        if not cfg.get("cloud_api_key", "").strip():
            return action + " Cloud API is selected, but no API key is configured. Open Settings and add one."
        if not cfg.get("cloud_model", "").strip():
            return action + " Cloud API is selected, but no model is chosen. Open Settings and pick a chat model."
        return action + " Cloud API is selected, but it is not connected yet. Open Settings and click Test & Connect."

    if mode == "server":
        if cfg.get("server_url", "").strip():
            return action + " The local server is selected, but it is not connected yet. Open Settings and click Test / Auto-Detect."
        return action + " The local server mode is selected, but no server URL is set. Open Settings and configure or auto-detect a server."

    if mode == "cli":
        if not cfg.get("cli_models_enabled", False):
            return action + " Command-line models are switched off. Enable them in Settings."
        if not cfg.get("cli_model", "").strip():
            return action + " No command-line model is selected. Open Settings and choose one."
        return action + " The selected command-line model is unavailable. Open Settings and check its installation."

    if mode == "local":
        path = cfg.get("model_path", "").strip()
        if not path:
            return action + " No local GGUF model is selected. Open Settings and choose a model file."
        if not os.path.isfile(path):
            return action + " The selected local GGUF model file was not found. Open Settings and choose a valid model file."
        return action + " A local GGUF model is selected, but it has not finished loading yet. Open Settings and click Save & Load / Connect."

    return action + " No AI backend is configured. Open Settings and choose a local model, local server, or cloud API."


class _CloudClient:
    """OpenAI-compatible chat client. Drop-in for the _backend.client interface.

    Mirrors Vedium's internal completion client shape: __call__ returns
    {"choices":[{"text":...}]} (or yields those dicts when stream=True) and
    exposes context_params.n_ctx, so all existing call sites work unchanged.
    """

    def __init__(self, base_url: str, api_key: str, model: str, cfg_n_ctx: int = 8192):
        self.base_url = base_url.rstrip("/")
        self.api_key  = api_key.strip()
        self.model    = model.strip()
        self._n_ctx   = cfg_n_ctx

    class _ContextParams:
        def __init__(self, n_ctx: int):
            self.n_ctx = n_ctx

    @property
    def context_params(self) -> "_CloudClient._ContextParams":
        return self._ContextParams(self._n_ctx)

    def _request(self, payload: dict):
        import urllib.request, json as _json
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data    = _json.dumps(payload).encode(),
            headers = {
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method  = "POST",
        )
        return urllib.request.urlopen(req, timeout=120)

    def _call_messages(
        self,
        messages: list,
        *,
        max_tokens: int = 128,
        temperature: float = 0.1,
        stop=None,
        stream: bool = False,
        retry_on_empty: bool = True,
    ):
        import urllib.error, json as _json

        payload = {
            "model":       self.model,
            "messages":    messages,
            "max_tokens":  max_tokens,
            "temperature": temperature,
            "stream":      stream,
        }
        if stop:
            payload["stop"] = stop

        if not stream:
            try:
                resp = self._request(payload)
                data = _json.loads(resp.read())
                choice = (data.get("choices") or [{}])[0]
                text = (choice.get("message") or {}).get("content") or ""
                if (_finish_is_length(choice.get("finish_reason"))
                        and _reasoning_starved_output(text)):
                    # The token budget ran out before a dependable answer:
                    # either a server-side thinking model (Gemini-style, hidden
                    # reasoning eats max_tokens, content comes back empty) or an
                    # inline thinker (gemma-4-style <thought> blocks) whose
                    # visible answer got cut off. Reserve the thinking budget
                    # for all later calls and retry this one.
                    # (retry_on_empty=False callers — the connect smoke test —
                    # still flag the model but skip the retry: they don't need
                    # the text, and the extra request would burn free-tier RPM.)
                    _flag_hidden_reasoning(self.model, payload["max_tokens"])
                    if retry_on_empty:
                        retry = dict(payload,
                                     max_tokens=payload["max_tokens"] + _THINKING_BUDGET_AUTO)
                        resp = self._request(retry)
                        data = _json.loads(resp.read())
                        choice = (data.get("choices") or [{}])[0]
                        text = (choice.get("message") or {}).get("content") or ""
                return {"choices": [{"text": text}]}
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                raise RuntimeError(_friendly_cloud_error(exc.code, body, self.model)) from exc
        return self._iter_stream(payload)

    def __call__(
        self,
        prompt:         str,
        *,
        max_tokens:     int   = 128,
        temperature:    float = 0.1,
        repeat_penalty: float = 1.0,   # not supported by chat APIs; accepted+ignored
        stop                  = None,
        echo:           bool  = False,
        stream:         bool  = False,
        retry_on_empty: bool  = True,
    ):
        return self._call_messages(
            _prompt_to_messages(prompt),
            max_tokens=max_tokens,
            temperature=temperature,
            stop=stop,
            stream=stream,
            retry_on_empty=retry_on_empty,
        )

    def stream_messages(
        self,
        messages: list,
        *,
        max_tokens: int = 128,
        temperature: float = 0.7,
        stop=None,
    ):
        """Stream a response from a real chat-message list."""
        return self._call_messages(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stop=stop,
            stream=True,
        )

    def _iter_stream(self, payload, _is_retry: bool = False):
        import urllib.error, json as _json
        try:
            resp = self._request(payload)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise RuntimeError(_friendly_cloud_error(exc.code, body, self.model)) from exc
        yielded_text = False
        finish_reason = None
        try:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = _json.loads(data)
                    choice = (chunk.get("choices") or [{}])[0]
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    text = (choice.get("delta") or {}).get("content", "")
                    if text:
                        yielded_text = True
                        yield {"choices": [{"text": text}]}
                except _json.JSONDecodeError:
                    pass
        finally:
            resp.close()
        if not yielded_text and not _is_retry and _finish_is_length(finish_reason):
            # Same hidden-reasoning signature as the non-stream path: the
            # budget ran out before any visible text. Flag, bump, retry once.
            _flag_hidden_reasoning(self.model, payload["max_tokens"])
            retry = dict(payload,
                         max_tokens=payload["max_tokens"] + _THINKING_BUDGET_AUTO)
            yield from self._iter_stream(retry, _is_retry=True)


def connect_cli(model: str, enabled: bool, effort: str = "") -> str:
    """Select an installed CLI backend without spending a model request."""

    stop_managed_server()

    # Selection failure must not leave the previous backend resident: with the
    # config already set to "cli", that would act like an invisible fallback.
    with _backend.lock:
        _backend.client = None
        _backend.path = ""
        _backend.label = "None"
    diagnostics.set_status(backend="None", backend_mode="cli")

    if not enabled:
        raise CLIModelError(
            "Command-line models are switched off. Enable them in settings."
        )
    selected = next(
        (entry for entry in selectable_models(True) if entry["id"] == model),
        None,
    )
    if selected is None:
        raise CLIModelError(
            f"The command-line model {model!r} is not available. "
            "Install/sign in to its CLI, then reopen Settings."
        )

    client = CLIClient(model, effort, enabled=True)
    label = selected["label"]
    with _backend.lock:
        _backend.client = client
        _backend.path = model
        _backend.label = f"AI CLI ({label})"

    diagnostics.set_status(backend=_backend.label, backend_mode="cli")
    diagnostics.record("backend", "info", f"Selected command-line model {model}.")
    return _backend.label


def connect_cloud(base_url: str, api_key: str, model: str, cfg_n_ctx: int = 8192) -> str:
    """Connect to an OpenAI-compatible cloud API. Smoke-tests, then sets _backend.client.

    Returns a short backend label on success; raises on failure.
    """

    stop_managed_server()
    base_url = base_url.strip()
    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url
    if not api_key.strip():
        raise RuntimeError("No API key provided.")
    if not model.strip():
        raise RuntimeError("No model name provided.")

    client = _CloudClient(base_url, api_key, model, cfg_n_ctx=cfg_n_ctx)
    # Smoke-test: a 1-token completion confirms key + model + endpoint all work.
    # retry_on_empty=False: a thinking model returning empty at 1 token is a
    # PASS (auth and model are proven) — the bigger-budget retry would only
    # waste a request and up to 2k thinking tokens of free-tier quota.
    out = client("System: You are a test.\n\nUser: Say hi.", max_tokens=1,
                 temperature=0.0, retry_on_empty=False)
    if not isinstance(out, dict) or "choices" not in out:
        raise RuntimeError("Unexpected response from cloud API.")

    with _backend.lock:
        _backend.client         = client
        _backend.path    = f"{base_url}::{model}"
        _backend.label = f"Cloud ({model})"

    diagnostics.set_status(backend=_backend.label, backend_mode="cloud")
    diagnostics.record("backend", "info", f"Connected to cloud model {model}.")
    print(f"[Vedium] Connected to cloud API {base_url} (model={model})", file=sys.stderr)
    return _backend.label


# Models that cannot do chat completions (embeddings, audio, image, etc.).
# The /models endpoint lists these alongside chat models, but selecting one
# yields a confusing "not supported for generateContent" error — so we hide them.
_NON_CHAT_MODEL_RE = re.compile(
    r"embedding|embed|tts|whisper|dall-?e|imagen|image-generation|stable-?diffusion"
    r"|moderation|rerank|\baqa\b|veo|sora|text-to-speech|speech",
    re.IGNORECASE,
)


def _friendly_cloud_error(code: int, body: str, model: str = "") -> str:
    """Turn a raw cloud-API HTTP error into a short, plain-language message."""
    msg = ""
    try:
        import json as _json
        data = _json.loads(body)
        if isinstance(data, list) and data:
            data = data[0]
        err = data.get("error", data) if isinstance(data, dict) else data
        if isinstance(err, dict):
            msg = err.get("message", "") or ""
        elif isinstance(err, str):
            msg = err
    except Exception:
        msg = (body or "").strip()[:200]

    low = (msg or "").lower()
    m = f" '{model}'" if model else ""
    if code in (401, 403) or "api key" in low or "unauthorized" in low or "permission" in low:
        return "API key was rejected — it may be invalid, expired, or not allowed to use this model."
    if code == 429 or "quota" in low or "rate limit" in low or "resource_exhausted" in low:
        return ("Rate limit or quota reached — this is usually the free tier's per-minute or "
                "daily cap. Wait a bit, or switch to another (free) model.")
    if (code in (400, 404)
            or "not supported for generatecontent" in low
            or "not found" in low or "is not a chat" in low or "does not exist" in low):
        return (f"Model{m} can't be used for text chat — it's likely an embedding, image, or "
                f"audio model, or it isn't available on your plan. Pick a different model.")
    if 500 <= code < 600:
        return "The provider had a server error. Please try again in a moment."
    return msg or f"Cloud API error (HTTP {code})."


def _is_cloud_quota_error(message: str) -> bool:
    """Return True when a cloud error is likely quota/rate-limit related."""
    low = (message or "").lower()
    return any(
        marker in low
        for marker in (
            "rate limit",
            "quota",
            "resource_exhausted",
            "per-minute",
            "daily cap",
            "429",
        )
    )


def _is_cloud_server_error(message: str) -> bool:
    """True when a cloud error is the provider's fault (5xx, overload).

    The first marker matches _friendly_cloud_error's 5xx wording; the rest
    catch provider messages that reach the caller verbatim ("model is
    overloaded", "service unavailable", ...). Another model on the favorites
    list is likely to work, so these are worth failing over."""
    low = (message or "").lower()
    return any(
        marker in low
        for marker in (
            "server error",
            "internal error",
            "unavailable",
            "overloaded",
            "high demand",
            "try again later",
        )
    )


def _is_cloud_transient_error(message: str) -> bool:
    """Errors worth retrying on the next favorite model: quota/rate limits
    and provider server errors. Anything else (bad key, unknown model, bad
    request) would fail identically on every model, so it is surfaced."""
    return _is_cloud_quota_error(message) or _is_cloud_server_error(message)


def _normalise_cloud_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


def _cloud_favorite_fallbacks(cfg: dict | None, current: "_CloudClient") -> list[dict]:
    """Return usable favorite cloud targets after the current model in favorite order."""
    if not cfg:
        return []

    favorites = [dict(f) for f in cfg.get("cloud_favorites", []) if isinstance(f, dict)]
    if not favorites:
        return []

    current_base = _normalise_cloud_url(current.base_url)
    current_model = current.model.strip()
    current_provider = (cfg.get("cloud_provider") or "").strip()
    key_map = cfg.get("cloud_api_keys", {})
    if not isinstance(key_map, dict):
        key_map = {}

    current_idx = None
    for idx, fav in enumerate(favorites):
        if (
            _normalise_cloud_url(fav.get("base_url", "")) == current_base
            and (fav.get("model") or "").strip() == current_model
        ):
            current_idx = idx
            break

    ordered = (
        favorites[current_idx + 1:] + favorites[:current_idx]
        if current_idx is not None else favorites
    )

    seen = {(current_base, current_model)}
    result = []
    for fav in ordered:
        provider = (fav.get("provider") or "").strip()
        base_url = _normalise_cloud_url(fav.get("base_url", ""))
        model = (fav.get("model") or "").strip()
        if not base_url or not model or (base_url, model) in seen:
            continue

        api_key = (key_map.get(provider) or "").strip()
        if not api_key and provider == current_provider:
            api_key = (cfg.get("cloud_api_key") or "").strip()
        if not api_key and base_url == current_base:
            api_key = current.api_key
        if not api_key:
            continue

        seen.add((base_url, model))
        result.append({
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "api_key": api_key,
        })
    return result


def _activate_cloud_fallback(client: "_CloudClient", favorite: dict, cfg: dict | None) -> None:
    """Make a successful favorite the live cloud backend for later requests."""

    _backend.client = client
    _backend.path = f"{client.base_url}::{client.model}"
    _backend.label = f"Cloud ({client.model})"
    if cfg is not None:
        cfg["backend_mode"] = "cloud"
        provider = favorite.get("provider", cfg.get("cloud_provider", ""))
        cfg["cloud_provider"] = provider
        cfg["cloud_base_url"] = client.base_url
        cfg["cloud_model"] = client.model
        cfg["cloud_api_key"] = client.api_key
        if provider:
            keys = dict(cfg.get("cloud_api_keys", {}))
            keys[provider] = client.api_key
            cfg["cloud_api_keys"] = keys
    diagnostics.set_status(backend=_backend.label, backend_mode="cloud")
    diagnostics.record("backend", "info", f"Switched to fallback cloud model {client.model}.")


def _cloud_fallback_targets(cfg: dict | None, current: "_CloudClient") -> list[tuple["_CloudClient", dict | None]]:
    """Return the current cloud client followed by usable favorites in order."""
    targets: list[tuple[_CloudClient, dict | None]] = [(current, None)]
    for favorite in _cloud_favorite_fallbacks(cfg, current):
        targets.append((
            _CloudClient(
                favorite["base_url"], favorite["api_key"], favorite["model"],
                cfg_n_ctx=max(getattr(current, "_n_ctx", 8192), 8192),
            ),
            favorite,
        ))
    return targets


def _cloud_fallback_error(errors: list[str]) -> RuntimeError:
    return RuntimeError(
        "All available favorite cloud models hit a rate limit, quota, or provider error. "
        f"Tried: {'; '.join(errors)}"
    )


def _record_cloud_failover(model: str, message: str) -> None:
    reason = ("hit a quota/rate limit" if _is_cloud_quota_error(message)
              else "had a provider server error")
    diagnostics.record("backend", "warning",
                       f"Cloud model {model} {reason}.", message)


def _cloud_complete_with_fallback(messages: list, *, max_tokens: int,
                                  temperature: float, stop=None,
                                  cfg: dict | None = None, on_fallback=None) -> dict:
    """Complete once, retrying pre-response quota and provider-server failures."""
    current = _backend.client
    if not isinstance(current, _CloudClient):
        raise RuntimeError("Cloud fallback requested without a cloud backend.")

    transient_errors = []
    failed_models = []   # model names verbatim — model ids may themselves contain ":"
    for index, (client, favorite) in enumerate(_cloud_fallback_targets(cfg, current)):
        if index and on_fallback:
            failed_model = failed_models[-1] if failed_models else current.model
            try:
                on_fallback(failed_model, client.model)
            except Exception:
                pass
        try:
            result = client._call_messages(
                messages, max_tokens=max_tokens, temperature=temperature, stop=stop,
            )
            if index:
                _activate_cloud_fallback(client, favorite or {}, cfg)
            return result
        except RuntimeError as exc:
            message = str(exc)
            if not _is_cloud_transient_error(message):
                raise
            failed_models.append(client.model)
            transient_errors.append(f"{client.model}: {message}")
            _record_cloud_failover(client.model, message)

    raise _cloud_fallback_error(transient_errors)


def _cloud_stream_with_fallback(messages: list, *, max_tokens: int,
                                temperature: float, stop=None,
                                cfg: dict | None = None, on_fallback=None):
    """Stream a cloud reply, trying favorites after a quota or provider-server
    failure before any output has been produced.

    Once a model has yielded text, retrying with another model would duplicate or
    splice together two replies, so that partial-response failure is surfaced.
    """
    current = _backend.client
    if not isinstance(current, _CloudClient):
        raise RuntimeError("Cloud fallback requested without a cloud backend.")

    transient_errors = []
    failed_models = []   # model names verbatim — model ids may themselves contain ":"
    for index, (client, favorite) in enumerate(_cloud_fallback_targets(cfg, current)):
        if _stream_cancel.is_set():
            return
        if index and on_fallback:
            failed_model = failed_models[-1] if failed_models else current.model
            try:
                on_fallback(failed_model, client.model)
            except Exception:
                pass
        received_reply = False
        try:
            for chunk in client.stream_messages(
                messages, max_tokens=max_tokens, temperature=temperature, stop=stop,
            ):
                if _stream_cancel.is_set():
                    return
                if index and not received_reply:
                    _activate_cloud_fallback(client, favorite or {}, cfg)
                received_reply = True
                yield chunk
            return
        except RuntimeError as exc:
            message = str(exc)
            if received_reply or not _is_cloud_transient_error(message):
                raise
            failed_models.append(client.model)
            transient_errors.append(f"{client.model}: {message}")
            _record_cloud_failover(client.model, message)

    raise _cloud_fallback_error(transient_errors)


def _cli_enabled_for_request(client: CLIClient, cfg: dict | None) -> bool:
    return client.enabled if cfg is None else bool(cfg.get("cli_models_enabled", False))


def _record_cli_provenance(provenance: dict) -> None:
    diagnostics.record("backend", "info", provenance_footer(provenance))


def _complete_prompt(prompt: str, *, max_tokens: int, temperature: float,
                     repeat_penalty: float = 1.0, stop=None, echo: bool = False,
                     cfg: dict | None = None, on_fallback=None,
                     messages: list[dict] | None = None) -> dict:
    """Run one completion, with favorite failover when the live backend is cloud."""
    client = _backend.client
    if isinstance(client, CLIClient):
        if messages is None:
            raise CLIModelError("Command-line model request is missing semantic messages.")
        text, provenance = client.complete(
            messages, enabled=_cli_enabled_for_request(client, cfg)
        )
        _record_cli_provenance(provenance)
        return {"choices": [{"text": text}], "cli_provenance": provenance}
    if isinstance(client, _CloudClient):
        return _cloud_complete_with_fallback(
            _prompt_to_messages(prompt), max_tokens=max_tokens,
            temperature=temperature, stop=stop, cfg=cfg, on_fallback=on_fallback,
        )
    if isinstance(client, _LlamaServerClient) and messages is not None:
        return client.complete_messages(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            repeat_penalty=repeat_penalty,
            stop=stop,
        )
    return client(
        prompt, max_tokens=max_tokens, temperature=temperature,
        repeat_penalty=repeat_penalty, stop=stop, echo=echo,
    )


def _stream_prompt(prompt: str, *, max_tokens: int, temperature: float,
                   repeat_penalty: float = 1.0, stop=None, echo: bool = False,
                   cfg: dict | None = None, on_fallback=None,
                   messages: list[dict] | None = None):
    """Stream one prompt, with quota failover for cloud backends."""
    client = _backend.client
    if isinstance(client, CLIClient):
        if messages is None:
            raise CLIModelError("Command-line model request is missing semantic messages.")
        if _stream_cancel.is_set():
            return
        text, provenance = client.complete(
            messages, enabled=_cli_enabled_for_request(client, cfg)
        )
        _record_cli_provenance(provenance)
        if not _stream_cancel.is_set():
            yield {"choices": [{"text": text}], "cli_provenance": provenance}
        return
    if isinstance(client, _CloudClient):
        yield from _cloud_stream_with_fallback(
            _prompt_to_messages(prompt), max_tokens=max_tokens,
            temperature=temperature, stop=stop, cfg=cfg,
            on_fallback=on_fallback,
        )
        return
    if isinstance(client, _LlamaServerClient) and messages is not None:
        yield from client.stream_messages(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            repeat_penalty=repeat_penalty,
            stop=stop,
        )
        return
    yield from client(
        prompt, max_tokens=max_tokens, temperature=temperature,
        repeat_penalty=repeat_penalty, stop=stop, echo=echo, stream=True,
    )


def fetch_cloud_models(base_url: str, api_key: str, timeout: float = 15.0) -> list:
    """List CHAT models from an OpenAI-compatible /models endpoint.

    Returns a list of (model_id, is_free) tuples, free models first. Non-chat
    models (embeddings, audio, image…) are filtered out. "Free" is only known
    for providers that expose pricing (OpenRouter: zero price or a ':free'
    suffix); elsewhere is_free is False and the caller should rely on the
    provider's free-tier note. Raises on HTTP / parse errors.
    """
    import urllib.request, json as _json

    base = base_url.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    req = urllib.request.Request(
        f"{base}/models",
        headers={"Authorization": f"Bearer {api_key.strip()}"},
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    data = _json.loads(resp.read())
    items = data.get("data") or data.get("models") or []

    out = []
    for it in items:
        if isinstance(it, str):
            mid, pricing = it, {}
        else:
            mid = it.get("id") or it.get("name") or it.get("model")
            pricing = it.get("pricing") or {}
        if not mid:
            continue
        mid = str(mid)
        if mid.startswith("models/"):     # Gemini ids come back as "models/<name>"
            mid = mid[len("models/"):]
        if _NON_CHAT_MODEL_RE.search(mid):
            continue                      # skip embedding / audio / image models
        free = mid.endswith(":free")
        if pricing:
            try:
                free = free or (float(pricing.get("prompt", 1) or 0) == 0
                                and float(pricing.get("completion", 1) or 0) == 0)
            except (TypeError, ValueError):
                pass
        out.append((mid, free))

    # De-duplicate, then sort free-first and alphabetically.
    seen, uniq = set(), []
    for mid, free in out:
        if mid not in seen:
            seen.add(mid)
            uniq.append((mid, free))
    uniq.sort(key=lambda t: (not t[1], t[0].lower()))
    return uniq


def connect_server(url: str, cfg_n_ctx: int = 4096) -> str:
    """
    Connect to a running llama-server at url.
    Creates a _LlamaServerClient, smoke-tests it, assigns it to _backend.client.
    cfg_n_ctx is the context-window size from the user's settings; used as
    the fallback when the server's /props endpoint does not expose n_ctx.
    Returns a short backend label string on success; raises on failure.
    """
    import urllib.request, json as _json

    # Manual server mode supersedes only the child process Vedium owns. An
    # arbitrary external llama-server is never terminated.
    stop_managed_server()

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    client = _LlamaServerClient(url, cfg_n_ctx=cfg_n_ctx)

    # Smoke-test: send a minimal completion to confirm the server is alive
    # and accepting requests before we commit the client to _backend.client.
    payload = _json.dumps({"prompt": "Hi", "n_predict": 1, "stream": False}).encode()
    req = urllib.request.Request(
        f"{client.base_url}/completion",
        data    = payload,
        headers = {"Content-Type": "application/json"},
        method  = "POST",
    )
    urllib.request.urlopen(req, timeout=8)   # raises urllib.error.URLError on failure

    with _backend.lock:
        _backend.client         = client
        _backend.path    = url
        _backend.label = f"Server ({url})"

    diagnostics.set_status(backend=_backend.label, backend_mode="server")
    diagnostics.record("backend", "info", f"Connected to local server {url}.")
    print(f"[Vedium] Connected to llama-server at {url} (n_ctx={client._n_ctx})",
          file=sys.stderr)
    return _backend.label


# Common ports a local llama-server is started on. Scanned at startup (in this
# priority order) when auto_detect_server is enabled and no server_url is set.
# 8080 is llama-server's default; the rest cover frequent manual choices.
_LOCAL_SERVER_PORTS = (
    8080, 8000, 8033, 8081, 8082, 8083, 8001, 8008,
    8088, 8888, 5000, 5001, 3000, 4000,
)


def _port_is_open(host: str, port: int, timeout: float) -> bool:
    """Return True if a TCP connection to host:port succeeds within timeout."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _is_llama_server(base: str, timeout: float) -> bool:
    """Confirm base URL is a llama.cpp server we can actually talk to.

    Tries the GET /health and /props endpoints first, then falls back to a
    tiny /completion POST (the same protocol connect_server uses). This
    filters out unrelated services that happen to hold an open port, and
    OpenAI-only servers (Ollama/LM Studio) that don't speak /completion.
    """
    import urllib.request, json as _json

    for ep in ("/health", "/props"):
        try:
            resp = urllib.request.urlopen(f"{base}{ep}", timeout=timeout)
            if getattr(resp, "status", 200) == 200:
                return True
        except Exception:
            pass

    try:
        payload = _json.dumps({"prompt": "Hi", "n_predict": 1, "stream": False}).encode()
        req = urllib.request.Request(
            f"{base}/completion", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=timeout * 6)
        return True
    except Exception:
        return False


def detect_local_server(timeout: float = 0.3, host: str = "127.0.0.1") -> str:
    """Scan common local ports for a running llama-server.

    Two phases: (1) a fast parallel TCP scan to find which ports are even
    listening, then (2) validate each open port — in priority order — is
    actually a llama-server before returning its base URL. Returns "" if none
    is found. The parallel scan keeps total time to roughly one timeout even
    though closed localhost ports can be slow to refuse on Windows.
    """
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(_LOCAL_SERVER_PORTS)) as pool:
        open_flags = list(pool.map(
            lambda p: _port_is_open(host, p, timeout), _LOCAL_SERVER_PORTS))

    open_ports = [p for p, is_open in zip(_LOCAL_SERVER_PORTS, open_flags) if is_open]
    if open_ports:
        print(f"[Vedium] Open local ports: {open_ports}", file=sys.stderr)

    for port in open_ports:
        base = f"http://{host}:{port}"
        if _is_llama_server(base, timeout):
            print(f"[Vedium] Auto-detected llama-server at {base}", file=sys.stderr)
            return base
    return ""


def load_model(path: str):
    """Load a local GGUF with Vedium's managed llama-server process."""
    cfg = load_cfg()
    gpu_mode = cfg.get("gpu_acceleration", "Auto")
    n_ctx = int(cfg.get("n_ctx", 4096))
    server_executable = find_managed_server(cfg.get("managed_server_path", ""))

    if not server_executable:
        stop_managed_server()
        with _backend.lock:
            _backend.client = None
            _backend.path = ""
            _backend.label = "None"
        msg = (
            "The managed local-model engine is not installed. Re-run the "
            "Vedium installer and choose the NVIDIA GPU or CPU engine. "
            "Cloud API, CLI, and manually configured server modes are unaffected."
        )
        diagnostics.set_status(backend="Unavailable", backend_mode="local")
        diagnostics.record("backend", "error", "Local model backend unavailable.", msg)
        raise RuntimeError(msg)

    with _backend.lock:
        _backend.client = None
        _backend.path = ""
        _backend.label = "None"

    try:
        url = start_managed_server(
            path,
            n_ctx=n_ctx,
            gpu_mode=gpu_mode,
            configured_path=server_executable,
        )
        client = _LlamaServerClient(url, cfg_n_ctx=n_ctx)
        # /health only proves that the process started. A real chat request also
        # verifies the GGUF architecture and its embedded template.
        probe = client.complete_messages(
            [{"role": "user", "content": "Reply with OK."}],
            max_tokens=16,
            temperature=0.0,
        )
        if not isinstance(probe, dict) or not probe.get("choices"):
            raise ManagedServerError(
                "Managed server returned an invalid probe response."
            )
    except Exception as exc:
        stop_managed_server()
        msg = (
            "The managed local engine could not load this GGUF model. "
            f"{exc}"
        )
        diagnostics.set_status(backend="Load failed", backend_mode="local")
        diagnostics.record("backend", "error", "Local GGUF load failed.", msg)
        raise RuntimeError(msg) from exc

    executable_lower = server_executable.lower()
    if "vulkan" in executable_lower:
        engine = "Vulkan"
        label = "Local server GPU (Vulkan)"
    elif "cuda" in executable_lower:
        engine = "CUDA"
        label = "Local server GPU (CUDA)"
    elif "cpu" in executable_lower or gpu_mode == "Off":
        engine = "CPU"
        label = "Local server (CPU)"
    else:
        engine = "managed auto-fit"
        label = "Local server (managed auto-fit)"

    with _backend.lock:
        _backend.client = client
        _backend.path = path
        _backend.label = label
    diagnostics.set_status(backend=label, backend_mode="local")
    diagnostics.record(
        "backend", "info",
        f"Loaded local GGUF with managed llama-server ({engine}).",
    )
    print(
        f"[Vedium] Managed llama-server loaded {path} at {url} ({engine}).",
        file=sys.stderr,
    )
    return label
