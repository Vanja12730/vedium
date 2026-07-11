# Product Finish Plan - Vedium (formerly GrammarAnywhere / Spell Daemon)

This plan is aimed at turning the app from a capable power-user utility into a finished product: reliable, understandable, polished, installable, and easy to recover when something goes wrong.

The app already has a strong foundation: cross-app correction, tray workflow, review toasts, selected-text rephrasing, custom modes, cloud/local/server backends, safety gates, and broad manual test coverage. The remaining work is less about inventing features and more about trust, clarity, polish, packaging, and maintainability.

## Product North Star

The app should feel like a quiet writing assistant that works anywhere:

- It stays out of the way while the user types.
- It is obvious when it is enabled, connected, and ready.
- It never silently destroys text.
- It explains failures in plain language.
- It can be installed, updated, configured, and removed cleanly.
- Its main value is available within the first minute of use.

## Stage 1 - Stabilize The Core Experience

Goal: make the current app predictable and trustworthy before expanding features.

### 1.1 Add a visible status surface

Create a simple status panel in Settings, and optionally expose the same status from the tray menu.

It should show:

- App enabled or disabled.
- Spell-check mode: Auto, Toast, or Off.
- Active backend: Cloud, Server, Local, or None.
- Model/provider name.
- Last successful action.
- Last error, if any.
- Whether a correction/rephrase is currently running.

Why this matters: when a background app does nothing, the user needs to know whether it is disabled, disconnected, waiting on the model, blocked by focus, or intentionally skipping unsafe output.

### 1.2 Replace silent failures with user-visible diagnostics

Keep the daemon resilient, but stop swallowing important errors completely.

Add a lightweight diagnostics module that records:

- Timestamp.
- Component: buffer, replacement, LLM, UI, config, backend, clipboard.
- Severity: info, warning, error.
- Short user-facing message.
- Optional technical detail for debugging.

Use this for failures currently hidden behind broad exception handlers, especially in:

- UI callback dispatch.
- Backend connection.
- Cloud API calls.
- Clipboard access.
- Keyboard replacement.
- Toast creation.
- Streaming cancellation.

Output should go to an in-memory ring buffer first. Persisting logs can be added later.

### 1.3 Make destructive replacement paths extra conservative

The current safety gates are already thoughtful. Add final guardrails around actual replacement:

- Never replace with empty text.
- Never replace if the target app focus appears to have changed during review.
- In Toast mode, show "Copy" as a fallback when replacement is risky.
- If clipboard restore fails, show a warning.
- If selection copy returns unusually large text, ask for confirmation before rephrasing.

### 1.4 Confirm the default configuration

Review the default settings for a fresh user:

- Spell check should probably default to Toast, not Auto, until trust is established.
- Working language should default to Auto unless there is a product-specific reason.
- Backend setup should guide the user into Cloud, Server, or Local clearly.
- If no backend is configured, first launch should open Settings with a clear setup state.

Deliverable for Stage 1:

- A status/diagnostics surface.
- Fewer silent failures.
- Safer final replacement behavior.
- A fresh install that clearly tells the user what to do next.

## Stage 2 - First-Run And Setup Polish

Goal: make the app understandable within the first minute.

### 2.1 Add first-run onboarding inside Settings

Do not build a marketing page. Build a compact setup flow inside the app:

- Choose backend: Cloud, Local Model, or Local Server.
- Configure provider/model/API key or local model path.
- Test connection.
- Choose default behavior: Review suggestions or replace automatically.
- Choose language behavior: Auto, English, BHS, etc.
- Finish with a test sentence.

The user should leave setup knowing the app is ready.

### 2.2 Add connection testing for every backend

Each backend should have a "Test" action that gives a plain result:

- Connected.
- API key missing.
- Model unavailable.
- Server unreachable.
- Local model file missing.
- Context too small.
- Provider rejected the request.

Cloud errors should stay human-readable. The existing friendly cloud error work is a good base.

### 2.3 Clarify API key handling

Current config intentionally avoids storing cloud secrets, which is good for safety but confusing if the UI accepts keys that disappear after save.

Pick one product policy:

- Safer option: environment variable only, with very clear UI copy.
- Convenient option: store using Windows Credential Manager or OS keychain.
- Simple beta option: allow local encrypted-ish storage with a strong warning.

Recommended: Windows Credential Manager / OS keychain for a finished Windows product.

Deliverable for Stage 2:

- A guided first-run setup.
- Clear backend test results.
- A settled API key storage policy.
- No confusing "saved but not really saved" behavior.

## Stage 3 - Toast And Interaction Finish

Goal: make the main interaction feel finished, fast, and safe.

### 3.1 Finish toast polish

The toast is the product's face. Prioritize:

- Fade in/out.
- Consistent hover states on buttons.
- Clear keyboard hints: Replace with Ctrl+Enter, Ignore with Esc.
- Better spacing and typography.
- Diff view that remains readable for longer sentences.
- A compact "something went wrong" toast for failed model calls.

### 3.2 Improve replacement confidence

When a suggestion appears, the user should understand what will happen:

- Show the original/corrected difference clearly.
- Let users copy instead of replace.
- Keep manual editing easy.
- Disable Replace while streaming.
- Show when text was not changed because it looked unsafe.

### 3.3 Add a small correction history

The app already has recent rephrases in memory. Expand this into a small local history UI:

- Last corrections/rephrases.
- Original text.
- Suggested text.
- Mode used.
- Copy action.
- No sensitive persistence by default.

Make persistence optional later.

Deliverable for Stage 3:

- Toasts that feel polished and understandable.
- Better failure feedback.
- Safer, clearer review-and-replace behavior.

## Stage 4 - Settings Simplification

Goal: keep power while reducing visual and cognitive load.

### 4.1 Reorganize Settings around user intent

Suggested tabs:

- Status
- Writing
- Rephrase Modes
- Backend
- Appearance
- Advanced

Move rarely changed options into Advanced:

- Prompt format.
- Context size.
- GPU settings.
- Server thinking toggle.
- Trigger characters.
- Idle reset.
- Custom prompt overrides.

### 4.2 Add mode management polish

Custom modes are a major differentiator. Make them feel first-class:

- Duplicate a built-in mode.
- Reset a built-in prompt to default.
- Test a mode on sample text.
- Assign trigger prefix.
- Assign hotkey.
- Warn on hotkey conflicts.

### 4.3 Improve wording

Use plain labels where possible:

- "Review suggestions" instead of "Toast mode".
- "Replace automatically" instead of "Auto".
- "Writing language" instead of "Working language".
- "AI provider" instead of "Cloud backend".

Keep internal terms in tooltips or Advanced.

Deliverable for Stage 4:

- Settings that feel like a product, not a control panel.
- Power features still available, but no longer visually dominant.

## Stage 5 - Packaging And Release Readiness

Goal: make the app installable and supportable.

### 5.1 Create a repeatable build process

Document and script the release build:

- Clean build.
- Version number.
- Icon.
- Installer creation.
- Smoke test after install.
- Uninstall test.

The existing Inno Setup file is a good start. Turn it into a release checklist.

### 5.2 Add app versioning

Add a single version constant and show it in:

- Settings.
- Tray About item.
- Installer metadata.
- Logs/diagnostics.

### 5.3 Add crash and diagnostics export

Create "Export diagnostics" in Settings:

- App version.
- OS.
- Backend mode.
- Provider/model name, without secrets.
- Recent diagnostic events.
- Config with secrets stripped.

This makes support possible without asking users to dig through files.

### 5.4 Signpost privacy clearly

Add a short privacy section in Settings:

- What text is sent to the model.
- Whether text is stored.
- Whether API keys are stored.
- How clipboard is used.
- How to disable the app.

This app touches user writing globally, so privacy clarity is not optional.

Deliverable for Stage 5:

- Repeatable installer build.
- Versioned releases.
- Diagnostics export.
- Clear privacy behavior.

## Stage 6 - Maintainability Refactor

Goal: make the code easier to change without breaking fragile behavior.

Do this after the product behavior is stable, not before.

### 6.1 Split large modules by responsibility

Suggested split:

- `ui_settings.py`
  - `settings_window.py`
  - `settings_backend_tab.py`
  - `settings_modes_tab.py`
  - `settings_appearance_tab.py`
  - `settings_widgets.py`

- `ui_toasts.py`
  - `toast_base.py`
  - `correction_toast.py`
  - `editable_toast.py`
  - `recent_rephrases.py`

- `llm.py`
  - `llm_backends.py`
  - `llm_cloud.py`
  - `llm_local.py`
  - `llm_server.py`
  - `llm_safety.py`
  - `llm_streaming.py`

Keep public call signatures stable while moving code.

### 6.2 Add focused unit tests

Good test candidates:

- Buffer state transitions.
- Trigger detection.
- Prefix trigger resolution.
- Prompt building.
- LLM output cleanup.
- Safety gates.
- Config loading and migration.
- Backend error formatting.

### 6.3 Add integration smoke tests

Keep these small and practical:

- Launch Settings.
- Save config.
- Create a toast.
- Accept toast callback.
- Simulate backend unavailable.
- Verify secrets are stripped from config.

Deliverable for Stage 6:

- Smaller modules.
- More automated confidence.
- Less fear when editing UI or backend behavior.

## Stage 7 - Beta Release Loop

Goal: learn from real usage without destabilizing the app.

### 7.1 Define beta scope

Recommended beta promise:

"A Windows tray assistant that reviews or rewrites text anywhere you type, using your chosen AI backend."

For beta, support:

- Windows first.
- Cloud backend first.
- Local server second.
- Local GGUF model as advanced.

### 7.2 Track feedback categories

Collect feedback into:

- Setup confusion.
- Suggestions are wrong.
- Replacement failed.
- App did nothing.
- UI unclear.
- Performance too slow.
- Backend/provider issue.
- Privacy concern.

### 7.3 Ship small release candidates

Release candidate checklist:

- Fresh install works.
- Backend setup works.
- Settings opens.
- Tray menu works.
- Toast review works.
- Selected text rephrase works.
- Clipboard restores.
- Quit works.
- Uninstall works.

Deliverable for Stage 7:

- A beta build that can be shared confidently.
- A feedback loop that points to product issues instead of random bug reports.

## Suggested Timeline

### Week 1

- Stage 1 diagnostics/status.
- Fresh config review.
- Key replacement guardrails.

### Week 2

- Stage 2 first-run setup.
- Backend test actions.
- API key policy decision.

### Week 3

- Stage 3 toast polish.
- Better visible error states.
- Correction/rephrase history improvements.

### Week 4

- Stage 4 settings simplification.
- Privacy/status/about surfaces.
- Start packaging cleanup.

### Week 5

- Stage 5 release process.
- Versioning.
- Diagnostics export.
- Installer smoke tests.

### Week 6+

- Stage 6 maintainability refactor.
- Stage 7 beta loop.
- Iterate based on real usage.

## Highest-Leverage Next Three Tasks

If only three things happen first, do these:

1. Add a Status tab with backend readiness, enabled state, last action, and last error.
2. Add backend test buttons with plain-language results.
3. Add diagnostics logging so failures stop disappearing.

Those three changes will make the app feel much closer to a finished product because they solve the biggest trust problem: the user always knows what state the app is in.

