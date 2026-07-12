# Guide: Add "AI CLI models" (Claude Code / Codex / Antigravity) to any app

**Audience:** an AI coding assistant (of any capability level) implementing this
feature in an arbitrary application. This document is self-contained — you do
not need access to the original app it was extracted from. Follow it
step-by-step. Where the guide says MUST, do exactly that; where it says
SHOULD, deviate only if the host app makes it impossible.

**Start at Step 0 (in section 3): answer the host-app questionnaire in
writing before you produce any plan or code.** Everything else in this guide
is conditioned on those answers.

---

## 1. What you are building (mental model)

Many users have the **Claude Code** CLI (`claude`), the **Codex** CLI
(`codex`), and/or Google's **Antigravity** CLI (`agy`) installed and signed
in to a subscription. Those CLIs can be run **non-interactively**: you spawn
the CLI as a subprocess, feed it a prompt, and read back one reply. That
turns the user's existing subscription into a "model backend" for your app —
**no API keys, no per-token billing**.

(Historical note: Google's older `gemini` CLI stopped serving consumer
accounts — free, Google AI Pro, and AI Ultra — on 2026-06-18. Antigravity
CLI is its successor for those accounts; do not target `gemini` for new
work.)

The mental model is: *the app opens a terminal in the user's working folder
and types `claude "<prompt>"` for them, then copies the answer back.*

Key facts that shape the whole design:

1. **One subprocess per request.** The CLI starts, answers once, and exits.
   There is no persistent connection.
2. **Stateless.** The CLI does not remember previous requests. Multi-turn
   chat is done by replaying the transcript in the prompt (section 7).
3. **The CLI handles its own auth.** The user signed in once in their own
   terminal. Your app never sees, stores, or asks for credentials. If the CLI
   isn't signed in, the subprocess fails with an error message — surface it.
4. **These are cloud models with a local process.** The prompt leaves the
   machine through the user's account, and every request spends the user's
   subscription usage. Therefore the feature MUST be opt-in and clearly
   labelled (section 9).
5. **The working directory matters.** Run the CLI with `cwd` set to the
   folder the user is working in, so the CLI's file-reading tools see the
   same project a human in a terminal would.

## 2. Prerequisites (user-side, not app-side)

The only prerequisite is: *the CLI already works in the user's own terminal.*
That means the user has run `claude` (and logged in), `codex login`, or `agy`
(and completed the Google sign-in) at least once. Your app does not install,
configure, or authenticate anything.

## 3. Architecture — the five pieces you will write

| Piece | Responsibility |
|---|---|
| **Detector** | Find `claude` / `codex` / `agy` on PATH (with Windows quirks handled). |
| **Invoker** | Build the exact command line, spawn the subprocess, feed stdin, collect output, enforce a timeout. |
| **Parser** | Turn raw CLI output into `(reply_text, model_that_answered)`. |
| **Adapter** | Flatten your app's chat-message format into one prompt string; append a provenance footer to replies. |
| **Settings + UI** | Opt-in toggle (default OFF), effort dropdown, "detected CLIs" display. |

Keep all of it in **one module/file** in the host app. It has no dependencies
beyond the standard library (subprocess, JSON, temp files, PATH lookup).

### Step 0 — the host-app questionnaire (MUST, before any code or plan)

The reference code in this guide makes assumptions that fit some apps and not
others. Before writing your plan, **read the host app's provider/model layer
and answer these five questions IN WRITING in your plan file, each with a
`file:line` citation proving where you looked.** An answer without a citation
is a guess; do not proceed on guesses.

1. **Is the provider layer sync or async?** The `asyncio.to_thread` advice
   applies only when the calling code is `async`. If the host app's provider
   functions are plain synchronous functions (e.g. sync FastAPI/Flask routes,
   which already run on a worker threadpool), call the blocking
   `run_claude`/`run_codex` directly — wrapping them in `asyncio.to_thread`
   there is wrong (there may be no running event loop at all).
2. **How does the provider layer report errors — exceptions or return
   values?** If existing providers return something like
   `(content, usage, error)` and callers check the error slot, the CLI
   backend MUST follow the same contract: catch its own exceptions and
   return them in the error slot. Raising where callers expect tuples
   breaks every multi-model feature that tolerates individual failures.
3. **Does the app have a "user workspace folder"?** In a coding app,
   cwd = the project folder and read-only file tools add real context. In an
   app with no user-owned folder (a document reader, a note app, a chat
   client), file tools are useless or leaky — pass `--tools ""` (Claude),
   keep `--sandbox read-only` (Codex) and `--mode plan` (Antigravity), and
   set cwd to something neutral like
   the app's data directory. Never default to the app's own source folder
   with tools enabled: the model would read your app's code as "context".
4. **Can the app invoke models WITHOUT an explicit user pick?** Search the
   codebase for fan-out features (send-to-all-models, panels, debates,
   consensus voting), automatic failover chains, "pick me any model"
   helpers (referee/judge/summarizer selectors), and background features
   that call models without a user action. Every one of those can silently
   spend the user's subscription. CLI models MUST only run when the user
   explicitly selected that model for that request. Two requirements:
   - **Be exhaustive, and search the FRONTEND too:** grep for every call
     site of the model-listing and model-invoking helpers — backend AND
     client-side code — and list ALL of them in your plan with the fix for
     each; finding two of four is a failing answer. Fan-out often lives in
     the frontend as a JS loop over the model list firing one request per
     model, which no backend-only sweep will find. Watch for default
     parameter values (`pool=None` → "use all models") that reintroduce the
     full pool when a caller omits the argument.
   - **Consent semantics:** if the app's design is "selecting/favoriting a
     model enrolls it in group features" (a parliament/panel that queries
     all chosen models), enrolling a CLI model makes those features spend
     subscription usage on every use. That may be acceptable — the user did
     choose the model after an opt-in with a spend warning — but it is a
     policy decision: state it explicitly in your plan and let the human
     decide, don't decide silently.
   - **Centralize the exclusion:** implement it once, inside the shared
     model-selection/ordering helper ("skip CLI models unless the requested
     id names one exactly"), rather than filtering at each call site —
     per-site filters are whack-a-mole and the next feature will forget.
   Also flag the policy question to the human: if the app has a "default
   model" setting that background features use, decide whether a CLI model
   may be set as default. Two defensible answers — the human picks:
   (a) forbid it (safest: the default can never silently spend), or
   (b) allow it, treating "the user set this default" as consent for every
   feature that follows the default — but even then, CLI models must only
   ever LEAD a failover order (explicitly picked or default), never appear
   in the fallback tail, so a failing free model can't silently escalate
   into subscription spend. If you forbid it (a), the settings UI MUST show
   the rejection message — a silently reverting dropdown reads as a bug.
5. **Where do non-secret settings live?** Reuse the host app's existing
   settings store and persistence pattern; do not invent a new file.
6. **What are the frontend's conventions?** Before writing any UI, read the
   app's existing UI code and cite it like everything else: the framework
   (web, Tkinter, Qt, plain JS…), the module system, how an existing
   settings section is rendered, and the design tokens / CSS variable names
   **actually defined in the stylesheet — never guess token names**. Also
   enumerate every UI surface where a model can be picked (main chat
   picker, per-feature pickers, panels, per-task assignments) and state how
   a model gets into each (e.g. "models must be favorited to appear") — the
   new CLI models must be reachable from every one of them, and the manual
   test plan must exercise each.
7. **Does the app already have a multi-model abstraction — or one
   hard-wired backend?** Apps like chat clients often have a provider
   layer where adding a model means adding an entry; this guide's wiring
   advice assumes that shape. But many apps have exactly one model baked
   in (a single global client, one `backend_mode` switch, direct calls to
   one library). If that's the host app, adding CLI models means
   **introducing the backend seam first**: define the narrowest interface
   the existing call sites actually use (inspect them — often just "text
   in, text out" plus errors), make the existing backend implement it
   untouched in behavior, then add the CLI backend as the second/third
   implementation. Plan that seam as its own reviewable stage BEFORE any
   CLI code, and state in the plan which call sites you will and will NOT
   route through it (a latency-sensitive hot path that fires automatically
   — e.g. on every keystroke or sentence — should usually keep its fast
   local backend; see question 4's consent rules).
8. **Do any call sites expect streaming?** The CLIs in this guide return
   one complete reply after many seconds; they do not stream tokens. If
   the app's consumers iterate over partial output (a token generator, a
   callback per chunk, a UI that renders text as it arrives), decide the
   adaptation explicitly: either wrap the CLI reply as a one-chunk stream
   (yield once at the end — simplest, works everywhere the UI tolerates a
   long silent wait), or add a non-streaming path in the consumer. Say
   which, per call site, in the plan — and make the UI show a "waiting"
   state, because a stream that says nothing for 20 seconds looks like a
   hang to the user.

Your questionnaire answers override this guide's defaults wherever they
conflict — and they also modify the test plan in section 14 (noted there).

### Working protocol (SHOULD, when a human is reviewing)

Work in stages with a review gate after each: (1) plan + questionnaire
answers, (2) the standalone CLI module, (3) backend wiring, (4) UI/settings,
(5) manual tests. Do not start a stage until the previous one is approved.
Keep a log file recording each prompt and a summary of what you did.

## 4. Step 1 — Detecting the CLIs

Goal: given a name like `"claude"`, return an **absolute path to something the
OS can actually execute**, or "not found".

### The algorithm (MUST follow exactly on Windows)

npm installs CLI commands as **two files**: an extensionless POSIX shell shim
(a bash script) and a `.cmd` wrapper. A naive PATH lookup can return the
extensionless shim, which Windows `CreateProcess` cannot run — you get
**WinError 193 ("%1 is not a valid Win32 application")**. Avoid it by trying
real executable extensions first:

```
resolve_cli(name):
    for candidate in [name + ".exe", name + ".cmd", name + ".bat", name]:
        path = which(candidate)        # standard PATH lookup
        if path: return path
    return None
```

On macOS/Linux the plain name is fine, and this algorithm still works there
(the `.exe/.cmd/.bat` probes simply miss).

### Launching `.cmd`/`.bat` files (Windows)

Batch scripts cannot be spawned directly as a process image. Wrap them:

```
wrap_command(executable):
    if executable ends with ".cmd" or ".bat" (case-insensitive):
        return ["cmd.exe", "/c", executable]
    return [executable]
```

In Node.js the equivalent is passing `{ shell: true }` — but prefer the
explicit `cmd.exe /c` prefix so argument quoting stays predictable.

### What "detected" means in the UI

- Found → show label, the models it provides, and the resolved path
  (e.g. as a tooltip).
- Not found → show "not found on PATH" and the one-line prerequisite:
  "install the CLI and sign in once in your own terminal."

Re-run detection on each settings-screen load; do not cache it across app
restarts (the user may install a CLI while your app is running).

## 5. Step 2 — Invoking Claude Code non-interactively

### The exact command

```
claude -p --output-format json \
       --tools "Read,Glob,Grep" \
       --no-session-persistence \
       --model <alias> \
       [--effort <low|medium|high|xhigh|max>] \
       [--system-prompt "<system prompt text>"]
```

- **The prompt goes in on stdin** (write it, then close stdin). Do NOT pass
  the prompt as a command-line argument — prompts can be huge and contain
  quotes/newlines.
- **The reply comes back as a single JSON object on stdout.**
- Run with `cwd` = the user's working folder.

Flag-by-flag, so nothing gets dropped:

| Flag | Why it is there |
|---|---|
| `-p` | "Print mode" — the documented non-interactive automation surface. One request, one reply, process exits. Without it the CLI opens an interactive UI and hangs your app. |
| `--output-format json` | Machine-readable reply instead of formatted text. |
| `--tools "Read,Glob,Grep"` | Read-only file tools: the CLI may read the project for context but cannot write, run commands, or browse. Pass `--tools ""` to disable tools entirely (faster, no folder awareness). **Never grant write/execute tools** unless your app deliberately wants an autonomous agent — see section 10. |
| `--no-session-persistence` | One-shot requests otherwise pile up session files on disk. |
| `--model <alias>` | Which model answers. Known-good aliases: `sonnet`, `opus`, `haiku`, `fable`. Expose these in your model picker as e.g. `claude-sonnet`. |
| `--effort <level>` | Optional thinking-effort knob (same as `/model` in the interactive CLI). Omit the flag entirely to use the CLI's default. |
| `--system-prompt <text>` | Your app's system prompt. Omit the flag if you have none (do not pass an empty string). |

> **Version drift warning:** CLI flags can change between releases. Before
> relying on this guide's exact flags, run `claude --help` and confirm each
> flag exists; if one is missing, look for its renamed equivalent in the help
> text. The concepts (print mode, JSON output, tool restriction, model
> selection) are stable even when spellings change.

### The reply JSON — what to parse

Stdout is one JSON object. The fields you need (ignore all others):

```json
{
  "is_error": false,
  "result": "The assistant's reply text ...",
  "modelUsage": {
    "claude-sonnet-5":              { "outputTokens": 312, "...": "..." },
    "claude-haiku-4-5-20251001":    { "outputTokens": 12,  "...": "..." }
  }
}
```

Parsing algorithm (MUST):

1. `json.parse(stdout)`. On failure → error: `"Claude returned unparseable
   output: <first 300 chars of stdout>"`.
2. `text = payload["result"]`, stripped. If `payload["is_error"]` is true OR
   `text` is empty → error with the text (or `"empty reply"`).
3. Determine **which model actually answered** from `modelUsage` — see below.

### The `modelUsage` trap (real bug, do not skip)

`modelUsage` often contains **more than one model**: Claude Code runs a small
haiku helper for background chores alongside the main model. If you naively
take the first key, you will sometimes report the helper as the answering
model. Correct selection:

```
served_model(usage_dict, requested_alias):
    if usage_dict is empty or not a dict: return ""
    matches = [id for id in usage_dict if requested_alias is a substring of id]
    if matches: return matches[0]
    # nothing matches the requested alias — a genuine mismatch worth showing
    return the key whose entry has the largest "outputTokens"
```

Example: user picked `sonnet`, keys are `claude-sonnet-5` and
`claude-haiku-4-5-20251001` → `"sonnet" in "claude-sonnet-5"` → report
`claude-sonnet-5`.

### Exit-code handling

If the process exit code is non-zero, do not try to parse stdout. Raise:
`"claude exited with <code>: <first 300 chars of stderr, falling back to
stdout>"`. This is how "not signed in", "flag not recognized", and network
failures surface — the message is user-actionable, so show it.

## 6. Step 3 — Invoking Codex non-interactively

### The exact command

```
codex exec [-m <model-slug>] \
           [-c model_reasoning_effort="<level>"] \
           --sandbox read-only \
           --skip-git-repo-check \
           --color never \
           --cd <working-folder> \
           -o <tempfile-path> \
           -
```

- The trailing **`-`** means "read the prompt from stdin". Write the prompt,
  close stdin.
- **`-o <file>` writes only the final agent message to that file.** This is
  the trick that makes Codex easy: its stdout is full of banners, progress,
  and token counts, and scraping it is fragile. Create a temp file, pass its
  path to `-o`, read the file after exit, and delete it in a `finally` block.
- `--sandbox read-only` is the entire safety story for Codex: it may read the
  project but not write or run anything.
- `--cd <folder>` sets the working folder (Codex has an explicit flag;
  Claude Code uses the process `cwd` instead — set both anyway, it's free).
- `--skip-git-repo-check` — Codex otherwise refuses to run outside a git
  repository; your users' folders may not be repos.
- `--color never` — no ANSI escape codes in any output you might log.

Success = exit code 0 AND the temp file is non-empty. Anything else → error:
`"codex exited with <code>: <first 300 chars of stderr or stdout>"`.

### Differences from Claude Code (memorize these three)

1. **No system-prompt flag.** Prepend instructions to the prompt itself:
   ```
   [Instructions]
   <system prompt>

   <actual prompt>
   ```
2. **Models are discoverable locally — don't hard-code the list.** Codex
   serves several models (the lineup changes over time), selectable with
   `-m <slug>`. Two local files tell you what's available *right now*,
   both readable for free without invoking anything:
   - `~/.codex/models_cache.json` — a catalog the CLI itself keeps fresh
     (it has `fetched_at`/`etag` fields). Use `models[]`, keep only entries
     with `visibility == "list"` (hidden entries are internal), and read
     `slug`, `display_name`, and `supported_reasoning_levels` (a list of
     `{effort, description}`).
   - `~/.codex/config.toml` — the line `model = "<slug>"` is the user's
     configured default, i.e. what plain `codex exec` (no `-m`) will run.
   Expose one app model id per listed slug (e.g. `codex-gpt-5.6-sol`) plus
   a bare `codex` id meaning "CLI default" (label it with the config.toml
   slug when readable).
   **Degrade gracefully (MUST):** both files are *undocumented internals*
   and their format can change. Parse defensively; on any read/parse
   failure fall back to the single `codex` id. Never let a cache-format
   change break detection of the CLI itself.
3. **Effort levels are per-model.** Each cache entry's
   `supported_reasoning_levels` lists what that model accepts (newer models
   go up to `xhigh`/`max`/`ultra`; older ones stop at `xhigh`). Clamp the
   requested effort to the chosen model's supported list; when the list is
   unknown (cache unreadable), clamp to `high` — every Codex model accepts
   that. Effort is passed as a config override:
   `-c model_reasoning_effort="high"` (note: `-c` and the `key="value"`
   string are **two separate argv items**).

Reference discovery snippet (Python; mirror it in other languages):

```python
def discover_codex_models():
    """[{slug, label, efforts}] from the CLI's local cache; [] on any failure."""
    import json
    from pathlib import Path
    try:
        raw = json.loads(
            (Path.home() / ".codex" / "models_cache.json").read_text(encoding="utf-8")
        )
        return [
            {
                "slug": m["slug"],
                "label": m.get("display_name") or m["slug"],
                "efforts": [
                    l.get("effort") for l in m.get("supported_reasoning_levels", [])
                ],
            }
            for m in raw.get("models", [])
            if m.get("visibility") == "list" and m.get("slug")
        ]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def codex_default_model():
    """The slug plain `codex exec` runs, from config.toml; '' if unreadable."""
    import re
    from pathlib import Path
    try:
        text = (Path.home() / ".codex" / "config.toml").read_text(encoding="utf-8")
        m = re.search(r'^model\s*=\s*"([^"]+)"', text, flags=re.M)
        return m.group(1) if m else ""
    except OSError:
        return ""
```

## 6b. Step 3b — Invoking Antigravity CLI (`agy`) non-interactively

Antigravity CLI serves Google's Gemini family plus a few third-party models
through the user's Google account (free tier, or paid Google AI Pro/Ultra).
Verified against agy 1.1.1 on Windows, 2026-07-11 — re-verify with
`agy --help` per the version-drift warning above.

### The exact command

```
agy -p "<full prompt text>" \
    --mode plan \
    --print-timeout 240s \
    --log-file <tempfile-in-cwd> \
    [--model "<exact display label>"]
```

- **The prompt is a command-line argument, not stdin.** Pass empty stdin and
  close it. Windows caps a command line at 32767 characters, so reject
  prompts over ~30000 characters with an actionable "too long" error before
  spawning.
- **The reply is plain text on stdout** (agy 1.1.1 has no JSON output
  format). Strip it; empty stdout or non-zero exit → error.
- `--mode plan` is the read-only mode — the safety equivalent of Codex's
  `--sandbox read-only`. Still use a neutral/empty cwd per Step 0 question 3.
- `--print-timeout 240s` aligns agy's internal print-mode wait with your
  subprocess timeout.
- No system-prompt flag: prepend an `[Instructions]` block exactly like
  Codex (section 6).
- No reasoning-effort flag: **the effort level is part of the model label**
  ("Gemini 3.5 Flash (Low)" vs "(High)"). Ignore your app's effort setting
  for agy models.

### TTY behavior (MUST know, or everything appears broken)

- Only `-p`/`--print` mode works headless. `agy models`, `agy` interactive
  mode, and most subcommands **hang forever without a real terminal** — do
  not call them from your app, not even with output redirected to a file.
- Print mode works from ordinary subprocess pipes on agy 1.1.1. Older
  versions silently dropped stdout under pipes (google-antigravity issue
  #76); after any agy update, re-verify with one piped `pong` test before
  trusting it.
- Sign-in is interactive-only. If agy is not signed in, print mode hangs or
  errors — your timeout is the backstop; tell the user to run `agy` once in
  their own terminal.

### The silent-model-substitution trap (real bug, MUST handle)

`--model` accepts the **exact display label** shown by `agy models`
(e.g. `"Gemini 3.5 Flash (Low)"`). For ANY name it does not recognize —
including plausible slug spellings like `gemini-3.5-flash-low` — agy prints
**no error, exits 0, and silently runs the user's default model instead**.
Some unrecognized-but-accepted spellings even resolve to a different
reasoning level than requested. Never trust exit code + output alone.

The only place the real selection is visible is agy's run log. Pass
`--log-file <path inside your throwaway temp dir>` and verify after the run:

1. If the log contains `Failed to resolve model flag` → the name was
   rejected; raise "model list has changed; run `agy models` and reselect".
2. If a specific model was requested, require
   `label="<requested label>"` to appear in the log; otherwise raise the
   same error. On success, that label is your provenance string.
3. For the "CLI default" entry (no `--model` flag), extract the last
   `label="…"` occurrence as the served model for provenance.

### Model discovery (static list, log-verified)

`agy models` needs a real TTY, and there is no local cache file to read
(unlike Codex), so you cannot discover models programmatically. Ship a
static list captured from a real `agy models` run, plus a "CLI default"
entry, and rely on the log verification above to convert list drift into a
clear user-facing error instead of silent wrong-model spend. Snapshot from
agy 1.1.1 (2026-07-11):

```
Gemini 3.5 Flash (Medium)   Gemini 3.5 Flash (High)   Gemini 3.5 Flash (Low)
Gemini 3.1 Pro (Low)        Gemini 3.1 Pro (High)
Claude Sonnet 4.6 (Thinking)  Claude Opus 4.6 (Thinking)  GPT-OSS 120B (Medium)
```

When capturing your own snapshot, ask the user to run `agy models` and paste
the output — do not try to automate a TTY.

### Differences from Claude Code / Codex (memorize these four)

1. Prompt on **argv**, not stdin (with the ~30000-character guard).
2. Plain-text stdout; no JSON parsing, no `modelUsage` — provenance comes
   from the `--log-file` label check instead.
3. Model names are display labels, silently substituted when wrong — the
   log verification is mandatory, not optional.
4. No effort flag; reasoning level is baked into the model label.

## 7. Step 4 — Multi-turn conversations (all CLIs)

The CLIs take one prompt per invocation and remember nothing. Replay the
history as a labelled transcript each turn. Given an OpenAI-style messages
array (`[{role, content}, ...]`):

1. Concatenate all `system` messages (joined by blank lines) → the system
   prompt.
2. If there is exactly **one** user/assistant message, use its content as the
   prompt directly (no labels).
3. Otherwise build:

```
[User]
first user message

[Assistant]
first assistant reply

[User]
newest user message

[Assistant]
(Reply as the assistant to the last user message. Output only the reply itself.)
```

That final parenthetical block is load-bearing — without it, models often
narrate the transcript or answer earlier turns.

This is O(conversation²) tokens over a long chat. That is acceptable; do not
build session persistence to avoid it unless your app really needs it.

## 8. Step 5 — "Reply as JSON" callers

You cannot grammar-constrain a CLI. If a caller needs a JSON reply:

- Append to the system prompt:
  `"Reply with ONLY a valid JSON object — no prose, no code fences."`
- **Skip the provenance footer** (section 9) for these replies — a footer
  would corrupt the JSON.
- The **caller** must still validate the parse and retry on failure. Do not
  promise valid JSON from this layer.

## 9. Step 6 — Settings, opt-in, and provenance

### Opt-in (MUST)

Every request spends the user's paid subscription usage, so:

- Persist a boolean, e.g. `cli_models_enabled`, **default false**.
- While disabled: the CLI models do not appear in the model picker, and a
  direct request to run one fails with a clear message
  ("Command-line models are switched off. Enable them in settings.").
- Persist the effort level too (e.g. `cli_reasoning_effort`, default `""` =
  CLI's own default). Valid values: `"" | low | medium | high | xhigh | max`
  (clamped for Codex per section 6; ignored for Antigravity per section 6b —
  its reasoning level is part of the model label).

### UI (SHOULD)

One settings section ("Command line" tab or similar) containing:
- The detected-CLI list from section 4 (found/not-found, models, path).
- One checkbox: "Use installed AI CLIs as models" with a one-line warning
  that requests spend the CLI subscription's usage.
- One dropdown: reasoning effort (default / low / medium / high / xhigh / max).

### Provenance footer (SHOULD — users love this)

Users rightly distrust that the model they picked is the model that ran.
Append a small footer to every non-JSON reply:

```
*(via Claude Code · claude-sonnet-5 · effort low)*
```

Format: `*(via <runner>[ · <exact served model id>][ · effort <level>])*` —
runner is `Claude Code`, `Codex CLI`, or `Antigravity CLI`; the served model
comes from the `modelUsage` logic in section 5 for Claude and from the
log-verified label in section 6b for Antigravity (Codex doesn't report one;
omit it).

## 10. Step 7 — Concurrency, timeouts, and safety rails

1. **Never block your app's event loop.** If your request handler is async,
   run the blocking subprocess on a worker thread (`asyncio.to_thread` in
   Python, or use the async subprocess API; in Node use the async
   `child_process.spawn`, never `spawnSync`).
2. **Timeout: 240 seconds per invocation.** Agentic replies that read project
   files before answering are genuinely slow; 30–60 s timeouts cause spurious
   failures. On timeout, kill the process and report
   `"The CLI took longer than 240s and was stopped."`
3. **Read-only, always.** All invocations above are read-only by
   construction (`--tools "Read,Glob,Grep"` / `--sandbox read-only` /
   `--mode plan`). If your
   app lets models edit files, route those edits through your app's own
   propose → show diff → user approves → apply flow. Do not hand the CLI
   write access as a shortcut — you'd have two competing safety models.
4. **Local, single-user only (terms-of-service posture).** Print/exec modes
   are the vendors' documented automation surfaces, invoked as the real
   binaries with their own auth — no token extraction, no client spoofing.
   Keep it that way: never expose an endpoint that lets other machines or
   other people spend the user's subscription.
5. **Text encoding:** decode subprocess output as UTF-8 with error
   replacement (Windows consoles default to legacy code pages otherwise).

## 11. Reference implementation — Python (complete, standard library only)

This is a distilled, working version. Adapt names to the host app.

```python
import asyncio, json, shutil, subprocess, tempfile
from pathlib import Path

SUBPROCESS_TIMEOUT = 240.0
CLAUDE_ALIASES = ("sonnet", "opus", "haiku", "fable")
EFFORT_LEVELS = ("", "low", "medium", "high", "xhigh", "max")


class CLIModelError(Exception):
    pass


def wrap_command(executable):
    if executable.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", executable]
    return [executable]


def resolve_cli(name):
    for candidate in (f"{name}.exe", f"{name}.cmd", f"{name}.bat", name):
        path = shutil.which(candidate)
        if path:
            return path
    return None


def flatten_messages(messages):
    """OpenAI-style messages -> (system_prompt, single_prompt_string)."""
    system = "\n\n".join(
        str(m.get("content", "")) for m in messages
        if m.get("role") == "system" and str(m.get("content", "")).strip()
    )
    turns = [m for m in messages if m.get("role") in ("user", "assistant")]
    if len(turns) == 1:
        return system, str(turns[0].get("content", ""))
    blocks = [
        f"[{'User' if t.get('role') == 'user' else 'Assistant'}]\n{t.get('content', '')}"
        for t in turns
    ]
    blocks.append(
        "[Assistant]\n(Reply as the assistant to the last user message. "
        "Output only the reply itself.)"
    )
    return system, "\n\n".join(blocks)


def served_model(usage, alias):
    if not isinstance(usage, dict) or not usage:
        return ""
    matches = [m for m in usage if alias in m]
    if matches:
        return matches[0]

    def out_tokens(m):
        e = usage.get(m)
        return int(e.get("outputTokens", 0)) if isinstance(e, dict) else 0

    return max(usage, key=out_tokens)


def run_claude(path, alias, system, prompt, cwd, effort):
    """Blocking. Returns (reply_text, exact_model_id_that_answered)."""
    command = wrap_command(path) + [
        "-p", "--output-format", "json",
        "--tools", "Read,Glob,Grep",
        "--no-session-persistence",
        "--model", alias,
    ]
    if effort:
        command += ["--effort", effort]
    if system.strip():
        command += ["--system-prompt", system]
    result = subprocess.run(
        command, input=prompt, capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=cwd,
        timeout=SUBPROCESS_TIMEOUT,
    )
    if result.returncode != 0:
        raise CLIModelError(
            f"claude exited with {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[:300]}"
        )
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise CLIModelError(
            f"Claude returned unparseable output: {result.stdout.strip()[:300]}"
        ) from exc
    text = str(payload.get("result", "")).strip()
    if payload.get("is_error") or not text:
        raise CLIModelError(f"Claude reported an error: {text[:300] or 'empty reply'}")
    return text, served_model(payload.get("modelUsage"), alias)


def run_codex(path, system, prompt, cwd, effort):
    """Blocking. Returns reply_text."""
    if system.strip():
        prompt = f"[Instructions]\n{system}\n\n{prompt}"
    with tempfile.NamedTemporaryFile(
        mode="r", suffix=".txt", delete=False, encoding="utf-8"
    ) as handle:
        out_path = handle.name
    try:
        effort_args = []
        if effort:
            # Baseline-safe clamp; prefer the per-model supported list from
            # discover_codex_models() (section 6) when you implement discovery.
            level = "high" if effort in ("xhigh", "max") else effort
            effort_args = ["-c", f'model_reasoning_effort="{level}"']
        command = wrap_command(path) + [
            "exec", *effort_args,
            "--sandbox", "read-only",
            "--skip-git-repo-check",
            "--color", "never",
            "--cd", cwd,
            "-o", out_path,
            "-",
        ]
        result = subprocess.run(
            command, input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=SUBPROCESS_TIMEOUT,
        )
        text = Path(out_path).read_text(encoding="utf-8").strip()
        if result.returncode != 0 or not text:
            raise CLIModelError(
                f"codex exited with {result.returncode}: "
                f"{(result.stderr or result.stdout).strip()[:300]}"
            )
        return text
    finally:
        Path(out_path).unlink(missing_ok=True)


AGY_MODELS = (  # snapshot of `agy models`; verified per-run via the log
    "Gemini 3.5 Flash (Medium)", "Gemini 3.5 Flash (High)",
    "Gemini 3.5 Flash (Low)", "Gemini 3.1 Pro (Low)", "Gemini 3.1 Pro (High)",
    "Claude Sonnet 4.6 (Thinking)", "Claude Opus 4.6 (Thinking)",
    "GPT-OSS 120B (Medium)",
)
AGY_PROMPT_LIMIT = 30000  # prompt travels on argv; Windows caps ~32767 chars


def run_agy(path, model_label, system, prompt, cwd):
    """Blocking. Returns (reply_text, confirmed_model_label).

    model_label is the exact display label from `agy models`, or "" for the
    user's default model.
    """
    if system.strip():
        prompt = f"[Instructions]\n{system}\n\n{prompt}"
    if len(prompt) > AGY_PROMPT_LIMIT:
        raise CLIModelError(
            f"This request is too long for the Antigravity CLI "
            f"(over {AGY_PROMPT_LIMIT} characters)."
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
    result = subprocess.run(
        command, input="", capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=cwd,
        timeout=SUBPROCESS_TIMEOUT,
    )
    text = str(result.stdout or "").strip()
    if result.returncode != 0 or not text:
        raise CLIModelError(
            f"agy exited with {result.returncode}: "
            f"{(result.stderr or result.stdout or '').strip()[:300]}"
        )
    try:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log_text = ""
    # agy silently substitutes its default model for unknown names (exit 0);
    # the run log is the only place the real selection is visible.
    if model_label:
        if ("Failed to resolve model flag" in log_text
                or f'label="{model_label}"' not in log_text):
            raise CLIModelError(
                f"Antigravity CLI did not run {model_label!r}. Its model "
                "list has probably changed; run 'agy models' and reselect."
            )
        return text, model_label
    import re
    served = re.findall(r'label="([^"]+)"', log_text)
    return text, served[-1] if served else ""


async def converse(model, messages, cwd, effort="", json_only=False):
    """Entry point. model: 'claude-<alias>', 'codex', or 'agy[-<slug>]'."""
    system, prompt = flatten_messages(messages)
    if json_only:
        system = (
            f"{system}\n\nReply with ONLY a valid JSON object — no prose, "
            "no code fences."
        ).strip()
    if model.startswith("claude-"):
        path = resolve_cli("claude")
        if not path:
            raise CLIModelError("The claude CLI was not found on PATH.")
        alias = model.removeprefix("claude-")
        if alias not in CLAUDE_ALIASES:
            raise CLIModelError(f"Unknown Claude alias {alias!r}.")
        try:
            text, served = await asyncio.to_thread(
                run_claude, path, alias, system, prompt, cwd, effort
            )
        except subprocess.TimeoutExpired:
            raise CLIModelError("The CLI took longer than 240s and was stopped.")
        footer = "" if json_only else (
            f"\n\n*(via Claude Code{' · ' + served if served else ''}"
            f"{' · effort ' + effort if effort else ''})*"
        )
        return text + footer
    if model == "codex":
        path = resolve_cli("codex")
        if not path:
            raise CLIModelError("The codex CLI was not found on PATH.")
        try:
            text = await asyncio.to_thread(
                run_codex, path, system, prompt, cwd, effort
            )
        except subprocess.TimeoutExpired:
            raise CLIModelError("The CLI took longer than 240s and was stopped.")
        footer = "" if json_only else (
            f"\n\n*(via Codex CLI{' · effort ' + effort if effort else ''})*"
        )
        return text + footer
    if model == "agy" or model.startswith("agy-"):
        path = resolve_cli("agy")
        if not path:
            raise CLIModelError("The Antigravity CLI (agy) was not found on PATH.")
        # App model ids are slugified labels, e.g. "agy-gemini-3.5-flash-low";
        # map the id back to the exact display label before invoking.
        import re as _re
        label = ""
        if model != "agy":
            by_slug = {
                "agy-" + _re.sub(r"[^a-z0-9.]+", "-", l.lower()).strip("-"): l
                for l in AGY_MODELS
            }
            label = by_slug.get(model, "")
            if not label:
                raise CLIModelError(f"Unknown Antigravity model {model!r}.")
        try:
            text, served = await asyncio.to_thread(
                run_agy, path, label, system, prompt, cwd
            )
        except subprocess.TimeoutExpired:
            raise CLIModelError("The CLI took longer than 240s and was stopped.")
        footer = "" if json_only else (
            f"\n\n*(via Antigravity CLI{' · ' + served if served else ''})*"
        )
        return text + footer
    raise CLIModelError(f"Unknown command-line model {model!r}.")
```

## 12. Reference implementation — Node.js (same logic)

```javascript
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const which = require("which"); // npm i which  (or hand-roll PATH scan)

const SUBPROCESS_TIMEOUT_MS = 240_000;
const CLAUDE_ALIASES = ["sonnet", "opus", "haiku", "fable"];

function wrapCommand(executable) {
  if (/\.(cmd|bat)$/i.test(executable)) return ["cmd.exe", ["/c", executable]];
  return [executable, []];
}

function resolveCli(name) {
  for (const c of [`${name}.exe`, `${name}.cmd`, `${name}.bat`, name]) {
    const p = which.sync(c, { nothrow: true });
    if (p) return p;
  }
  return null;
}

function flattenMessages(messages) {
  const system = messages
    .filter((m) => m.role === "system" && String(m.content || "").trim())
    .map((m) => m.content)
    .join("\n\n");
  const turns = messages.filter((m) => m.role === "user" || m.role === "assistant");
  if (turns.length === 1) return { system, prompt: String(turns[0].content || "") };
  const blocks = turns.map(
    (t) => `[${t.role === "user" ? "User" : "Assistant"}]\n${t.content || ""}`
  );
  blocks.push(
    "[Assistant]\n(Reply as the assistant to the last user message. " +
      "Output only the reply itself.)"
  );
  return { system, prompt: blocks.join("\n\n") };
}

function servedModel(usage, alias) {
  if (!usage || typeof usage !== "object") return "";
  const keys = Object.keys(usage);
  if (!keys.length) return "";
  const match = keys.find((k) => k.includes(alias));
  if (match) return match;
  return keys.reduce((a, b) =>
    (usage[a]?.outputTokens || 0) >= (usage[b]?.outputTokens || 0) ? a : b
  );
}

function runProcess(exe, args, { cwd, input }) {
  return new Promise((resolve, reject) => {
    const [cmd, prefix] = wrapCommand(exe);
    const child = spawn(cmd, [...prefix, ...args], { cwd });
    let stdout = "", stderr = "";
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error("The CLI took longer than 240s and was stopped."));
    }, SUBPROCESS_TIMEOUT_MS);
    child.stdout.on("data", (d) => (stdout += d));
    child.stderr.on("data", (d) => (stderr += d));
    child.on("error", (err) => { clearTimeout(timer); reject(err); });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr });
    });
    child.stdin.write(input, "utf-8");
    child.stdin.end();
  });
}

async function runClaude(cliPath, alias, system, prompt, cwd, effort) {
  const args = [
    "-p", "--output-format", "json",
    "--tools", "Read,Glob,Grep",
    "--no-session-persistence",
    "--model", alias,
  ];
  if (effort) args.push("--effort", effort);
  if (system.trim()) args.push("--system-prompt", system);
  const { code, stdout, stderr } = await runProcess(cliPath, args, { cwd, input: prompt });
  if (code !== 0)
    throw new Error(`claude exited with ${code}: ${(stderr || stdout).trim().slice(0, 300)}`);
  let payload;
  try { payload = JSON.parse(stdout); }
  catch { throw new Error(`Claude returned unparseable output: ${stdout.trim().slice(0, 300)}`); }
  const text = String(payload.result || "").trim();
  if (payload.is_error || !text)
    throw new Error(`Claude reported an error: ${text.slice(0, 300) || "empty reply"}`);
  return { text, served: servedModel(payload.modelUsage, alias) };
}

async function runCodex(cliPath, system, prompt, cwd, effort) {
  if (system.trim()) prompt = `[Instructions]\n${system}\n\n${prompt}`;
  const outPath = path.join(os.tmpdir(), `codex-last-${Date.now()}-${Math.random().toString(36).slice(2)}.txt`);
  try {
    const effortArgs = [];
    if (effort) {
      // Baseline-safe clamp; prefer the per-model supported list from the
      // models_cache.json discovery (section 6) when you implement it.
      const level = effort === "xhigh" || effort === "max" ? "high" : effort;
      effortArgs.push("-c", `model_reasoning_effort="${level}"`);
    }
    const args = [
      "exec", ...effortArgs,
      "--sandbox", "read-only",
      "--skip-git-repo-check",
      "--color", "never",
      "--cd", cwd,
      "-o", outPath,
      "-",
    ];
    const { code, stdout, stderr } = await runProcess(cliPath, args, { cwd, input: prompt });
    const text = fs.existsSync(outPath)
      ? fs.readFileSync(outPath, "utf-8").trim()
      : "";
    if (code !== 0 || !text)
      throw new Error(`codex exited with ${code}: ${(stderr || stdout).trim().slice(0, 300)}`);
    return text;
  } finally {
    try { fs.unlinkSync(outPath); } catch {}
  }
}

const AGY_MODELS = [ // snapshot of `agy models`; verified per-run via the log
  "Gemini 3.5 Flash (Medium)", "Gemini 3.5 Flash (High)",
  "Gemini 3.5 Flash (Low)", "Gemini 3.1 Pro (Low)", "Gemini 3.1 Pro (High)",
  "Claude Sonnet 4.6 (Thinking)", "Claude Opus 4.6 (Thinking)",
  "GPT-OSS 120B (Medium)",
];
const AGY_PROMPT_LIMIT = 30000; // prompt travels on argv; Windows caps ~32767

async function runAgy(cliPath, modelLabel, system, prompt, cwd) {
  if (system.trim()) prompt = `[Instructions]\n${system}\n\n${prompt}`;
  if (prompt.length > AGY_PROMPT_LIMIT)
    throw new Error(
      `This request is too long for the Antigravity CLI (over ${AGY_PROMPT_LIMIT} characters).`
    );
  const logPath = path.join(cwd, "agy_run.log");
  const args = [
    "-p", prompt,
    "--mode", "plan",
    "--print-timeout", "240s",
    "--log-file", logPath,
  ];
  if (modelLabel) args.push("--model", modelLabel);
  const { code, stdout, stderr } = await runProcess(cliPath, args, { cwd, input: "" });
  const text = String(stdout || "").trim();
  if (code !== 0 || !text)
    throw new Error(`agy exited with ${code}: ${(stderr || stdout).trim().slice(0, 300)}`);
  let logText = "";
  try { logText = fs.readFileSync(logPath, "utf-8"); } catch {}
  // agy silently substitutes its default model for unknown names (exit 0);
  // the run log is the only place the real selection is visible.
  if (modelLabel) {
    if (
      logText.includes("Failed to resolve model flag") ||
      !logText.includes(`label="${modelLabel}"`)
    )
      throw new Error(
        `Antigravity CLI did not run ${modelLabel}. Its model list has ` +
          "probably changed; run 'agy models' and reselect."
      );
    return { text, served: modelLabel };
  }
  const served = [...logText.matchAll(/label="([^"]+)"/g)].map((m) => m[1]);
  return { text, served: served.length ? served[served.length - 1] : "" };
}

module.exports = {
  resolveCli, flattenMessages, runClaude, runCodex, runAgy,
  CLAUDE_ALIASES, AGY_MODELS,
};
```

## 13. Wiring it into the host app's model list

- **Model ids:** if `claude` is detected, add `claude-sonnet`, `claude-opus`,
  `claude-haiku`, `claude-fable` to the picker; if `codex` is detected, add
  `codex` plus one id per discovered slug; if `agy` is detected, add `agy`
  (CLI default) plus one slugified id per snapshot label
  (e.g. `agy-gemini-3.5-flash-low`). Only while the opt-in toggle is ON.
- **Routing:** a request for any of those ids goes to this module; everything
  else goes to the app's existing providers. Match by prefix
  (`claude-<known alias>`, `agy-<slug>`) and exact id (`codex`, `agy`).
- **Interface parity:** if the app's provider interface has `max_tokens` or
  temperature parameters, accept and ignore them — the CLIs budget their own
  output.

How this wiring looks concretely is dictated by your Step 0 questionnaire
answers (section 3): sync vs async invocation, error contract, cwd/tools,
fan-out exclusion, and settings location all come from there, not from this
guide's defaults.

## 14. Manual test plan (run these by hand — each spends real usage)

Do these in order; stop at the first failure and fix before continuing.
Tests 4–9 each cost one subscription request, so run them once, not in a loop.
Adapt the list to your Step 0 questionnaire answers: skip test 7 if you
disabled file tools (question 3), and add test 11 if the app has any
multi-model or failover feature (question 4).

1. **Detection, CLI present:** with `claude` installed, the settings screen
   shows it as found with a real path. (Free — no request made.)
1b. **Reachability everywhere:** after enabling (and favoriting/selecting,
   if the app requires it), the CLI models appear in EVERY model-picking
   surface listed in your Step 0 question 6 answer — not just the main
   picker. A model the user can't select from a feature doesn't exist for
   that feature. (Free.)
2. **Detection, CLI absent:** temporarily rename the CLI shim folder out of
   PATH (or test on a machine without it) → shows "not found", app still
   works. (Free.)
2b. **Codex discovery:** with codex installed, the model list shows the
   slugs from `~/.codex/models_cache.json` (compare by eye), the bare
   `codex` entry is labelled with the config.toml default, and renaming
   `models_cache.json` away degrades to the single `codex` entry without
   errors. Restore the file afterwards. (Free.)
3. **Disabled by default:** fresh settings → toggle is off, CLI models absent
   from the picker, and a forced request errors with the "switched off"
   message. (Free.)
4. **Single-turn smoke test:** enable, pick `claude-haiku` (cheapest), send
   "Reply with exactly: pong". Expect "pong" plus a provenance footer naming
   a haiku model id.
5. **Model verification:** pick `claude-sonnet`, ask anything trivial →
   footer names a sonnet model id (proves `modelUsage` selection works).
6. **Multi-turn:** send "My name is Bo", then "What is my name?" → second
   reply says "Bo" (proves transcript replay).
7. **Workspace awareness:** in a folder containing a README, ask "What is
   this project? Check the files." → answer reflects the README (proves cwd
   + read tools).
8. **Codex smoke test** (if installed): pick `codex`, send "Reply with
   exactly: pong" → "pong" with a Codex footer.
9. **Effort setting:** set effort to `low`, send a trivial prompt → footer
   shows "effort low".
9b. **Antigravity smoke test** (if installed): pick a cheap Flash model by
    its exact label id, send "Reply with exactly: pong" → "pong" with an
    Antigravity footer naming that same label (proves the log verification
    confirmed the requested model actually served).
9c. **Antigravity substitution guard:** temporarily add a fake label to the
    static model list, request it → clean "did not run …; run 'agy models'"
    error, NOT a silent success. Remove the fake afterwards. (Costs one
    request on the default model.)
10. **Timeout path (optional):** temporarily set the timeout to 1 s, send any
    prompt → clean "took longer than" error, no zombie process, UI usable.
    Restore 240 s afterwards.
11. **Fan-out safety (if the app has multi-model or failover features):**
    with a CLI model enabled and favorited, trigger the multi-model feature
    → verify **zero** CLI invocations happen (check that no provenance
    footer from a CLI model appears, and no CLI process spawns). A CLI model
    must run only when explicitly picked. (Free if it passes.)

## 15. Troubleshooting table

| Symptom | Cause | Fix |
|---|---|---|
| WinError 193 / "not a valid Win32 application" | PATH lookup returned npm's extensionless POSIX shim | Section 4 resolution order; launch `.cmd` via `cmd.exe /c` |
| App freezes while CLI thinks | Blocking subprocess on the event loop | Worker thread / async spawn (section 10) |
| Footer names a haiku model when user picked sonnet | Took first `modelUsage` key | Section 5 `served_model` algorithm |
| Codex reply full of banners/garbage | Scraping stdout | Use `-o <tempfile>`, read the file |
| `codex` refuses to start | Folder is not a git repo | `--skip-git-repo-check` |
| "not signed in" style errors | User never logged the CLI in | Prerequisite: CLI must work in the user's own terminal |
| Garbled non-ASCII text on Windows | Legacy console code page | Decode as UTF-8 with error replacement |
| Model answers an old turn in the chat | Missing final `[Assistant] (Reply as…)` block | Section 7 transcript format |
| Unknown-flag error from `claude` | CLI version drift | Run `claude --help`, map to renamed flag |
| Multi-model feature silently spends subscription | CLI models included in fan-out pool or failover chain | Exclude them per Step 0 question 4 |
| Multi-model features break when one CLI model errors | CLI backend raises where the app expects error return values | Match the host error contract per Step 0 question 2 |
| Only a single generic `codex` model shows up | Model list hard-coded instead of discovered | Read `~/.codex/models_cache.json` + `config.toml` per section 6 |
| Codex model list wrong/empty after a CLI update | Cache format changed (it's undocumented) | The graceful fallback to bare `codex` is by design; update the parser |
| `agy models` / `agy` hangs forever from your app | Those surfaces need a real TTY | Only `-p` print mode works headless (section 6b); get the model list from the user |
| agy ran the wrong (default) model with exit 0 | Unknown `--model` names are silently substituted | Pass exact display labels and verify `label="…"` in the `--log-file` (section 6b) |
| agy `-p` returns nothing through pipes after an update | Regression of the non-TTY stdout bug (issue #76) | Re-run one piped `pong` test per agy update; fall back to file-redirected output if needed |
| `agy` not found right after install | Installer updates registry PATH without broadcasting the change | New terminals/apps see it after a `WM_SETTINGCHANGE` broadcast, re-login, or reboot |
| Very long chat fails only on Antigravity | Prompt travels on argv; Windows command-line limit | The ~30000-character guard in section 6b; shorten history or route long chats elsewhere |
| UI freezes or shows nothing for ~20s, then everything at once | Streaming consumer fed a non-streaming CLI reply with no waiting state | Step 0 question 8: yield-once adaptation + a visible pending indicator |
| CLI request fired on an automatic hot path (per keystroke/sentence) | Single-backend app routed ALL call sites through the new seam | Step 0 question 7: route only explicitly-picked, latency-tolerant flows |

## 16. Acceptance checklist

- [ ] Step 0 questionnaire answered in writing, every answer with a `file:line` citation from the host app
- [ ] CLI backend matches the host app's sync/async style and error-reporting contract
- [ ] CLI models excluded from every fan-out pool and failover chain — backend and frontend — they run only on an explicit user pick; group-feature enrollment (parliament-style) surfaced to the human as a policy decision
- [ ] UI uses the frontend's real conventions (framework, module system, design tokens verified against the stylesheet — no guessed names)
- [ ] CLI models reachable from every model-picking surface in the app
- [ ] Single-backend apps: the backend seam was introduced as its own stage, existing behavior unchanged; automatic/latency-sensitive call sites explicitly kept OFF the CLI path
- [ ] Streaming consumers: adaptation decided per call site, with a visible waiting state during the CLI's silent seconds
- [ ] Detection resolves `.exe`/`.cmd`/`.bat` before the bare name; `.cmd`/`.bat` launched via `cmd.exe /c`
- [ ] Claude invoked with `-p --output-format json --tools "Read,Glob,Grep" --no-session-persistence --model <alias>`; prompt on stdin
- [ ] Claude reply parsed from `result` / `is_error`; served model chosen by alias-substring match with output-token fallback
- [ ] Codex invoked with `exec --sandbox read-only --skip-git-repo-check --color never --cd <folder> -o <tempfile> -`; temp file cleaned up
- [ ] Codex system prompt prepended as `[Instructions]` block; effort clamped to the chosen model's supported levels (`high` when unknown)
- [ ] Codex models discovered from `~/.codex/models_cache.json` (visibility == "list") with the config.toml default labelled; graceful fallback to bare `codex` when either file is unreadable
- [ ] Antigravity invoked with `-p <prompt> --mode plan --print-timeout 240s --log-file <tempfile>` (+ `--model "<exact label>"`); prompt on argv with the ~30000-character guard; empty stdin
- [ ] Antigravity model selection verified against the run log (`Failed to resolve` absent AND `label="<requested>"` present); silent default substitution surfaces as a clear error
- [ ] Antigravity model list is a static snapshot from a real `agy models` run (needs a TTY — ask the user); effort setting ignored for agy models
- [ ] Multi-turn history flattened to the labelled transcript with the closing instruction block
- [ ] Subprocess never blocks the event loop; 240 s timeout with a clean error
- [ ] Feature default-OFF, persisted toggle, clear "spends your subscription" label
- [ ] Provenance footer on non-JSON replies; omitted for JSON-only replies
- [ ] No write/execute tools granted to either CLI
- [ ] Nothing network-exposed: local, single-user only
