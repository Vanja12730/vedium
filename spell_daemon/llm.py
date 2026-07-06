import os
import sys
import re
import threading
import time
import urllib.request
import urllib.error
import json
import difflib
import unicodedata

from spell_daemon.constants import _SLAVIC_LANGS, _REPHRASE_PROMPTS, _CJK_RE, _word_count
from spell_daemon.config import load_cfg
from spell_daemon.prompt_builder import _build_prompt, _build_chat_prompt
from spell_daemon import diagnostics

# LLM globals, matching the namespace of the original module
_llm = None
_llm_path = ""
_llm_backend = "None"
_llm_lock = threading.Lock()
_stream_cancel = threading.Event()
_reasoning_model_detected = False
_server_disable_thinking = True

def _make_llama(path: str, *, use_gpu: bool, n_ctx: int = 768):
    """Create a Llama instance. If use_gpu=True, request full GPU layer offload.
    n_ctx sets the KV cache / context window size; read from config by load_model()."""
    from llama_cpp import Llama

    kwargs = {
        "model_path": path,
        "n_ctx":      n_ctx,  # configurable via Settings; default 768
                              # (system ~150 tokens + sentence ~80 + output ~100 = ~330 peak)
                              # raise to 2048-4096 for long selected-text rephrases
        "n_batch":    128,    # smaller batch = less prefill overhead for short prompts
        "n_threads":  os.cpu_count() or 4,
        "verbose":    False,
    }
    if use_gpu:
        # -1 asks llama.cpp to offload all possible layers. If CUDA/GPU support is
        # unavailable or loading fails, load_model() catches the error and retries CPU.
        kwargs["n_gpu_layers"] = -1

    return Llama(**kwargs)


class _LlamaServerClient:
    """
    Drop-in replacement for a llama_cpp.Llama instance that talks to a
    running llama-server (llama.cpp HTTP server) instead of loading a model
    in-process.

    The __call__ interface mirrors llama_cpp.Llama closely enough that all
    existing call sites (spell_check_sentence, rephrase_sentence_stream,
    chat_with_text_stream) work without any modification.

    llama-server quick-start:
        llama-server -m your_model.gguf --port 8080
    Default URL: http://127.0.0.1:8080
    """

    def __init__(self, base_url: str, cfg_n_ctx: int = 4096):
        self.base_url = base_url.rstrip("/")
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
        global _reasoning_model_detected
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
                # Always reserve the thinking budget so that IF the user turns the
                # "disable thinking" setting off, spell-check/rephrase still have
                # token room for the model to reason. Harmless when thinking is
                # disabled (just a higher max_tokens cap the model stops short of).
                _reasoning_model_detected = True
                if self._think_disable_suffix:
                    print("[SpellDaemon] Reasoning model detected — thinking disabled by "
                          "default (toggle in Settings → server panel).", flush=True)
                else:
                    print("[SpellDaemon] Reasoning model detected via /props — thinking budget active.", flush=True)
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
        if _server_disable_thinking and self._think_disable_suffix:
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

    def _iter_stream(self, req):
        """Yield llama_cpp-compatible chunk dicts from the SSE stream.

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

    if mode == "local":
        path = cfg.get("model_path", "").strip()
        if not path:
            return action + " No local GGUF model is selected. Open Settings and choose a model file."
        if not os.path.isfile(path):
            return action + " The selected local GGUF model file was not found. Open Settings and choose a valid model file."
        return action + " A local GGUF model is selected, but it has not finished loading yet. Open Settings and click Save & Load / Connect."

    return action + " No AI backend is configured. Open Settings and choose a local model, local server, or cloud API."


class _CloudClient:
    """OpenAI-compatible chat client. Drop-in for the _llm interface.

    Mirrors llama_cpp.Llama / _LlamaServerClient: __call__ returns
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
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
                return {"choices": [{"text": text or ""}]}
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
    ):
        return self._call_messages(
            _prompt_to_messages(prompt),
            max_tokens=max_tokens,
            temperature=temperature,
            stop=stop,
            stream=stream,
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

    def _iter_stream(self, payload):
        import urllib.error, json as _json
        try:
            resp = self._request(payload)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise RuntimeError(_friendly_cloud_error(exc.code, body, self.model)) from exc
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
                    delta = (chunk.get("choices") or [{}])[0].get("delta", {})
                    text  = delta.get("content", "")
                    if text:
                        yield {"choices": [{"text": text}]}
                except _json.JSONDecodeError:
                    pass
        finally:
            resp.close()


def connect_cloud(base_url: str, api_key: str, model: str, cfg_n_ctx: int = 8192) -> str:
    """Connect to an OpenAI-compatible cloud API. Smoke-tests, then sets _llm.

    Returns a short backend label on success; raises on failure.
    """
    global _llm, _llm_path, _llm_backend

    base_url = base_url.strip()
    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url
    if not api_key.strip():
        raise RuntimeError("No API key provided.")
    if not model.strip():
        raise RuntimeError("No model name provided.")

    client = _CloudClient(base_url, api_key, model, cfg_n_ctx=cfg_n_ctx)
    # Smoke-test: a 1-token completion confirms key + model + endpoint all work.
    out = client("System: You are a test.\n\nUser: Say hi.", max_tokens=1, temperature=0.0)
    if not isinstance(out, dict) or "choices" not in out:
        raise RuntimeError("Unexpected response from cloud API.")

    with _llm_lock:
        _llm         = client
        _llm_path    = f"{base_url}::{model}"
        _llm_backend = f"Cloud ({model})"

    diagnostics.set_status(backend=_llm_backend, backend_mode="cloud")
    diagnostics.record("backend", "info", f"Connected to cloud model {model}.")
    print(f"[SpellDaemon] Connected to cloud API {base_url} (model={model})", file=sys.stderr)
    return _llm_backend


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
    Creates a _LlamaServerClient, smoke-tests it, assigns it to _llm.
    cfg_n_ctx is the context-window size from the user's settings; used as
    the fallback when the server's /props endpoint does not expose n_ctx.
    Returns a short backend label string on success; raises on failure.
    """
    global _llm, _llm_path, _llm_backend
    import urllib.request, json as _json

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    client = _LlamaServerClient(url, cfg_n_ctx=cfg_n_ctx)

    # Smoke-test: send a minimal completion to confirm the server is alive
    # and accepting requests before we commit the client to _llm.
    payload = _json.dumps({"prompt": "Hi", "n_predict": 1, "stream": False}).encode()
    req = urllib.request.Request(
        f"{client.base_url}/completion",
        data    = payload,
        headers = {"Content-Type": "application/json"},
        method  = "POST",
    )
    urllib.request.urlopen(req, timeout=8)   # raises urllib.error.URLError on failure

    with _llm_lock:
        _llm         = client
        _llm_path    = url
        _llm_backend = f"Server ({url})"

    diagnostics.set_status(backend=_llm_backend, backend_mode="server")
    diagnostics.record("backend", "info", f"Connected to local server {url}.")
    print(f"[SpellDaemon] Connected to llama-server at {url} (n_ctx={client._n_ctx})",
          file=sys.stderr)
    return _llm_backend


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
        print(f"[SpellDaemon] Open local ports: {open_ports}", file=sys.stderr)

    for port in open_ports:
        base = f"http://{host}:{port}"
        if _is_llama_server(base, timeout):
            print(f"[SpellDaemon] Auto-detected llama-server at {base}", file=sys.stderr)
            return base
    return ""


def _cuda_device_count() -> int:
    """
    Return the number of CUDA devices visible to llama-cpp-python.
    Uses ctranslate2 if available (same check as voice_anywhere), then falls
    back to reading llama_cpp directly.  Returns 0 if nothing is found.
    """
    try:
        import ctranslate2 as _ct2
        return _ct2.get_cuda_device_count()
    except Exception:
        pass
    try:
        from llama_cpp import llama_backend_init  # noqa: F401  (presence confirms GPU build)
        # llama_cpp exposes no explicit device-count API; rely on the fact that
        # n_gpu_layers > 0 will simply fail at load time if CUDA is absent.
        return 1  # optimistic — actual check happens in _make_llama
    except Exception:
        return 0


def _detect_gpu_backend() -> str:
    """
    Return a human-readable label for the active GPU backend.

    Three methods are tried in order; the first one that succeeds wins:

    1. GGML backend registry  (llama-cpp-python >= 0.3 / modern llama.cpp)
       ggml_backend_reg_get_name() enumerates every loaded backend — the
       most reliable source because it reflects what is actually running, not
       just what was compiled in.

    2. llama_print_system_info() string  (older builds)
       Parses the "KEY = VALUE | …" feature string.  Spaces around "=" are
       stripped before matching to cope with minor formatting differences
       across llama.cpp versions.

    3. OS heuristic
       Metal is macOS-exclusive, so Darwin + GPU load success → Metal.

    Returns "GPU (unknown)" only if all three methods fail.
    """
    # ── Method 1: GGML backend registry ──────────────────────────────────────
    try:
        import llama_cpp.llama_cpp as _lc
        count = _lc.ggml_backend_reg_count()
        names = []
        for i in range(count):
            reg  = _lc.ggml_backend_reg_get(i)
            name = _lc.ggml_backend_reg_name(reg)
            if isinstance(name, (bytes, bytearray)):
                name = name.decode()
            names.append(name.upper())
        # Walk priority order — skip the always-present CPU backend
        _registry_checks = [
            ("CUDA",   "CUDA"),
            ("CUBLAS", "CUDA"),
            ("METAL",  "Metal"),
            ("HIP",    "ROCm (HIP)"),
            ("ROCM",   "ROCm (HIP)"),
            ("VULKAN", "Vulkan"),
            ("SYCL",   "SYCL"),
            ("OPENCL", "OpenCL"),
        ]
        for fragment, label in _registry_checks:
            if any(fragment in n for n in names):
                return label
    except Exception:
        pass

    # ── Method 2: llama_print_system_info() ──────────────────────────────────
    try:
        from llama_cpp import llama_print_system_info
        raw  = llama_print_system_info()
        info = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
        # Normalise spacing so "CUDA = 1" and "CUDA=1" both match
        info_norm = info.replace(" ", "")
        _sysinfo_checks = [
            ("CUDA=1",    "CUDA"),
            ("cuBLAS=1",  "CUDA"),
            ("CUBLAS=1",  "CUDA"),
            ("Metal=1",   "Metal"),
            ("HIP=1",     "ROCm (HIP)"),
            ("Vulkan=1",  "Vulkan"),
            ("SYCL=1",    "SYCL"),
            ("OpenCL=1",  "OpenCL"),
        ]
        for marker, label in _sysinfo_checks:
            if marker in info_norm:
                return label
    except Exception:
        pass

    # ── Method 3: OS heuristic ────────────────────────────────────────────────
    import platform
    if platform.system() == "Darwin":
        return "Metal"

    return "GPU (unknown)"


def load_model(path: str):
    """
    Load (or reload) a GGUF model from disk. Blocks until done.
    Returns a short backend label: "GPU", "CPU", or "CPU (GPU fallback)".
    """
    global _llm, _llm_path, _llm_backend

    # Fail fast with a clear message when llama-cpp-python is missing.
    # Without this check the GPU attempt fails with ModuleNotFoundError, the
    # code prints a misleading "falling back to CPU", and the CPU attempt
    # then fails with the exact same missing-module error.
    try:
        import llama_cpp  # noqa: F401
    except ImportError as exc:
        msg = (
            "llama-cpp-python is not installed in this Python "
            f"({sys.version_info.major}.{sys.version_info.minor}, {sys.executable}) — "
            "local GGUF models are unavailable. Cloud API and llama-server "
            "backends are unaffected. Install it with: pip install llama-cpp-python"
        )
        diagnostics.record("backend", "error", "Local model backend unavailable.", msg)
        print(f"[SpellDaemon] {msg}", file=sys.stderr)
        raise RuntimeError(msg) from exc

    cfg = load_cfg()
    gpu_mode = cfg.get("gpu_acceleration", "Auto")
    wants_gpu = gpu_mode in ("Auto", "Full")
    n_ctx     = int(cfg.get("n_ctx", 768))

    if not wants_gpu:
        print("[SpellDaemon] GPU acceleration disabled — loading on CPU.", file=sys.stderr)

    with _llm_lock:
        if wants_gpu:
            try:
                _llm = _make_llama(path, use_gpu=True, n_ctx=n_ctx)
                # Warmup: run a minimal inference to catch missing DLLs or driver
                # issues before the first real sentence arrives.  Same pattern as
                # voice_anywhere.py — the first real inference should not pay the
                # JIT cost or discover a broken CUDA install mid-sentence.
                try:
                    _llm("Hi", max_tokens=1, echo=False)
                    gpu_backend = _detect_gpu_backend()
                    _llm_backend = f"GPU ({gpu_backend})"
                    print(f"[SpellDaemon] GPU warmup passed — running on {gpu_backend}.", file=sys.stderr)
                except Exception as warmup_err:
                    print(
                        f"[SpellDaemon] GPU warmup failed ({warmup_err}); reloading on CPU.",
                        file=sys.stderr,
                    )
                    _llm = _make_llama(path, use_gpu=False, n_ctx=n_ctx)
                    _llm_backend = "CPU (GPU warmup failed)"
                    _llm_path = path
                    diagnostics.set_status(backend=_llm_backend, backend_mode="local")
                    diagnostics.record("backend", "info", f"Loaded local model on {_llm_backend}.")
                    return _llm_backend
            except Exception as gpu_error:
                diagnostics.record("backend", "warning", "GPU load failed; falling back to CPU.", str(gpu_error))
                print(
                    f"[SpellDaemon] GPU load failed; falling back to CPU: {gpu_error}",
                    file=sys.stderr,
                )

        _llm = _make_llama(path, use_gpu=False, n_ctx=n_ctx)
        _llm_path = path
        _llm_backend = "CPU (GPU fallback)" if wants_gpu else "CPU"
        diagnostics.set_status(backend=_llm_backend, backend_mode="local")
        diagnostics.record("backend", "info", f"Loaded local model on {_llm_backend}.")
        return _llm_backend


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


def _is_translation_mode(mode: str) -> bool:
    """Return True for explicit translation modes that manage their own output language."""
    return (mode or "").startswith("Translate ")


# Reasoning-tag variants emitted by different model families, matched
# case-insensitively and tolerant of surrounding whitespace.  DeepSeek-R1 /
# QwQ use <think>; some Qwen3 and other builds use <thinking>; a handful use
# <reason>/<reasoning> or <thought>...</thought>.
_THINK_OPEN_RE  = re.compile(r"<\s*(?:think|thinking|thought|reason|reasoning)\s*>", re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"<\s*/\s*(?:think|thinking|thought|reason|reasoning)\s*>", re.IGNORECASE)

# --- Harmony / channel-format scaffolding (gpt-oss and similar) ---------------
# These models wrap output in <|channel|>NAME<|message|>...<|end|> segments.
# The user-facing answer lives in the "final" channel; "analysis"/"commentary"/
# "reasoning" channels are chain-of-thought and must NEVER reach the document.
# Some local templates emit malformed single-pipe variants such as
# "<|channel>thought<channel|>", so every token pattern tolerates a missing pipe
# on either side.  The harmony-token pattern deliberately requires at least one
# pipe so it never eats generic angle-bracket content like List<String> or <br>.
_HARMONY_TOK_RE = re.compile(r"<\|[A-Za-z_]+\|?>|<[A-Za-z_]+\|>")
_CHANNEL_HDR_RE = re.compile(r"<\|?channel\|?>\s*([A-Za-z]+)", re.IGNORECASE)
_FINAL_HDR_RE   = re.compile(r"<\|?channel\|?>\s*final\b", re.IGNORECASE)
_COT_CHANNELS   = {"analysis", "commentary", "reasoning"}
_LEAD_NOISE_RE  = re.compile(
    r"^\s*(?:assistant|developer|system|user|final|analysis|commentary|reasoning|thought|thinking)\b[:\s]*",
    re.IGNORECASE,
)

# Some local templates (gpt-oss-style, observed with Gemma builds) delimit the
# end of the reasoning block with a literal "<thought_end>" marker instead of a
# closing </think> tag, e.g.:
#   <|channel>...reasoning...<thought_end><channel|>the actual answer
# The answer is everything after the LAST such marker. Tolerates pipe and
# separator variants: <|thought_end|>, <thought-end>, <thought end>, etc.
_THOUGHT_END_RE = re.compile(r"<\s*\|?\s*thought[_\-\s]*end\s*\|?\s*>", re.IGNORECASE)
# The matching opener, used by the streaming filter to know reasoning has begun.
_THOUGHT_OPEN_RE = re.compile(r"<\s*\|?\s*(?:thought[_\-\s]*(?:start|begin)?|channel)\s*\|?\s*>", re.IGNORECASE)


def _strip_channels(text: str) -> str:
    """Remove harmony channel scaffolding, keeping only the answer text.

    Handles three shapes:
      1. final channel present  – keep only what follows the LAST "final"
         channel header (canonical gpt-oss answer location; drops any preceding
         analysis/commentary reasoning).
      2. only CoT channels      – no "final" channel and every channel seen is a
         known chain-of-thought one (analysis/commentary/reasoning): the answer
         never made it out (e.g. truncated mid-reasoning) -> return "" so the
         safety layer falls back to the original text instead of pasting CoT.
      3. other / malformed      – strip the scaffolding tokens and keep the
         prose.  Covers models that route the actual answer through a
         non-standard channel name (the observed case used a channel literally
         called "thought").
    """
    if "<|" not in text and "|>" not in text:
        return text

    finals = list(_FINAL_HDR_RE.finditer(text))
    if finals:
        text = text[finals[-1].end():]
    else:
        names = [m.group(1).lower() for m in _CHANNEL_HDR_RE.finditer(text)]
        if names and all(n in _COT_CHANNELS for n in names):
            return ""

    text = _HARMONY_TOK_RE.sub("", text)
    # Drop a leading channel-name or role word left behind by the token removal
    # (e.g. "thought\n...", "assistant: ...").  Loop a few times to peel a short
    # "assistant final" style run.
    for _ in range(4):
        peeled = _LEAD_NOISE_RE.sub("", text)
        if peeled == text:
            break
        text = peeled
    return text.strip()


def _strip_reasoning(text: str) -> str:
    """Remove a reasoning model's chain-of-thought from raw output.

    Handles three output shapes, not just the well-formed one:

      1. Balanced       "<think>...</think>answer"        -> "answer"
      2. Close-only     "...reasoning...</think>answer"   -> "answer"
         DeepSeek-R1 and similar models inject the opening <think> into the
         PROMPT via their chat template, so generation starts *inside* the
         block and the raw output contains only the closing tag.  The old
         "<think>.*?</think>" regex never matched this, so the entire
         chain-of-thought was returned as the "correction" and silently
         pasted into the user's document.
      3. Open, no close "<think>...(truncated)"           -> ""
         max_tokens was hit before the model finished reasoning; no answer
         survived, so return empty and let the safety layer fall back to the
         original text rather than leaking partial reasoning.
    """
    if not text:
        return text

    # Channel/thought-style reasoning that opened but never closed (no
    # <thought_end>, no closing </think>, no 'final' channel) means the model
    # ran out of budget or looped while still reasoning — no answer survived.
    # Return "" so the caller falls back to the original text instead of pasting
    # raw chain-of-thought. The <|message|> guard preserves well-formed harmony
    # output (handled by _strip_channels below), which always carries a message
    # token even when its answer rides a non-standard channel name.
    if (_THOUGHT_OPEN_RE.search(text)
            and not _THOUGHT_END_RE.search(text)
            and not _THINK_CLOSE_RE.search(text)
            and not _FINAL_HDR_RE.search(text)
            and not re.search(r"<\|?message\|?>", text, re.IGNORECASE)):
        return ""

    # Some templates close reasoning with a literal "<thought_end>" marker rather
    # than </think>; the answer is whatever follows the last one. Cut here first
    # so the channel/think handling below only sees the answer portion.
    thought_ends = list(_THOUGHT_END_RE.finditer(text))
    if thought_ends:
        text = text[thought_ends[-1].end():]

    # Harmony / channel-format models wrap the answer in <|channel|> segments;
    # strip that scaffolding before the <think>-tag handling below.
    text = _strip_channels(text)

    # 1. Strip well-formed blocks (also covers multiple blocks).
    text = re.sub(
        r"<\s*(think|thinking|thought|reason|reasoning)\s*>.*?<\s*/\s*\1\s*>",
        "", text, flags=re.DOTALL | re.IGNORECASE,
    )
    # 2. Close-only remnant: keep only what follows the LAST closing tag.
    closes = list(_THINK_CLOSE_RE.finditer(text))
    if closes:
        text = text[closes[-1].end():]
    # 3. Unterminated opening tag -> truncated reasoning, nothing to keep.
    if _THINK_OPEN_RE.search(text):
        return ""

    return text.strip()


def _maybe_flag_reasoning_model(raw: str) -> None:
    """Set _reasoning_model_detected=True the first time a reasoning block is seen.

    Called immediately after every raw LLM completion so the flag is set before
    the *next* call computes its token budget — meaning the very first response
    from a reasoning model still runs short, but every subsequent call gets the
    correct budget.  In practice this is fine: the first trigger is almost always
    a warm-up or short sentence whose truncated <think> output still resolves to
    the right correction; the improvement is visible from the second sentence on.
    """
    global _reasoning_model_detected
    if not _reasoning_model_detected and (
        _THINK_OPEN_RE.search(raw) or _THINK_CLOSE_RE.search(raw)
        or _THOUGHT_END_RE.search(raw)
    ):
        # A bare closing tag (no opening one) is the DeepSeek-R1 signature:
        # the open tag lives in the prompt, so we must detect on </think> too,
        # otherwise the thinking budget never activates for these models.
        # <thought_end> covers gpt-oss-style templates that use that delimiter.
        _reasoning_model_detected = True


def _streaming_answer(raw: str) -> tuple:
    """Given the raw stream accumulated so far, return (visible_text, thinking).

    Used to drive a live UI: while the model is still inside its reasoning
    block, `thinking` is True and `visible_text` is "" (so the UI shows a
    "Thinking…" placeholder); once the answer begins to emerge, `thinking`
    flips to False and `visible_text` holds the cleaned answer-so-far.

    Reuses the same delimiters as _strip_reasoning, evaluated incrementally:
      • answer is whatever follows the LAST reasoning boundary
        (<thought_end>, </think>, or a 'final' channel header);
      • before any boundary, if a reasoning opener is actually present we are
        still thinking;
      • a model with no reasoning markers at all streams straight through.
    """
    if not raw:
        return "", False

    boundary = None
    for m in _THOUGHT_END_RE.finditer(raw):
        boundary = m.end()
    for m in _THINK_CLOSE_RE.finditer(raw):
        boundary = max(boundary or 0, m.end())
    finals = list(_FINAL_HDR_RE.finditer(raw))
    if finals:
        boundary = max(boundary or 0, finals[-1].end())

    if boundary is not None:
        answer = _strip_reasoning(raw[boundary:])
        return answer, False

    # No boundary yet. If we can SEE a reasoning opener, we're mid-reasoning.
    if (_THINK_OPEN_RE.search(raw) or _THOUGHT_OPEN_RE.search(raw)
            or _CHANNEL_HDR_RE.search(raw)):
        return "", True

    # No markers at all. Only assume "still reasoning" when a reasoning model was
    # detected AND thinking is actually active — i.e. a model (like DeepSeek-R1)
    # whose opening tag lives in the prompt, so its raw output starts mid-thought
    # with no visible opener. When thinking is disabled (the default for the
    # llama-server path) or on cloud backends, the output IS the answer and must
    # stream live — otherwise the chat just shows "Thinking…" the whole time.
    if _reasoning_model_detected and not _server_disable_thinking:
        return "", True

    # Plain answer (or thinking-disabled output): show it as it streams.
    return raw.strip(), False


# Tokens reserved for <think> blocks when a reasoning model is auto-detected.
# correction task uses 200-600 thinking tokens on small models; larger reasoning
# models (14B+) routinely use 1000-3000+ tokens. 2048 is a safer default.
# Users can override this via cfg["thinking_budget"].
_THINKING_BUDGET_AUTO = 2048


def _get_thinking_budget(cfg: dict | None = None) -> int:
    """Return the number of tokens to reserve for <think> reasoning output.

    Priority:
      1. cfg["thinking_budget"] if > 0  — explicit manual override.
      2. _THINKING_BUDGET_AUTO           — when a reasoning model has been
                                           auto-detected (first <think> seen).
      3. 0                               — non-reasoning model; no budget needed.
    """
    if cfg is not None:
        manual = int(cfg.get("thinking_budget", 0))
        if manual > 0:
            return manual
    return _THINKING_BUDGET_AUTO if _reasoning_model_detected else 0


_BHS_ALLOWED = set("šŠčČćĆžŽđĐ")

# Standard Serbian Cyrillic → Gaj's Latin transliteration. Small models
# sometimes emit Cyrillic homoglyphs (e.g. Cyrillic 'о' U+043E that looks
# identical to Latin 'o') in otherwise-Latin BHS text. We transliterate any
# stray Cyrillic back to Latin so the output is consistently Latin script.
_CYRILLIC_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ђ": "đ", "е": "e",
    "ж": "ž", "з": "z", "и": "i", "ј": "j", "к": "k", "л": "l", "љ": "lj",
    "м": "m", "н": "n", "њ": "nj", "о": "o", "п": "p", "р": "r", "с": "s",
    "т": "t", "ћ": "ć", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "č",
    "џ": "dž", "ш": "š",
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Ђ": "Đ", "Е": "E",
    "Ж": "Ž", "З": "Z", "И": "I", "Ј": "J", "К": "K", "Л": "L", "Љ": "Lj",
    "М": "M", "Н": "N", "Њ": "Nj", "О": "O", "П": "P", "Р": "R", "С": "S",
    "Т": "T", "Ћ": "Ć", "У": "U", "Ф": "F", "Х": "H", "Ц": "C", "Ч": "Č",
    "Џ": "Dž", "Ш": "Š",
}


def _sanitize_bhs_diacritics(text: str) -> str:
    """Normalise model output to clean BHS Latin script.

    BHS only uses š/č/ć/ž/đ. Models sometimes hallucinate other accented
    characters (ǒ, ù, ó, …) or emit Cyrillic homoglyphs. This:
      1. transliterates any stray Cyrillic letter to its Latin equivalent, and
      2. strips combining marks from any non-BHS accented character, keeping
         only the base letter.
    """
    result = []
    for ch in text:
        if ch in _CYRILLIC_TO_LATIN:
            result.append(_CYRILLIC_TO_LATIN[ch])
        elif ch in _BHS_ALLOWED or ord(ch) < 128:
            result.append(ch)
        else:
            # Decompose to base + combining marks, keep only the base
            decomposed = unicodedata.normalize("NFD", ch)
            base = decomposed[0]
            recomposed = unicodedata.normalize("NFC", base)
            result.append(recomposed)
    return "".join(result)


# Common BHS words that lose their diacritics when typed on a plain keyboard.
# Each key (de-diacriticised form) maps unambiguously to one correct spelling —
# i.e. the key is NOT itself a valid different BHS word. Deliberately excludes
# traps like 'posao' (job), 'zao' (evil), 'sto' (hundred/table) where the
# bare-Latin form is a real word. This catches the high-frequency cases small
# models miss; it is not a full dictionary.
_BHS_DIACRITIC_WORDS = {
    # verbs: ići / doći / naći / proći / izaći (l-participles)
    "isao": "išao", "dosao": "došao", "nasao": "našao", "prosao": "prošao",
    "otisao": "otišao", "izasao": "izašao", "usao": "ušao",
    "dosli": "došli", "nasli": "našli", "prosli": "prošli", "otisli": "otišli",
    "izasli": "izašli", "dosla": "došla", "nasla": "našla", "prosla": "prošla",
    # moći / htjeti / željeti / kazati present tense
    "moze": "može", "mozes": "možeš", "mozemo": "možemo", "mozete": "možete",
    "hoce": "hoće", "hoces": "hoćeš", "hocemo": "hoćemo", "hocete": "hoćete",
    "nece": "neće", "neces": "nećeš", "necemo": "nećemo", "necete": "nećete",
    "zelim": "želim", "zelis": "želiš", "zeli": "želi", "zelimo": "želimo",
    "kaze": "kaže", "kazes": "kažeš", "kazem": "kažem",
    "ides": "ideš", "imas": "imaš", "znas": "znaš", "gledas": "gledaš",
    "radis": "radiš", "vidis": "vidiš", "jedes": "jedeš",
    # common nouns / adverbs / interjections
    "cao": "ćao", "sta": "šta", "nesto": "nešto", "nista": "ništa",
    "svasta": "svašta", "vise": "više", "jos": "još", "vec": "već",
    "covek": "čovek", "covjek": "čovjek", "zena": "žena", "zene": "žene",
    "muz": "muž", "noc": "noć", "noci": "noći", "moc": "moć",
    "pomoc": "pomoć", "caj": "čaj", "cesto": "često", "veceras": "večeras",
    "vecera": "večera",
}

_BHS_WORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in sorted(_BHS_DIACRITIC_WORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _match_case(original: str, replacement: str) -> str:
    """Apply the casing of `original` to `replacement`."""
    if original.isupper():
        return replacement.upper()
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _restore_common_bhs(text: str) -> str:
    """Restore diacritics on common BHS words the model left bare.

    Whole-word, case-preserving. Only touches words in the curated
    unambiguous list, so correctly-spelled text passes through untouched.
    """
    def repl(m: "re.Match") -> str:
        word = m.group(0)
        corrected = _BHS_DIACRITIC_WORDS[word.lower()]
        return _match_case(word, corrected)
    return _BHS_WORD_RE.sub(repl, text)


def _capitalize_sentences(text: str) -> str:
    """Capitalize the first letter of the text and of each sentence.

    A sentence boundary is sentence-ending punctuation (. ! ?) followed by
    whitespace. Only the first alphabetic character after the boundary is
    upper-cased; everything else is left untouched so mid-sentence words and
    intentional casing are preserved. Unicode-aware, so BHS letters
    (š/č/ć/ž/đ → Š/Č/Ć/Ž/Đ) capitalize correctly.
    """
    chars = list(text)
    n = len(chars)
    capitalize_next = True
    for i, ch in enumerate(chars):
        if capitalize_next:
            if ch.isspace():
                continue
            if ch.isalpha():
                # Extract word to check for camelCase (brand names / code symbols)
                j = i
                while j < n and (chars[j].isalnum() or chars[j] in "'_-"):
                    j += 1
                word = "".join(chars[i:j])
                is_camel = any(c1.islower() and c2.isupper() for c1, c2 in zip(word, word[1:]))
                if not is_camel:
                    chars[i] = ch.upper()
            capitalize_next = False
        elif ch in ".!?":
            # Only a real sentence boundary if the mark is followed by whitespace
            # or ends the text. This prevents capitalizing the character after a
            # period inside URLs/emails/decimals/abbreviations — e.g.
            # "example.com" must NOT become "example.Com", and "3.14" stays "3.14".
            nxt = chars[i + 1] if i + 1 < n else None
            if nxt is None or nxt.isspace():
                capitalize_next = True
    return "".join(chars)


def spell_check_sentence(text: str, fmt: str, working_language: str = "Auto", deterministic: bool = False, diacritics_only: bool = False) -> str:
    """
    Run the loaded LLM on `text` and return the grammar/spelling-corrected result.
    Returns `text` unchanged if no model is loaded.
    deterministic=False uses temperature-based sampling (faster, slightly varied).
    deterministic=True forces temperature=0.0 (greedy decoding — always picks the
    single most probable token, so output is fully reproducible and consistent).

    When working_language is "Auto", the prompt instructs the model to fix grammar
    in whatever language the input is in.  When a specific language is set, the
    prompt is restructured so translation is the *primary* instruction and grammar
    correction is secondary — this prevents small models from latching onto the
    "do not rewrite" clause and ignoring the language directive.
    """
    with _llm_lock:
        if _llm is None:
            diagnostics.set_status(backend="None")
            diagnostics.record_throttled(
                "backend", "warning",
                "No AI backend is connected; correction skipped.",
                cooldown_sec=20.0,
            )
            return text

        lang = (working_language or "Auto").strip()

        if diacritics_only:
            sys_msg = (
                "You are a diacritics restoration tool for BHS (Bosnian/Croatian/Serbian). "
                "Your ONLY task is to restore missing diacritical characters: "
                "replace 'c' with 'č' or 'ć', 's' with 'š', 'z' with 'ž', 'd' with 'đ' — "
                "but ONLY where the correct diacritic is unambiguous given the word's meaning and context. "
                "If a word is ambiguous, leave it unchanged. "
                "Do NOT fix grammar, change structure, rephrase, alter capitalization, punctuation, spacing, or word order. "
                "Do NOT add or remove any words. "
                "Return ONLY the text with diacritics restored. No explanation. No extra lines. "
                "If no diacritics need restoring, return the text exactly as given."
            )
            user_msg = f"Restore diacritics only:\n{text}"
            prompt, stop = _build_prompt(fmt, sys_msg, user_msg)
            thinking = _get_thinking_budget()
            max_tok = max(32, _word_count(text) * 2 + 10) + thinking
            out = _llm(prompt, max_tokens=max_tok, temperature=0.0, repeat_penalty=1.3, stop=stop, echo=False)
            raw = out["choices"][0]["text"]
            _maybe_flag_reasoning_model(raw)
            raw = _strip_reasoning(raw)
            return raw.strip()

        if lang == "Auto":
            sys_msg = (
                "You are a minimal spell and grammar corrector. "
                "Your ONLY job is to fix clear errors: misspelled words, wrong capitalization, "
                "missing or wrong punctuation, and broken spacing. "
                "If the input appears to be Bosnian, Croatian, or Serbian written without diacritics "
                "(e.g. 'c' instead of 'č'/'ć', 's' instead of 'š', 'z' instead of 'ž', 'd' instead of 'đ'), "
                "restore the correct diacritics throughout the text. "
                "IMPORTANT: Preserve all brand names, camelCase words, code variables, URLs, paths, and email addresses exactly as written. Do NOT change their casing or spelling. "
                "Do NOT substitute any correctly spelled word with a synonym — even if you think "
                "a different word sounds better, leave it exactly as written. "
                "Do NOT restructure, rephrase, or reorder any part of the sentence. "
                "Do NOT add words, remove words, or change sentence structure. "
                "Make the smallest possible change to fix each error and leave everything else untouched. "
                "Keep the output in the same language as the input text. "
                "Preserve existing commas unless they are clearly wrong. "
                "Ensure there is one space after sentence-ending punctuation before the next sentence. "
                "Return ONLY the corrected text. No explanation. No extra lines. "
                "If there are no errors, return the text exactly as given."
            )
        elif lang in _SLAVIC_LANGS:
            # Correction-first prompt for South Slavic languages (BHS).
            # The user is typing IN this language already, so a translation-first
            # prompt ("translate BHS → BHS") confuses small models and they
            # return the text unchanged, leaving missing diacritics uncorrected.
            sys_msg = (
                "You are a minimal spell and grammar corrector for BHS (Bosnian/Croatian/Serbian). "
                "Your ONLY job is to fix clear errors: misspelled words, wrong capitalization, "
                "missing or wrong punctuation, and broken spacing. "
                "The input may be written without some or all diacritics "
                "(e.g. 'c' instead of 'č'/'ć', 's' instead of 'š', 'z' instead of 'ž', 'd' instead of 'đ'). "
                "Restore the correct BHS diacritics where they are missing. "
                "IMPORTANT: The ONLY diacritical characters used in BHS are: š, č, ć, ž, đ "
                "(and their uppercase forms Š, Č, Ć, Ž, Đ). "
                "Do NOT introduce any other Unicode or accented characters (e.g. ǒ, ù, ó, à, â, etc.). "
                "Output BHS in LATIN script only — never use Cyrillic letters. "
                "Do NOT add diacritics to words that are already spelled correctly without them. "
                "IMPORTANT: Preserve all brand names, camelCase words, code variables, URLs, paths, and email addresses exactly as written. Do NOT change their casing or spelling. "
                "Common BHS words that need diacritics: 'cao'->'ćao', 'ces'->'ćeš', 'hoces'->'hoćeš', "
                "'neces'->'nećeš', 'mozes'->'možeš', 'ides'->'ideš', 'jesi'->'jesi' (no change), "
                "'sta'->'šta', 'sto'->'što', 'vise'->'više', 'nase'->'naše', 'vase'->'vaše', "
                "'isao'->'išao', 'dosao'->'došao', 'nasao'->'našao', 'prosao'->'prošao', 'cesto'->'često'. "
                "Do NOT substitute any correctly spelled word with a synonym — even if you think "
                "a different word sounds better, leave it exactly as written. "
                "Do NOT restructure, rephrase, or reorder any part of the sentence. "
                "Do NOT add words, remove words, or change sentence structure. "
                "Make the smallest possible change to fix each error and leave everything else untouched. "
                "Preserve existing commas unless they are clearly wrong. "
                "Ensure there is one space after sentence-ending punctuation before the next sentence. "
                "Return ONLY the corrected text. No explanation. No extra lines. "
                "If there are no errors, return the text exactly as given."
            )
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

        if lang == "Auto" or lang in _SLAVIC_LANGS:
            user_msg = (
                f"Fix only clear spelling, grammar, punctuation, and diacritic errors. "
                f"Do not change any correctly spelled word, do not use synonyms, "
                f"do not rephrase:\n{text}"
            )
        else:
            user_msg = (
                f"Translate naturally into {lang} — not word-for-word, but how a native speaker would say it. "
                f"The input may be written without diacritics (š->s, č/ć->c, ž->z, đ->d) — interpret it correctly. "
                f"Fix any grammar, spelling, and punctuation in the result:\n{text}"
            )
        prompt, stop = _build_prompt(fmt, sys_msg, user_msg)
        # Budget: for pure grammar correction the output length ≈ input length,
        # so we cap dynamically.  For translation the target language may be more
        # verbose (e.g. English articles/prepositions vs Bosnian), so add extra slack.
        # Reasoning models (DeepSeek-R1, QwQ, etc.) emit a <think> block before
        # their answer — _get_thinking_budget() adds headroom for that so the
        # actual answer is never crowded out.
        slack = 40 if (lang != "Auto" and lang not in _SLAVIC_LANGS) else 20
        thinking = _get_thinking_budget()
        max_tok = max(32, _word_count(text) * 3 + slack) + thinking
        if deterministic:
            out = _llm(prompt, max_tokens=max_tok, temperature=0.0, repeat_penalty=1.3, stop=stop, echo=False)
        else:
            out = _llm(prompt, max_tokens=max_tok, temperature=0.05, repeat_penalty=1.3, stop=stop, echo=False)
        raw = out["choices"][0]["text"]
        _maybe_flag_reasoning_model(raw)
        raw = _strip_reasoning(raw)
        result = raw.strip()
        if lang in _SLAVIC_LANGS:
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

    The function is called inside _llm_lock in both rephrase_sentence and
    rephrase_sentence_stream, so _llm is guaranteed to be non-None here.
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
    try:
        n_ctx = _llm.context_params.n_ctx
        thinking = _get_thinking_budget()
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
    print(f"[SpellDaemon] rephrase budget: input_words={input_words}, "
          f"max_tok={max_tok}, thinking={thinking}, total={total}", flush=True)
    return total


def rephrase_sentence_stream(text: str, fmt: str, mode: str, working_language: str = "Auto", deterministic: bool = False, creativity: float = 0.35):
    """
    Run the loaded LLM on `text` and yield rephrased result as raw text chunks.
    The caller is still responsible for final cleanup/safety once all chunks
    have been collected. Falls back to yielding `text` unchanged if no model
    is loaded.

    _llm_lock is held for the entire call including the streaming loop.
    llama.cpp's inference state is not thread-safe: releasing the lock before
    iteration is complete would allow spell_check_sentence (or a concurrent
    chat stream) to call _llm(...) at the same time, corrupting model state.

    Early exit: each iteration checks _stream_cancel. When close_with() inside
    _Toast._show sets that event the generator returns immediately, releasing
    _llm_lock without waiting for the model to finish generating.  The pipelines
    call _stream_cancel.clear() before starting a new stream so a stale cancel
    from a previous toast never suppresses a fresh generation.
    """
    with _llm_lock:
        if _llm is None:
            diagnostics.set_status(backend="None")
            diagnostics.record_throttled(
                "backend", "warning",
                "No AI backend is connected; rephrase skipped.",
                cooldown_sec=20.0,
            )
            yield text
            return

        base_msg = _REPHRASE_PROMPTS.get(mode, _REPHRASE_PROMPTS["Diacritics Only"])
        lang = (working_language or "Auto").strip()
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

        stream = _llm(
            prompt,
            max_tokens=max_tok,
            temperature=temp,
            repeat_penalty=1.3,
            stop=stop,
            echo=False,
            stream=True,
        )
        _first_chunk = True
        for chunk in stream:
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


def chat_with_text_stream(text: str, user_instruction: str, fmt: str):
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

    _llm_lock is held for the entire call.  See rephrase_sentence_stream for
    the rationale.  The _stream_cancel event lets the _Toast close_with()
    callback stop an in-progress stream the moment the user dismisses the
    toast, so the lock is released promptly rather than after the model
    finishes generating its full response.
    """
    with _llm_lock:
        if _llm is None:
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
            n_ctx = _llm.context_params.n_ctx
            prompt_overhead = 200 + int(input_words * 1.3)
            max_tok = max(64, min(max_tok, n_ctx - prompt_overhead))
        except Exception:
            max_tok = min(max_tok, 2048)

        stream = _llm(
            prompt,
            max_tokens=max_tok,
            temperature=0.35,
            repeat_penalty=1.3,
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


def free_chat_stream(history: list, fmt: str, cfg: dict | None = None):
    """
    Free-form multi-turn chat with the loaded LLM.

    `history` is a list of {"role": "user"|"assistant", "content": "..."} dicts
    representing the full conversation so far.  The last entry must be a user
    message; this function streams the assistant's next reply.

    Uses _build_chat_prompt() so all supported model families work correctly.

    Output token budget is clamped to available n_ctx space so long conversations
    don't overflow the context window.  Falls back to an empty stream if no model
    is loaded.

    _llm_lock is held for the entire streaming call — same rationale as
    rephrase_sentence_stream.  The _stream_cancel event lets the chat window
    stop an in-progress stream the moment the user closes it.
    """
    global _llm, _llm_path, _llm_backend
    with _llm_lock:
        if _llm is None:
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

        # Token budget: generous for chat; clamp to available context window.
        total_input_words = sum(_word_count(t["content"]) for t in history)
        max_tok = max(256, min(1024, total_input_words * 2))
        try:
            n_ctx = _llm.context_params.n_ctx
            prompt_overhead = 200 + int(total_input_words * 1.5)
            if prompt_overhead >= n_ctx:
                # The accumulated conversation is already too large to fit in
                # the KV cache.  Passing the prompt to llama.cpp would raise an
                # unrecoverable exception (caught silently downstream), leaving
                # the chat window frozen with no feedback.  Surface a clear
                # message instead so the user knows what happened and can act.
                yield (
                    "\n\n[Conversation too long for the current context window "
                    f"({n_ctx} tokens).  Please start a new chat or load a "
                    "model with a larger context window.]\n"
                )
                return
            max_tok = max(64, min(max_tok, n_ctx - prompt_overhead))
        except Exception:
            max_tok = min(max_tok, 2048)

        if isinstance(_llm, _CloudClient):
            messages = _chat_history_to_messages(sys_msg, history)
            current_client = _llm
            cloud_targets = [{"client": current_client}]
            for fav in _cloud_favorite_fallbacks(cfg, current_client):
                cloud_targets.append({
                    "client": _CloudClient(
                        fav["base_url"],
                        fav["api_key"],
                        fav["model"],
                        cfg_n_ctx=max(getattr(current_client, "_n_ctx", 8192), 8192),
                    ),
                    "favorite": fav,
                })

            quota_errors = []
            for idx, target in enumerate(cloud_targets):
                client = target["client"]
                if idx:
                    failed_model = quota_errors[-1].split(":", 1)[0] if quota_errors else current_client.model
                    yield (
                        f"\n\n[Cloud model '{failed_model}' hit a rate limit or quota. "
                        f"Trying favorite '{client.model}'...]\n\n"
                    )
                    _llm = client
                    _llm_path = f"{client.base_url}::{client.model}"
                    _llm_backend = f"Cloud ({client.model})"
                    if cfg is not None:
                        fav = target.get("favorite", {})
                        cfg["backend_mode"] = "cloud"
                        cfg["cloud_provider"] = fav.get("provider", cfg.get("cloud_provider", ""))
                        cfg["cloud_base_url"] = client.base_url
                        cfg["cloud_model"] = client.model
                    diagnostics.set_status(backend=_llm_backend, backend_mode="cloud")
                    diagnostics.record("backend", "info", f"Switched to fallback cloud model {client.model}.")

                try:
                    stream = client.stream_messages(
                        messages,
                        max_tokens=max_tok,
                        temperature=0.7,
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
                    return
                except RuntimeError as exc:
                    msg = str(exc)
                    if _is_cloud_quota_error(msg):
                        quota_errors.append(f"{client.model}: {msg}")
                        diagnostics.record(
                            "backend", "warning",
                            f"Cloud model {client.model} hit a quota/rate limit.",
                            msg,
                        )
                        continue
                    raise

            if quota_errors:
                raise RuntimeError(
                    "All available favorite cloud models hit a rate limit or quota. "
                    f"Tried: {'; '.join(quota_errors)}"
                )
            return
        else:
            prompt, stop = _build_chat_prompt(fmt, sys_msg, history)
            stream = _llm(
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


# --- LLM output cleanup / safety ---
def _strip_echo_prefix(original: str, corrected: str) -> str:
    """
    Some models echo the source text before the translation, producing output like:
        "Na primjer, ako pišeš something...\nFor example, if you write something..."
    This function detects that pattern and returns only the intended output part.

    Strategy: split on newlines; if ANY leading chunk is very similar to the
    original (>=65% token overlap after Unicode normalisation), discard everything
    up to and including that chunk and return the rest.  Falls back to `corrected`
    unchanged if nothing looks like an echo.
    Unicode normalisation handles diacritics: "pises" ≈ "pišeš".
    """
    import unicodedata

    def _normalise(s: str) -> str:
        """Fold diacritics so 'pises' and 'pišeš' share tokens."""
        return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode()

    lines = [l.strip() for l in corrected.split("\n") if l.strip()]
    if len(lines) < 2:
        return corrected          # single line — nothing to strip

    orig_tokens = set(re.findall(r"\w+", _normalise(original).lower()))
    if not orig_tokens:
        return corrected

    # Walk forward through lines; once we find one that doesn't look like the
    # original, everything from that line onward is the real output.
    for i, line in enumerate(lines):
        line_tokens = set(re.findall(r"\w+", _normalise(line).lower()))
        if not line_tokens:
            continue
        overlap = len(orig_tokens & line_tokens) / len(orig_tokens)
        if overlap >= 0.65:
            # This line looks like the original — skip it and return the rest.
            remainder = "\n".join(lines[i + 1:])
            if remainder:
                return remainder
            # Echo was the last line; stripping it leaves nothing useful.
            # Return an empty string so _safe_model_correction falls back to
            # the original rather than keeping the echoed source text.
            return ""

    return corrected


def _clean_llm_output(text: str) -> str:
    """Normalize harmless formatting problems in model output."""
    text = text.strip()
    text = _strip_reasoning(text).strip()
    text = text.strip("\"'")
    # Add missing spaces after punctuation without disturbing decimals like 3.14.
    text = re.sub(r"([,;:])(?=\S)", r"\1 ", text)
    text = re.sub(r"([.!?])(?=[A-Za-zÀ-ž0-9])", r"\1 ", text)
    text = re.sub(r"(?<=\d)\. (?=\d)", ".", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _punct_counts(text: str) -> dict[str, int]:
    """Count punctuation marks that are usually unsafe to silently delete."""
    # Intentionally excludes " and ' — models routinely normalise straight quotes
    # to curly quotes (or vice versa), which would cause a spurious count mismatch
    # and make _safe_model_correction discard an otherwise valid correction.
    return {ch: text.count(ch) for ch in ",;:()[]{}"}


# URLs (http/https/www) and email addresses — must be preserved verbatim.
_URLISH_RE = re.compile(
    r"(?:https?://|www\.)\S+|[^\s@]+@[^\s@]+\.[^\s@]+",
    re.IGNORECASE,
)


def _looks_structurally_bad(original: str, corrected: str, is_translation: bool = False) -> bool:
    """Return True if model output appears to have damaged punctuation/spacing."""
    if not corrected:
        return True

    # CamelCase parity — checked in both directions:
    #
    #   corr_camel - orig_camel  (new transitions introduced by the model)
    #     Catches word-gluing artifacts: "itMy", "wordAnother".  Only flags
    #     transitions that are genuinely NEW so that brand names / acronyms
    #     already present in the original (iPhone, macOS, etc.) never cause a
    #     valid correction to be discarded.
    #
    #   orig_camel - corr_camel  (existing transitions removed by the model)
    #     Catches the mirror failure: the model silently flattening a
    #     programmatic identifier to lowercase.
    #     e.g. getUserByIndx -> getuserbyindx, isReady -> isready.
    #     Both the new-camelCase and removed-camelCase checks must apply
    #     regardless of is_translation: code identifiers must be preserved
    #     verbatim in the target language as well as the source.
    orig_camel = set(re.findall(r"[a-z][A-Z]", original))
    corr_camel = set(re.findall(r"[a-z][A-Z]", corrected))
    if corr_camel - orig_camel:          # new camelCase -> word-gluing artifact
        return True
    if orig_camel - corr_camel:          # removed camelCase -> identifier flattened
        return True

    # For every camelCase word in the original text, it must appear exactly case-sensitively in the corrected text
    # (if the word's base form is still there).
    for word in original.split():
        clean_word = word.strip(".,;:!?()\"'")
        if any(c1.islower() and c2.isupper() for c1, c2 in zip(clean_word, clean_word[1:])):
            if clean_word.lower() in corrected.lower() and clean_word not in corrected:
                return True

    # Bracket-content preservation: every [...] expression that appears in the
    # original must appear verbatim in the corrected output.
    # A grammar or translation model has no business renaming index variables
    # or key literals — arr[userIndex] -> arr[user index] is destructive even
    # though the bracket count stays at 1/1 and would pass all other checks.
    # Applied regardless of is_translation for the same reason as camelCase.
    for bracket_expr in re.findall(r"\[[^\]]*\]", original):
        if bracket_expr not in corrected:
            return True

    # URL / email preservation: any link or address in the original must survive
    # verbatim.  Models sometimes "helpfully" rewrite a URL — e.g. replacing the
    # scheme (https://example.com -> <link>://example.com), shortening it, or
    # turning it into markdown.  Trailing sentence punctuation is stripped from
    # the token before the check so a model legitimately adding a period after a
    # link doesn't trip it.  Applied regardless of is_translation: a URL or email
    # is identical in every language.
    for token in _URLISH_RE.findall(original):
        token = token.rstrip(".,;:!?)\"'")
        if token and token not in corrected:
            return True

    # Punctuation parity: do not silently delete existing internal punctuation
    # during grammar correction.  Skip this check for translation — the target
    # language legitimately uses different comma/semicolon structure, so a
    # missing comma is not evidence of model damage.
    if not is_translation:
        original_counts  = _punct_counts(original)
        corrected_counts = _punct_counts(corrected)
        for ch, count in original_counts.items():
            if corrected_counts.get(ch, 0) < count:
                return True

    return False


def _similarity(a: str, b: str) -> float:
    """Return a 0-1 similarity ratio between two strings (case-folded)."""
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


# _CJK_RE and _word_count live in constants.py (shared with buffer's
# min-word gating); both are imported at the top of this module.


def _word_count_ok(original: str, corrected: str, is_translation: bool = False) -> bool:
    """Return False if the model silently dropped or added words beyond a safe margin.

    Translations legitimately change word count (articles, prepositions, idioms),
    so the check is skipped for those.  For grammar/diacritics correction the output
    should be close to the same length as the input — a difference of more than one
    word is a strong signal that the model hallucinated or truncated.

    CJK text uses character counts rather than word counts (no spaces between words).
    Common grammatical corrections in Japanese/Chinese can add 1-2 characters (e.g.
    adding a polite です ending), so a tolerance of ±2 characters is used there.
    """
    if is_translation:
        return True
    tolerance = 2 if _CJK_RE.search(original) else 1
    return abs(_word_count(original) - _word_count(corrected)) <= tolerance


# Assistant boilerplate / refusals / chat acknowledgements a model emits instead
# of correcting the text (e.g. "Please provide the text you would like me to
# correct.", "I cannot fulfil this request", "As an AI...", "Okay, let's do it!").
# These must never replace the user's text — even in translation mode, where the
# normal low-similarity floor is intentionally skipped.
_META_RESPONSE_RE = re.compile(
    # — refusals / boilerplate —
    r"(please provide|provide the (text|sentence)"
    r"|you('| woul)d? ?(like|want) me to"
    r"|the (text|sentence) you('| woul)d?"
    r"|i (cannot|can ?not|can'?t|am unable|'m unable|am not able|'m not able)"
    r"|i'?m sorry|i am sorry|as an ai|i'?d be happy to|i would be happy to"
    r"|there (is|'s) no (text|sentence|content)|no text (was )?provided"
    r"|could you (please )?(provide|clarify)|feel free to|happy to help"
    # — conversational acknowledgements (model treated input as a chat) —
    # Kept deliberately specific: phrases that are almost never a legitimate
    # translation/correction of user text, so genuine short translations
    # (e.g. "Got it", "No problem", "I'll help you") are not falsely rejected.
    r"|let'?s do it|let'?s get started|sure thing|here (you go|'?s (the|your))"
    r"|i'?ll help you with|how can i (help|assist))",
    re.IGNORECASE,
)


def _looks_like_meta_response(original: str, corrected: str) -> bool:
    """True if the model answered ABOUT the task instead of doing it.

    Only flags boilerplate the model INTRODUCED — if the same phrase is in the
    original (e.g. the user actually wrote "please provide the report"), it is
    not treated as a meta-response, avoiding false positives on genuine text.
    """
    return bool(_META_RESPONSE_RE.search(corrected)
                and not _META_RESPONSE_RE.search(original))


def _safe_model_correction(
    original: str,
    corrected: str,
    is_translation: bool = False,
) -> str:
    """Clean model output and fall back to original if it looks unsafe."""
    corrected = _clean_llm_output(corrected)
    corrected = _strip_echo_prefix(original, corrected)
    # Reject assistant boilerplate / refusals regardless of mode. This runs even
    # for translations, where the low-similarity floor below is intentionally
    # skipped and would otherwise let a meta-response through and overwrite the
    # user's text.
    if _looks_like_meta_response(original, corrected):
        return original
    if _looks_structurally_bad(original, corrected, is_translation):
        return original
    if not _word_count_ok(original, corrected, is_translation):
        return original
    # Similarity floor: if the corrected text is less than 40% similar to the
    # original, the model has likely hallucinated or rewritten the sentence
    # rather than correcting it.  Skipped for translations, where a low
    # character-level similarity between source and target is expected and normal.
    if not is_translation and _similarity(original, corrected) < 0.40:
        return original
    return corrected
