# CLI models porting plan for Vedium

Status: Stage 1 complete; awaiting review before any runtime code is changed.

This plan applies `CLI_MODELS_PORTING_GUIDE.md` to the current Vedium codebase.
The implementation will proceed through the guide's review gates. Runtime code
must not begin until this document is approved.

## Confirmed product decisions

- Command-line models are an explicit opt-in and are disabled by default.
- A CLI model may be the active/default backend for manual actions because the
  user explicitly selected it after accepting the subscription-usage warning.
- Automatic CLI spell-checking is a second, independent opt-in. It is always
  off by default, including after upgrades, and every transition from off to on
  must show a confirmation warning explaining that completing sentences can
  repeatedly spend Claude/Codex subscription usage.
- CLI models never enter an automatic fallback chain. They run only when the
  active backend names the selected CLI model.
- Existing local, llama-server, and cloud spell-check behavior is unchanged.

## Step 0 host-app questionnaire

### 1. Is the provider layer synchronous or asynchronous?

It is synchronous. The active model is a module-global client protected by a
`threading.Lock` (`spell_daemon/llm.py:17-22`). Provider entry points such as
`connect_cloud` and `load_model` are normal blocking functions that return a
backend label or raise (`spell_daemon/llm.py:493-526`,
`spell_daemon/llm.py:1117-1149`). The host already keeps blocking work off the
Tk event thread: automatic sentence work is drained by a dedicated worker
thread (`spell_daemon/daemon.py:127-157`), hotkey actions start worker threads
(`spell_daemon/daemon.py:178-214`), and standalone chat starts its own worker
(`spell_daemon/ui_settings.py:2904-2983`).

Decision: the CLI module will expose blocking synchronous functions. It will
not introduce `asyncio` or `asyncio.to_thread`. Existing background threads are
the correct execution context.

### 2. How are provider errors reported?

Providers raise exceptions. For example, cloud connection validation raises
`RuntimeError` for missing credentials/model and propagates request failures
(`spell_daemon/llm.py:493-516`). Automatic spell-check and manual rephrase
pipelines catch exceptions, record them, release buffer state, and re-raise
(`spell_daemon/daemon.py:624-631`, `spell_daemon/daemon.py:766-773`). The
standalone chat catches an exception and inserts its message into the visible
transcript (`spell_daemon/ui_settings.py:2933-2946`). When no backend exists,
the text helpers use their existing benign fallbacks rather than raising
(`spell_daemon/llm.py:1661-1669`, `spell_daemon/llm.py:1895-1904`,
`spell_daemon/llm.py:2055-2065`).

Decision: `CLIModelError` will follow the exception contract. Missing CLI,
authentication, non-zero exit, malformed output, empty output, and timeout
errors will be raised with the guide's actionable text. Manual toast paths
must render that error visibly instead of merely leaving it in a background
thread log; standalone chat already has the required visible error path.

### 3. Does the app have a user workspace folder?

No. Vedium listens globally, captures text typed or selected in another
application, and sends that text to its model. The punctuation listener and
selection-first hotkey flow prove that the input is active-application text,
not a project workspace (`spell_daemon/daemon.py:173-246`,
`spell_daemon/daemon.py:383-416`). Its configuration contains model and
behavior settings but no workspace/project field
(`spell_daemon/constants.py:30-66`). The current data file also lives beside
the installed application rather than in a user project
(`spell_daemon/constants.py:21-26`).

Decision: never use the source/install folder as CLI context. Claude will be
invoked with `--tools ""`. Codex will retain `--sandbox read-only` and both
CLIs will run in a fresh, otherwise empty temporary directory. This prevents a
selected-text request from accidentally exposing Vedium source, configuration,
or unrelated user files. The temporary directory is removed after the call.

The settings warning will say both that prompts leave the computer and that
requests spend the user's CLI subscription usage.

### 4. Can the app invoke models without an explicit user pick?

Yes, in one feature: punctuation-triggered sentence correction. The keyboard
listener turns a completed sentence into a queued job
(`spell_daemon/daemon.py:223-246`), the worker calls `_pipeline`
(`spell_daemon/daemon.py:137-157`), and `_pipeline` calls
`spell_check_sentence` (`spell_daemon/daemon.py:429-459`). This is automatic and
can run repeatedly while the user types.

The exhaustive runtime invocation list is:

| Invocation surface | Trigger and call path | CLI policy |
|---|---|---|
| Completed-sentence spell-check | Punctuation -> queue -> `_pipeline` -> `spell_check_sentence` (`spell_daemon/daemon.py:223-246`, `spell_daemon/daemon.py:455-459`) | Blocked centrally unless `cli_spell_check_enabled` is true. The setting defaults false and requires a warning/confirmation to enable. Existing `spell_check_mode=Off` remains an additional global off switch. |
| Understand / Improve Writing hotkeys, including per-mode hotkeys | Explicit key press starts `_handle_rephrase_hotkey`; selected text routes to `_selected_rephrase_pipeline`, typed text to `_rephrase_pipeline` (`spell_daemon/daemon.py:178-214`, `spell_daemon/daemon.py:383-416`) | Allowed when the CLI backend/model was explicitly selected and the global CLI opt-in is on. |
| Typed-text rephrase generation | `_rephrase_pipeline` -> `rephrase_sentence_stream` (`spell_daemon/daemon.py:635-689`) | Allowed; explicit hotkey action. |
| Selected-text understand/rephrase generation | `_selected_rephrase_pipeline` -> `rephrase_sentence_stream` (`spell_daemon/daemon.py:777-833`) | Allowed; explicit selection plus hotkey action. |
| Follow-up AI action inside a spell suggestion toast | User submits "Ask AI" -> `chat_with_text_stream` (`spell_daemon/ui_toasts.py:292-341`) | Allowed; explicit send action. |
| Follow-up AI action inside an editable assistant toast | User submits instruction -> `chat_with_text_stream` (`spell_daemon/ui_toasts.py:740-793`) | Allowed; explicit send action. |
| Standalone Chat | User presses Send -> `free_chat_stream` (`spell_daemon/ui_settings.py:2874-2924`) | Allowed; explicit send action. Full history is replayed. |
| Cloud model failover | `_complete_prompt` / `_stream_prompt` enter fallback only for `_CloudClient`; candidates come only from `cloud_favorites` (`spell_daemon/llm.py:814-845`, `spell_daemon/llm.py:625-679`) | CLI models remain a separate backend type and cannot be stored in `cloud_favorites`; therefore they can neither lead nor appear in the fallback tail. |
| Backend connection checks | Cloud connection currently makes a one-token smoke request (`spell_daemon/llm.py:508-516`) | CLI "connect" performs detection/validation only and spends no subscription request. A real smoke request is reserved for the reviewed manual-test stage. |

There is no fan-out, debate, voting, panel, or send-to-all feature. Cloud
favorites are an ordered cloud-only failover list, not a generic model pool
(`spell_daemon/llm.py:625-679`). The central safety boundary will be the shared
CLI dispatch/`spell_check_sentence` path, not separate UI-only filters. A forced
CLI call while `cli_models_enabled` is false must raise "Command-line models
are switched off. Enable them in settings."

Default-model policy: Vedium has one active backend used by all features
(`spell_daemon/llm.py:17-22`). A CLI backend may be saved as active for manual
features. Automatic sentence correction additionally requires the independent
`cli_spell_check_enabled` consent described above. This records the human
decision requested by the guide rather than treating backend selection alone
as consent for repeated background requests.

### 5. Where do non-secret settings live?

Defaults live in `DEFAULT_CFG` (`spell_daemon/constants.py:30-66`). `load_cfg`
merges missing defaults and validates `backend_mode`
(`spell_daemon/config.py:233-293`). `save_cfg` persists the same dictionary to
the existing JSON file while stripping only secret cloud keys
(`spell_daemon/config.py:336-353`). The settings bottom bar copies widget state
into that dictionary before calling `save_cfg`
(`spell_daemon/ui_settings.py:2305-2386`).

Decision: add only non-secret keys to this existing store:

- `cli_models_enabled: false`
- `cli_model: ""`
- `cli_reasoning_effort: ""`
- `cli_spell_check_enabled: false`

`backend_mode` gains the valid value `"cli"`. No runtime settings file or
credential store will be added.

### 6. What are the frontend conventions and model-picking surfaces?

The UI is Python Tkinter/ttk, organized as functions in
`spell_daemon/ui_settings.py` with package-relative imports
(`spell_daemon/ui_settings.py:1-22`). Settings are a `ttk.Notebook` whose tabs
are built by the existing per-tab builder functions
(`spell_daemon/ui_settings.py:2525-2587`,
`spell_daemon/ui_settings.py:2681-2704`). There is no web frontend, JavaScript,
CSS, or separate module system.

The actual design tokens are `BG`, `SURFACE`, `SURF2`, `ACCENT`, `ACCENT2`,
`TEXT`, `MUTED`, `SUCCESS`, `WARN`, and `ERR`; their defaults and schemes are
defined in `spell_daemon/constants.py:68-110`. Existing model controls use
Tkinter radio buttons, labels, the shared toggle helper, and styled ttk
comboboxes (`spell_daemon/ui_settings.py:499-513`,
`spell_daemon/ui_settings.py:591-612`, `spell_daemon/ui_settings.py:726-756`).

Model-selection surfaces are exhaustive as follows:

1. The Model tab is the only real active-backend/model picker. It selects the
   backend by radio button, a local model by file path, or a cloud model by
   provider/model combobox; cloud Favorites is only a shortcut for those cloud
   fields (`spell_daemon/ui_settings.py:480-523`,
   `spell_daemon/ui_settings.py:669-756`). Add a fourth `AI CLI` radio and a
   CLI sub-panel here.
2. Setup's "Choose AI" card only navigates to the Model tab; it does not own a
   separate picker (`spell_daemon/ui_settings.py:1299-1325`). Update its status
   text to understand CLI readiness.
3. Standalone Chat contains no picker and always uses the active backend
   (`spell_daemon/ui_settings.py:2713-2750`,
   `spell_daemon/ui_settings.py:2922-2924`). No new chat picker is required.
4. The Assistant tab selects prompts/modes, not models
   (`spell_daemon/ui_settings.py:1574-1604`). No CLI entries belong there.

Decision: keep the detector, invokers, parsers, discovery, transcript adapter,
and CLI backend adapter together in `spell_daemon/cli_models.py`. Keep the
small Tk widget integration in the existing centralized
`spell_daemon/ui_settings.py`, rather than creating a second UI module. This is
the one host-convention adaptation to the guide's one-file suggestion.

### 7. Does the app already have a multi-model abstraction?

It has one active global backend, but that backend is already duck-typed rather
than hard-wired to llama.cpp. `_LlamaServerClient` deliberately mirrors the
`llama_cpp.Llama` call interface (`spell_daemon/llm.py:48-82`), and
`_CloudClient` does the same so existing call sites remain unchanged
(`spell_daemon/llm.py:328-336`). Shared `_complete_prompt` and `_stream_prompt`
helpers dispatch through the active client (`spell_daemon/llm.py:814-845`).

Decision: no broad provider refactor is needed. Add the minimum semantic
extension required by CLIs: the shared completion helpers will accept the
already-known system/user messages as optional input. Existing local/server
clients continue receiving their current rendered prompt byte-for-byte; the
CLI adapter receives semantic messages so it never sees ChatML/Llama template
markers. `free_chat_stream`, which already owns the full history, will pass the
system message plus that history directly to the CLI adapter
(`spell_daemon/llm.py:2037-2074`).

All existing local/server/cloud behavior remains identical. The only feature
policy deviation is the explicitly approved CLI spell-check gate.

### 8. Do call sites expect streaming?

Yes. `rephrase_sentence_stream` is a generator
(`spell_daemon/llm.py:1875-1904`), `chat_with_text_stream` yields chunks
(`spell_daemon/llm.py:1963-2034`), and `free_chat_stream` is consumed as a
stream by the chat window (`spell_daemon/ui_settings.py:2922-2932`). CLI
subprocesses return one complete reply.

Decision: adapt a CLI result as one final llama-style chunk. Do not add a
second non-streaming UI path.

- Typed and selected rephrase toasts already begin with "Generating
  suggestion..." and display "Thinking..." while no answer is visible
  (`spell_daemon/daemon.py:662-697`, `spell_daemon/daemon.py:796-841`). The
  one-chunk adapter fits without consumer changes.
- Both in-toast follow-up editors explicitly insert "Thinking..." before the
  call (`spell_daemon/ui_toasts.py:313-341`,
  `spell_daemon/ui_toasts.py:761-793`). The one-chunk adapter fits.
- Standalone Chat disables input but currently inserts only the AI label before
  starting work (`spell_daemon/ui_settings.py:2894-2904`). Add an immediate
  visible "Thinking..." placeholder so a silent CLI wait does not look hung.
- Sentence spell-check is already non-streaming
  (`spell_daemon/llm.py:1646-1661`). If CLI spell-check is explicitly enabled,
  its existing 12-second safety threshold converts a slow automatic result to
  a review toast instead of replacing blindly
  (`spell_daemon/daemon.py:524-556`).

There are no model callers that require a JSON-only semantic response; the
public feature functions return or yield user-facing text
(`spell_daemon/llm.py:1646-1649`, `spell_daemon/llm.py:1875-1882`,
`spell_daemon/llm.py:2037-2049`). Provenance must therefore be adapted to avoid
polluting replacement text: standalone Chat may append the guide's footer,
while spell-check/rephrase results show provenance in UI metadata or diagnostics
and never append it to text that could be pasted into the user's document.

## Implementation stages and review gates

### Stage 2: standalone CLI module

Files: new `spell_daemon/cli_models.py` and focused tests only.

- Implement exact Windows resolution order: `.exe`, `.cmd`, `.bat`, bare;
  launch batch shims via `cmd.exe /c`.
- Implement defensive Codex discovery from `~/.codex/models_cache.json` and
  default labeling from `~/.codex/config.toml`, degrading to bare `codex`.
- Implement effort validation and per-Codex-model clamping.
- Implement transcript flattening and Claude `modelUsage` served-model
  selection.
- Implement blocking Claude invocation with prompt on stdin, JSON parsing,
  `--tools ""`, no session persistence, UTF-8 replacement decoding, and a
  240-second timeout.
- Implement blocking Codex invocation with stdin, `--sandbox read-only`,
  `--skip-git-repo-check`, `--color never`, `--cd`, final-message tempfile,
  cleanup, and a 240-second timeout.
- Run both CLIs in a fresh neutral temporary directory.
- Unit-test command construction, `.cmd` wrapping, parsing, discovery fallback,
  effort clamping, disabled-state rejection, timeout translation, and tempfile
  cleanup with subprocess/filesystem mocks. No paid request in automated tests.
- Review gate: inspect the standalone module and test output before importing it
  into Vedium.

Local free verification already performed on 2026-07-11: both `claude.cmd` and
`codex.cmd` are installed; their current help output includes the required
Claude print/JSON/tools/session/model/effort/system-prompt flags and Codex
model/sandbox/cd/git-check/color/output-last-message flags. Stage 2 tests still
must not assume either CLI is installed.

### Stage 3: backend wiring

Files: `spell_daemon/llm.py`, `spell_daemon/daemon.py`,
`spell_daemon/constants.py`, `spell_daemon/config.py`, and existing focused
tests.

- Add `connect_cli` that validates the opt-in and selected detected model,
  installs the CLI adapter as the active backend, and performs no paid smoke
  request.
- Extend the shared completion/stream helpers with semantic-message input only
  for the CLI adapter. Preserve existing rendered prompts and behavior for all
  current backends.
- Route single-turn correction/rephrase/follow-up calls and full standalone
  chat history through the adapter. Accept and ignore max-token/temperature
  controls for CLI calls.
- Enforce the automatic spell-check consent in the shared spell-check path:
  when the active backend is CLI and `cli_spell_check_enabled` is false, return
  the original text without spawning a process.
- Add explicit CLI startup handling. A missing/disabled CLI opens Settings and
  reports the error; it never falls back to local, server, cloud, or another
  CLI model.
- Surface `CLIModelError` in the relevant toast/chat UI while preserving buffer
  cleanup and locks.
- Record provenance separately for replaceable outputs; append a footer only
  where it cannot enter the user's document.
- Run the existing prompt, smoke, config, and import tests plus new focused
  wiring tests.
- Review gate: verify behavior-preserving diffs for existing backends before UI
  work.

### Stage 4: settings and consent UI

File: `spell_daemon/ui_settings.py` plus focused UI tests.

- Add the `AI CLI` backend radio and sub-panel using the existing Tkinter
  tokens/widgets.
- Re-run detection every time Settings is constructed. Show found/not-found,
  resolved path as a tooltip, prerequisite text, discovered models, and Codex
  default label.
- Add `Use installed AI CLIs as models`, default off. Turning it on shows a
  confirmation that prompts leave the device and spend subscription usage;
  cancel restores off. While off, the model list is empty/disabled and direct
  calls remain blocked in the backend.
- Add a detected-model combobox and reasoning-effort combobox.
- Add `Allow automatic spell-check with AI CLI`, default off and disabled while
  the main CLI opt-in is off. Turning it on always shows the separate repeated-
  usage warning; cancel restores off.
- Extend save/connect, change detection, backend-panel switching, setup/status
  copy, and startup validation for `backend_mode="cli"`.
- Add the immediate standalone-chat waiting placeholder.
- Review gate: render/open Settings tests and inspect all CLI enabled/disabled,
  found/missing, and warning-cancel states before real model calls.

### Stage 5: verification

Run free checks first and stop at the first failure:

1. Existing full test runner, import checks, compile checks, prompt tests, and
   settings UI test.
2. Detection with present and absent shims, including `.cmd` selection and
   resolved-path display.
3. Codex cache discovery/default label and malformed/missing-cache fallback.
4. Fresh/default config: both CLI toggles false, CLI model unavailable, forced
   call rejected without a spawned process.
5. Warning cancellation: canceling either warning leaves its setting false.
6. Automatic-path safety: select CLI, leave CLI spell-check off, type a trigger
   sentence, and verify zero CLI subprocesses. Manual hotkeys and Chat remain
   available.
7. Failover safety: force cloud favorite failover and verify no CLI candidate
   can be selected; force a CLI failure and verify no fallback occurs.
8. Waiting states and one-chunk delivery for every streaming consumer.
9. Timeout cleanup with a mocked/short-lived fake CLI; no zombie process or
   leftover output/temp directory.

The guide's workspace-awareness test is intentionally skipped because file
tools are disabled and Vedium has no workspace.

Real-use tests each spend subscription usage and require fresh approval at
Stage 5 before they are run, once each:

1. Claude Haiku exact `pong`, with served-model provenance.
2. Claude Sonnet trivial request, proving alias-based `modelUsage` selection.
3. Two-turn standalone chat memory via transcript replay.
4. Codex default exact `pong`, if still installed and signed in.
5. One low-effort request and provenance verification.
6. One explicitly enabled CLI spell-check request after accepting the warning;
   then turn the setting off again and confirm a second sentence spawns no CLI.

## Acceptance differences from the generic guide

- Claude file tools are deliberately empty, and the workspace-awareness manual
  test is omitted, because Vedium has no user project folder.
- Provenance is never appended to text that can replace selected/typed content;
  it is shown separately. Standalone Chat can use the footer verbatim.
- The CLI mechanics remain in one dependency-free module, while the Tk widgets
  stay in Vedium's existing centralized UI module.
- CLI automatic spell-checking has a stricter second consent gate beyond the
  guide's general CLI opt-in.

