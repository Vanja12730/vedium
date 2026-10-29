# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""One-shot Claude Code and Codex CLI model support.

This module is intentionally standalone and standard-library only.  It owns
CLI detection, Codex model discovery, subprocess invocation, output parsing,
message flattening, and provenance formatting.  Host-app routing and widgets
live elsewhere.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


SUBPROCESS_TIMEOUT = 240.0
# The daemon normally runs under pythonw.exe, which has no console — so every
# console-app CLI call would otherwise flash a brand-new console window on
# screen. CREATE_NO_WINDOW gives the child an invisible console instead.
# (0 on non-Windows, where the flag does not exist and no flash occurs.)
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CLAUDE_ALIASES = ("sonnet", "opus", "haiku", "fable")
EFFORT_LEVELS = ("", "low", "medium", "high", "xhigh", "max")
_EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max", "ultra")

# agy receives the prompt as a command-line argument, and Windows caps a
# command line at 32767 characters.
_AGY_PROMPT_LIMIT = 30000
# `agy models` takes ~2.5s, which is too slow to repeat on every detection pass,
# so its answer is cached for the life of the process.
_AGY_LIST_TIMEOUT = 30.0
_agy_models_cache: list[str] | None = None


class CLIModelError(RuntimeError):
    """A user-actionable CLI detection, invocation, or parsing failure."""


def wrap_command(executable: str) -> list[str]:
    """Return an argv prefix that Windows can execute directly."""
    if executable.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", executable]
    return [executable]


def resolve_cli(name: str) -> str | None:
    """Resolve a real executable, preferring Windows-native wrappers."""
    for candidate in (f"{name}.exe", f"{name}.cmd", f"{name}.bat", name):
        path = shutil.which(candidate)
        if path:
            return path
    return None


def discover_codex_models(home: str | Path | None = None) -> list[dict]:
    """Read visible Codex models from its local cache; return [] on any drift."""
    root = Path(home) if home is not None else Path.home()
    try:
        payload = json.loads(
            (root / ".codex" / "models_cache.json").read_text(encoding="utf-8")
        )
        models = payload.get("models", [])
        if not isinstance(models, list):
            return []

        result = []
        for model in models:
            if not isinstance(model, dict) or model.get("visibility") != "list":
                continue
            slug = model.get("slug")
            if not isinstance(slug, str) or not slug.strip():
                continue
            levels = model.get("supported_reasoning_levels", [])
            efforts = [
                item.get("effort")
                for item in levels
                if isinstance(item, dict)
                and isinstance(item.get("effort"), str)
                and item.get("effort") in _EFFORT_ORDER
            ] if isinstance(levels, list) else []
            result.append({
                "slug": slug.strip(),
                "label": model.get("display_name") or slug.strip(),
                "efforts": efforts,
            })
        return result
    except (OSError, ValueError, TypeError):
        return []


def codex_default_model(home: str | Path | None = None) -> str:
    """Return the model configured for bare ``codex exec``, or an empty string."""
    root = Path(home) if home is not None else Path.home()
    try:
        text = (root / ".codex" / "config.toml").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r'^model\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def discover_antigravity_models(path: str, *, refresh: bool = False) -> list[str]:
    """Ask `agy models` what it can currently run; return [] on any failure.

    Nothing is hard-coded: whatever the installed CLI prints is what the app
    offers, so an agy update brings its new models along without an app change.
    agy 1.1.11 prints a stable slug plus a display label; 1.1.5 printed only
    the slug, and older builds printed only display labels. All forms are kept
    intact here and normalized by :func:`antigravity_model_entries`.

    The answer is cached for the life of the process.  Failures are not cached,
    so a CLI that is briefly unavailable is retried on the next pass.
    """
    global _agy_models_cache
    if _agy_models_cache is not None and not refresh:
        return list(_agy_models_cache)
    try:
        result = subprocess.run(
            wrap_command(path) + ["models"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_AGY_LIST_TIMEOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    names = [
        line.strip() for line in str(result.stdout or "").splitlines() if line.strip()
    ]
    _agy_models_cache = names
    return list(names)


def antigravity_model_entries(models: list[str] | tuple[str, ...] = ()) -> list[dict]:
    """Return the Antigravity default entry plus one entry per listed model.

    Newer agy versions print ``slug<TAB>display name`` while older versions
    printed either a slug or a display name on its own. Keep the stable first
    column for ids and ``--model``, but retain the second column for the UI.
    A future whitespace-aligned two-column format is handled the same way.
    """
    entries = [{
        "id": "agy",
        "label": "Antigravity CLI default",
        "efforts": [],
        "agy_model": "",
    }]
    seen_ids = {"agy"}
    for listed_name in models:
        raw = str(listed_name or "").strip()
        if not raw:
            continue
        columns = re.split(r"(?:\t+| {2,})", raw, maxsplit=1)
        if len(columns) == 2:
            if (not re.fullmatch(r"[a-z0-9][a-z0-9._:/-]*", columns[0].strip())
                    or not columns[1].strip()):
                continue
            name, display_name = (column.strip() for column in columns)
        else:
            name = display_name = raw
        slug = re.sub(r"[^a-z0-9.]+", "-", name.lower()).strip("-")
        model_id = f"agy-{slug}"
        if not slug or model_id in seen_ids:
            continue
        seen_ids.add(model_id)
        entries.append({
            "id": model_id,
            "label": f"{display_name} (Antigravity CLI)",
            "efforts": [],
            "agy_model": name,
        })
    return entries


def detect_clis(home: str | Path | None = None, *, refresh: bool = False) -> dict:
    """Return fresh detection and model metadata for the supported CLIs.

    ``refresh`` re-runs the cached `agy models` lookup; callers on the UI thread
    should run this in a worker thread because that lookup spawns a subprocess.
    """
    claude_path = resolve_cli("claude")
    codex_path = resolve_cli("codex")
    agy_path = resolve_cli("agy")
    codex_models = discover_codex_models(home) if codex_path else []
    default = codex_default_model(home) if codex_path else ""
    agy_models = (
        discover_antigravity_models(agy_path, refresh=refresh) if agy_path else []
    )
    return {
        "claude": {
            "path": claude_path,
            "models": [
                {
                    "id": f"claude-{alias}",
                    "label": f"Claude Code - {alias.title()}",
                    "efforts": list(EFFORT_LEVELS[1:]),
                }
                for alias in CLAUDE_ALIASES
            ] if claude_path else [],
        },
        "codex": {
            "path": codex_path,
            "default_model": default,
            "models": ([{
                "id": "codex",
                "label": f"Codex CLI default ({default})" if default else "Codex CLI default",
                "efforts": next((
                    list(model["efforts"])
                    for model in codex_models if model["slug"] == default
                ), []),
            }] + [
                {
                    "id": f"codex-{model['slug']}",
                    "label": f"{model['label']} (Codex CLI)",
                    "efforts": list(model["efforts"]),
                }
                for model in codex_models
            ]) if codex_path else [],
        },
        "antigravity": {
            "path": agy_path,
            "models": antigravity_model_entries(agy_models) if agy_path else [],
        },
    }


def selectable_models(enabled: bool, home: str | Path | None = None) -> list[dict]:
    """Return detected app-model entries only while the feature is enabled."""
    if not enabled:
        return []
    detected = detect_clis(home)
    return (
        detected["claude"]["models"]
        + detected["codex"]["models"]
        + detected["antigravity"]["models"]
    )


def flatten_messages(messages: list[dict]) -> tuple[str, str]:
    """Convert OpenAI-style messages to a system prompt and one CLI prompt."""
    system = "\n\n".join(
        str(message.get("content", ""))
        for message in messages
        if message.get("role") == "system"
        and str(message.get("content", "")).strip()
    )
    turns = [
        message for message in messages
        if message.get("role") in ("user", "assistant")
    ]
    if len(turns) == 1:
        return system, str(turns[0].get("content", ""))

    blocks = [
        f"[{'User' if turn.get('role') == 'user' else 'Assistant'}]\n"
        f"{turn.get('content', '')}"
        for turn in turns
    ]
    blocks.append(
        "[Assistant]\n(Reply as the assistant to the last user message. "
        "Output only the reply itself.)"
    )
    return system, "\n\n".join(blocks)


def served_model(usage: object, requested_alias: str) -> str:
    """Choose Claude's answering model, ignoring low-output helper models."""
    if not isinstance(usage, dict) or not usage:
        return ""
    match = next((model for model in usage if requested_alias in model), None)
    if match:
        return match

    def output_tokens(model: str) -> int:
        entry = usage.get(model)
        if not isinstance(entry, dict):
            return 0
        try:
            return int(entry.get("outputTokens", 0))
        except (TypeError, ValueError):
            return 0

    return max(usage, key=output_tokens)


def clamp_codex_effort(effort: str, supported: list[str] | tuple[str, ...] | None) -> str:
    """Clamp an effort to the selected Codex model's known capabilities."""
    if effort not in EFFORT_LEVELS:
        raise CLIModelError(f"Unknown reasoning effort {effort!r}.")
    if not effort:
        return ""

    allowed = [level for level in (supported or ()) if level in _EFFORT_ORDER]
    if effort in allowed:
        return effort
    if not allowed:
        return effort if effort in ("low", "medium", "high") else "high"

    requested = _EFFORT_ORDER.index(effort)
    lower = [level for level in allowed if _EFFORT_ORDER.index(level) <= requested]
    return max(lower, key=_EFFORT_ORDER.index) if lower else min(
        allowed, key=_EFFORT_ORDER.index
    )


def _check_effort(effort: str) -> None:
    if effort not in EFFORT_LEVELS:
        raise CLIModelError(f"Unknown reasoning effort {effort!r}.")


def _error_output(result: subprocess.CompletedProcess) -> str:
    return str(result.stderr or result.stdout or "").strip()[:300]


def run_claude(
    path: str,
    alias: str,
    system: str,
    prompt: str,
    cwd: str,
    effort: str = "",
) -> tuple[str, str]:
    """Run Claude Code once and return ``(reply, exact_served_model)``."""
    if alias not in CLAUDE_ALIASES:
        raise CLIModelError(f"Unknown Claude alias {alias!r}.")
    _check_effort(effort)

    command = wrap_command(path) + [
        "-p", "--output-format", "json",
        "--tools", "",
        "--no-session-persistence",
        "--model", alias,
    ]
    if effort:
        command += ["--effort", effort]
    if system.strip():
        command += ["--system-prompt", system]

    try:
        result = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            timeout=SUBPROCESS_TIMEOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise CLIModelError("The CLI took longer than 240s and was stopped.") from exc

    if result.returncode != 0:
        raise CLIModelError(
            f"claude exited with {result.returncode}: {_error_output(result)}"
        )
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
    except (TypeError, ValueError) as exc:
        raise CLIModelError(
            f"Claude returned unparseable output: {str(result.stdout).strip()[:300]}"
        ) from exc

    text = str(payload.get("result") or "").strip()
    if payload.get("is_error") or not text:
        raise CLIModelError(
            f"Claude reported an error: {text[:300] or 'empty reply'}"
        )
    return text, served_model(payload.get("modelUsage"), alias)


def run_codex(
    path: str,
    model_slug: str,
    system: str,
    prompt: str,
    cwd: str,
    effort: str = "",
    supported_efforts: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Run Codex once and return only its final agent message."""
    if system.strip():
        prompt = f"[Instructions]\n{system}\n\n{prompt}"
    effort = clamp_codex_effort(effort, supported_efforts)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8", dir=cwd
    ) as handle:
        output_path = Path(handle.name)

    try:
        command = wrap_command(path) + ["exec"]
        if model_slug:
            command += ["-m", model_slug]
        if effort:
            command += ["-c", f'model_reasoning_effort="{effort}"']
        command += [
            "--sandbox", "read-only",
            "--skip-git-repo-check",
            "--color", "never",
            "--cd", cwd,
            "-o", str(output_path),
            "-",
        ]

        try:
            result = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=cwd,
                timeout=SUBPROCESS_TIMEOUT,
                creationflags=_CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise CLIModelError("The CLI took longer than 240s and was stopped.") from exc

        try:
            text = output_path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if result.returncode != 0 or not text:
            raise CLIModelError(
                f"codex exited with {result.returncode}: {_error_output(result)}"
            )
        return text
    finally:
        output_path.unlink(missing_ok=True)


def run_agy(
    path: str,
    model_label: str,
    system: str,
    prompt: str,
    cwd: str,
) -> tuple[str, str]:
    """Run Antigravity CLI once and return ``(reply, confirmed_model_label)``.

    ``model_label`` is the exact name ``agy models`` printed (empty for the
    user's default model). agy has no reasoning-effort flag; the level is part
    of the model name itself.
    """
    # agy only takes the prompt as an argument. Through cmd.exe, a quote in
    # selected text would end the argument and let `&` run commands, and
    # cmd.exe has no complete escaping, so a .cmd/.bat wrapper is refused.
    if path.lower().endswith((".cmd", ".bat")):
        raise CLIModelError(
            "Vedium found the Antigravity CLI only as a .cmd or .bat wrapper, "
            "which cannot safely receive selected text. Install the native "
            "agy.exe and put it on PATH."
        )
    if system.strip():
        prompt = f"[Instructions]\n{system}\n\n{prompt}"
    if len(prompt) > _AGY_PROMPT_LIMIT:
        raise CLIModelError(
            "This request is too long for the Antigravity CLI "
            f"(over {_AGY_PROMPT_LIMIT} characters). Shorten the text or "
            "start a new chat."
        )

    log_path = Path(cwd) / "agy_run.log"
    command = wrap_command(path) + [
        "-p", prompt,
        "--mode", "plan",
        "--print-timeout", "240s",
        "--log-file", str(log_path),
    ]
    if model_label:
        command += ["--model", model_label]

    try:
        result = subprocess.run(
            command,
            input="",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            timeout=SUBPROCESS_TIMEOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise CLIModelError("The CLI took longer than 240s and was stopped.") from exc

    text = str(result.stdout or "").strip()
    if result.returncode != 0 or not text:
        raise CLIModelError(
            f"agy exited with {result.returncode}: {_error_output(result)}"
        )

    try:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log_text = ""
    # agy 1.1.5 rejects an unknown --model outright (exit 1 + stderr), which the
    # returncode check above already catches. Older builds silently substituted
    # their default and only admitted it in the run log, so that check stays.
    if model_label and "Failed to resolve model flag" in log_text:
        raise CLIModelError(
            f"Antigravity CLI did not run {model_label!r}. Its model list has "
            "probably changed; click Refresh in Settings and reselect the model."
        )
    # The log names the resolved model by its display label even when the run
    # was requested by slug, so it is the better provenance of the two.
    served = re.findall(r'label="([^"]+)"', log_text)
    return text, served[-1] if served else model_label


def provenance_footer(provenance: dict) -> str:
    """Format provenance separately so replacement callers can omit it."""
    parts = [f"via {provenance.get('runner', 'AI CLI')}"]
    if provenance.get("model"):
        parts.append(str(provenance["model"]))
    if provenance.get("effort"):
        parts.append(f"effort {provenance['effort']}")
    return f"*({' · '.join(parts)})*"


def converse(
    model: str,
    messages: list[dict],
    *,
    enabled: bool,
    effort: str = "",
    home: str | Path | None = None,
) -> tuple[str, dict]:
    """Run one enabled app-model id and return clean text plus provenance."""
    if not enabled:
        raise CLIModelError(
            "Command-line models are switched off. Enable them in settings."
        )
    _check_effort(effort)
    system, prompt = flatten_messages(messages)

    # ignore_cleanup_errors: on Windows a CLI can leave a detached child whose
    # cwd is this directory, making the rmdir fail AFTER a successful reply.
    # A stranded (tiny) temp dir is acceptable; losing the reply is not.
    with tempfile.TemporaryDirectory(
            prefix="vedium_cli_", ignore_cleanup_errors=True) as cwd:
        if model.startswith("claude-"):
            alias = model.removeprefix("claude-")
            if alias not in CLAUDE_ALIASES:
                raise CLIModelError(f"Unknown Claude alias {alias!r}.")
            path = resolve_cli("claude")
            if not path:
                raise CLIModelError("The claude CLI was not found on PATH.")
            text, served = run_claude(path, alias, system, prompt, cwd, effort)
            return text, {
                "runner": "Claude Code",
                "model": served,
                "effort": effort,
            }

        if model == "codex" or model.startswith("codex-"):
            path = resolve_cli("codex")
            if not path:
                raise CLIModelError("The codex CLI was not found on PATH.")

            discovered = discover_codex_models(home)
            default = codex_default_model(home)
            slug = "" if model == "codex" else model.removeprefix("codex-")
            if slug:
                selected = next((item for item in discovered if item["slug"] == slug), None)
                if selected is None:
                    raise CLIModelError(f"Unknown Codex model {slug!r}.")
            else:
                selected = next((item for item in discovered if item["slug"] == default), None)

            clamped_effort = clamp_codex_effort(
                effort, selected["efforts"] if selected else None
            )
            text = run_codex(
                path,
                slug,
                system,
                prompt,
                cwd,
                clamped_effort,
                selected["efforts"] if selected else None,
            )
            return text, {
                "runner": "Codex CLI",
                "model": "",
                "effort": clamped_effort,
            }

        if model == "agy" or model.startswith("agy-"):
            path = resolve_cli("agy")
            if not path:
                raise CLIModelError(
                    "The Antigravity CLI (agy) was not found on PATH."
                )
            entry = next(
                (item for item in
                 antigravity_model_entries(discover_antigravity_models(path))
                 if item["id"] == model),
                None,
            )
            if entry is None:
                raise CLIModelError(f"Unknown Antigravity model {model!r}.")
            label = entry["agy_model"]
            # agy has no effort flag; the reasoning level is baked into the
            # selected model label, so the UI effort setting is ignored here.
            text, served = run_agy(path, label, system, prompt, cwd)
            return text, {
                "runner": "Antigravity CLI",
                "model": served,
                "effort": "",
            }

    raise CLIModelError(f"Unknown command-line model {model!r}.")


class CLIClient:
    """Small host adapter that keeps CLI configuration and last provenance."""

    def __init__(self, model: str, effort: str = "", *, enabled: bool = True):
        _check_effort(effort)
        self.model = model
        self.effort = effort
        self.enabled = enabled
        self.last_provenance: dict = {}

    def complete(
        self, messages: list[dict], *, enabled: bool | None = None
    ) -> tuple[str, dict]:
        text, provenance = converse(
            self.model,
            messages,
            enabled=self.enabled if enabled is None else enabled,
            effort=self.effort,
        )
        self.last_provenance = provenance
        return text, provenance
