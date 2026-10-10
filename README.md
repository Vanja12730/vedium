# Vedium

A Windows tray assistant that helps you understand what you read and improve
what you write. Select text in your application and use a hotkey to explore
its meaning, ask follow-up questions, or revise your writing. It can run
**fully offline** with a local AI model, or use a cloud API / CLI agent you
already have.

## Demo

A quick look at Vedium.

https://github.com/user-attachments/assets/e0426ea2-732f-44ae-a85e-e7de1eb2324a

[Download the demo video](docs/demo/vedium-demo.mp4?raw=true)

## Features

- **Understand what you read** — select a passage to get its main idea, key
  takeaways, a plain-language explanation, or an explanation with supporting quotes.
- **Explore unfamiliar words and ideas** — define a word or phrase, then ask
  follow-up questions about the selected text and its explanation.
- **Improve your writing** — polish a draft, adjust its tone, make it more
  concise or detailed, simplify the wording, or sharpen a thesis.
- **Correct spelling and grammar** — fix recently typed text with a hotkey
  and review the changes before replacing it.
- **Give your text structure** — turn selected text into bullet points,
  numbered lists, checklists, Markdown tables, or paragraphs with headings.
- **Translate while you work** — use built-in translation modes for English,
  Bosnian/Croatian/Serbian, and Chinese.
- **Keep a conversation going** — use the chat window for longer exchanges.
- Save personal reflections to a selectable local plain-text or Markdown journal, grouped by source
- Save editable `Define This` results to a local plain-text or Markdown vocabulary journal, grouped by term
- New journals default to TXT; existing Markdown journals and chosen paths are preserved.
  Use **Change…** in either save dialog to choose TXT or Markdown for that journal.
- Toast popups that show a diff of what was changed
- Four interchangeable backends:
  - **Local model** (GGUF via Vedium's managed llama-server) — no internet needed
  - **llama-server** — point it at a server you already run
  - **Cloud API** — any OpenAI-compatible endpoint (e.g. Google Gemini)
  - **CLI agents** — Claude Code, Codex, or Antigravity, if installed
- Everything configured from the tray icon — right-click → Settings

## Install

Two ways in. If you already have Python, **installing from source is two
commands and Windows raises no SmartScreen warning**. If you would rather not
deal with Python at all, the installer bundles everything for you — but
Windows will warn you about it once, because it is not code-signed.

### Option 1 — From source (no SmartScreen warning)

Requires Python 3.10+ from [python.org](https://www.python.org/downloads/)
(3.12 recommended; tkinter is included in the python.org installer).

```
git clone https://github.com/Vanja12730/vedium.git
cd vedium
py -3.12 -m pip install -r requirements.txt
py -3.12 -m vedium
```

Or double-click `run.vbs` to start it without a console window.

Nothing here trips SmartScreen: you are running your own checkout with your
own Python, so there is no unrecognised downloaded executable for Windows to
quarantine.

To use a **local model** from a source checkout, put a current
`llama-server.exe` distribution in `llama-server\cuda` or
`llama-server\cpu`, or set the `VEDIUM_LLAMA_SERVER` environment variable to
its full path. See [GPU_INSTALL_GUIDE.md](GPU_INSTALL_GUIDE.md). Cloud API and
CLI backends need nothing extra.

### Option 2 — Installer (no Python needed)

1. Download `VediumSetup_v*.exe` from the
   [latest release](../../releases/latest).
2. Run it. **Windows SmartScreen will warn you** ("Windows protected your
   PC") because the installer is not code-signed — click **More info →
   Run anyway**. The installer is built from exactly the code in this
   repository; you can audit it or build it yourself (see below), or use
   Option 1 above and avoid the warning entirely.
3. Follow the wizard. It will:
   - install Vedium with its own **private Python** — it never touches or
     even looks at any Python you may have installed;
   - ask which managed llama-server build you want (it detects NVIDIA CUDA
     and recommends GPU or CPU — or choose *Skip* for cloud / CLI only);
   - offer a choice of two tested local models (~2.5 or ~5.0 GB, optional).
4. Double-click the **Vedium** shortcut on your Desktop. A tray icon
   appears — right-click it to open Settings.

No Python, no pip, no PATH changes — the install is fully self-contained.
It installs into your user profile, so there is no administrator prompt.

Uninstalling from *Add/Remove Programs* removes the app, its private Python,
the local engine, any model downloaded into the install folder, and your
settings, including custom modes and hotkeys. Two things stay behind:

- **Saved API keys** stay in Windows Credential Manager, so a later reinstall
  picks them up. To remove them, open **Credential Manager → Windows
  Credentials** and remove each `Vedium/cloud_api_key/<provider>` entry. This
  deletes only Vedium's saved copy: the key keeps working with its provider.
  Credential Manager does not show the key itself, so keep your own copy if
  you want to use it elsewhere.
- **Your notes**, `Reflections.txt` and `Vocabulary.txt` (or `.md`), stay in
  `Documents\Vedium` or the folder you chose for them.

Reinstalling or upgrading into the same directory is incremental: Vedium
preserves a compatible private Python environment, reuses an already-working
versioned local engine, keeps the existing model/backend configuration, and
caches downloaded engine archives. A new engine is fetched only when its pinned
llama.cpp build changes; a clean Python replacement happens only when the
installer declares an incompatible private-runtime version.

### Is the installer safe?

The first time you run the installer, Windows shows **"Windows protected your
PC"**. That is Microsoft Defender SmartScreen reporting an *unrecognised* app:
the installer is not code-signed and has not yet been downloaded by many
people. It does not mean a virus was found — when Microsoft Defender detects
malware, it blocks the file and shows a threat alert.

You can check the installer yourself instead of taking that on trust:

- **Compare the checksum.** Each release lists the installer's SHA-256. In
  PowerShell, in the folder you downloaded it to, run
  `Get-FileHash .\VediumSetup_v*.exe` and compare the result. A match proves
  the file is exactly the one published here.
- **Scan it.** Upload it to [VirusTotal](https://www.virustotal.com) to check
  it with dozens of antivirus engines at once. Unsigned installers sometimes
  get a false positive from one or two lesser-known engines.
- **Install from source** with Option 1 above: no installer, no warning, and
  every line of code is readable.
- **Build the installer yourself**, as described in
  [Building the installer yourself](#building-the-installer-yourself).

## Optional CLI backends

Vedium can use an AI CLI that is already installed and signed in. You only
need **one** of the following. On Windows, open PowerShell and run the relevant
official installer:

### Codex CLI

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://chatgpt.com/codex/install.ps1 | iex"
codex
```

The first run asks you to sign in. See the [Codex CLI guide](https://learn.chatgpt.com/docs/codex/cli).

### Google Antigravity CLI (`agy`)

```powershell
irm https://antigravity.google/cli/install.ps1 | iex
agy
```

The first run walks you through Google sign-in. See the
[Antigravity CLI guide](https://antigravity.google/docs/cli/getting-started).

### Claude Code

```powershell
irm https://claude.ai/install.ps1 | iex
claude
```

The first run asks you to sign in. See Anthropic's
[Claude Code installation guide](https://support.claude.com/en/articles/14552382-your-first-day-in-claude-code).

After installing a CLI, restart Vedium so it sees the updated `PATH`. Then open
**Settings -> Model**, choose **AI CLI**, enable *Use installed AI CLIs as
models*, select a detected model, and click **Save & Load Model**.

### Free-tier status (checked 2026-08-08)

- **Codex:** yes. ChatGPT Free includes limited Codex usage, including the CLI.
  Signing in with an API key is separate and billed at API rates. See
  [Codex pricing](https://learn.chatgpt.com/docs/pricing).
- **Antigravity:** yes. Google's Individual plan is $0/month and includes the
  CLI with basic weekly limits. See
  [Antigravity pricing](https://antigravity.google/pricing).
- **Claude Code:** no free CLI tier. Claude has a free chat plan, but Claude
  Code is included only in paid Claude plans; API and third-party-provider use
  may be billed separately. See [Claude pricing](https://claude.com/pricing).

Plans and limits change, so treat this dated snapshot as a quick guide and
check the linked provider pages before signing up.

## Choosing a model (local backend)

The installer offers two tested recommendations:

- **Gemma 4 E4B instruct, Q4_K_M** (~5.0 GB) — the best-quality choice,
  particularly for explanations and reading tasks. It is the default when a
  compatible NVIDIA system is detected. An 8 GB GPU is a practical fit for
  typical use: `lmstudio-community/gemma-4-E4B-it-GGUF`.
- **Gemma 3 4B instruct, Q4_K_M** (~2.5 GB) — the smaller, faster choice for
  CPU-only and lower-memory PCs: `unsloth/gemma-3-4b-it-GGUF`.

Either model can be selected on any supported PC, or the download can be
skipped. **Both models run on CPU as well as NVIDIA GPU**; GPU acceleration is
faster, but is not required. Architecture support does not guarantee that a
large model will fit comfortably in your RAM/VRAM. If full GPU offload is not
possible, llama.cpp can place some work in system RAM at reduced speed.

Settings → Browse next to *"Model (.gguf)"* → Save & Load Model. Leave
*Prompt format* on Auto.

## Privacy

Read this before using Vedium — it is a keyboard tool, and you should know
exactly what it does:

- **Vedium uses a global keyboard listener** (via `pynput`) to keep a short
  buffer of your recent keystrokes. That is what lets it correct the text
  you just typed. The buffer lives in memory only; it is never written to
  disk and never leaves your machine on its own. It is erased before Vedium
  accepts typing from a different foreground window and immediately when you
  disable Vedium from the tray. Capture also resets when the focused field
  changes, on Tab, and on document clicks/shortcuts. Password fields are
  excluded. Automatic capture pauses if Windows accessibility cannot confirm
  a supported, non-password text field; unsupported editors may require an
  explicit selection and manual copying of the result.
- **Replacement:** Vedium verifies the source selection, the focused field,
  and the active window before pasting. If the source changed, focus cannot be
  verified, or the selection cannot be copied back, replacement is refused.
- **Local model backend:** nothing is ever sent anywhere. The model runs on
  your machine and works with the internet unplugged.
- **Cloud API backend:** the text being corrected (and only that text) is
  sent to the provider you configured (e.g. Google). Their data policies
  apply to what you send.
- **CLI backend:** the text is passed to the CLI agent you selected
  (Claude Code / Codex / Antigravity), which sends it to its provider under
  your existing account and its policies.
- **API keys** are stored in the Windows Credential Manager, not in the
  config file. They remain there after an uninstall until you remove them
  (see [Install](#install)).
- Vedium itself has **no telemetry, no analytics, no auto-update** — it
  only talks to the network when the backend you chose requires it.

## Building the installer yourself

1. Install [Inno Setup 6](https://jrsoftware.org/isinfo.php).
2. Build the portable Python bundle (once, and after dependency changes):
   `py -3.12 make_python_bundle.py`
3. Compile `Vedium.iss` (right-click → Compile). The installer appears in
   `Output\`.

When changing the bundled Python version or its preinstalled runtime packages,
bump `PythonBundleVersion` in `Vedium.iss`. Do not bump it for ordinary Vedium
code changes. To ship a newer local engine, bump `ServerBuild` in
`CheckManagedLlamaServer`; a same-version reinstall reuses what is already there.

## License

Copyright (C) 2026 vehab. [GNU General Public License v3.0](LICENSE).

Vedium is free software under GPL-3.0. You may use, study, modify, and share it,
including commercially. If you distribute Vedium or a modified version, you
must provide the corresponding source under the GPL and preserve the same
freedoms for its recipients. This keeps Vedium open for others to learn from
and improve.

Vedium bundles third-party components under their own (GPL-compatible)
licenses. See [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

### Commercial licensing

Organizations that want to incorporate Vedium into software distributed under
terms incompatible with GPL-3.0 may request a separate commercial licence from
the copyright holder. This option does not remove or restrict the GPL rights of
any public Vedium release. A commercial licence covers only Vedium-owned code;
third-party components and model weights remain subject to their own terms.

Contributions are accepted under terms that preserve both the public GPL
release and this commercial-licensing option. See
[CONTRIBUTING.md](CONTRIBUTING.md) before submitting code.

## Support

Vedium is free. If it's useful to you, you can support its development:

[![Support me on Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/fortunadigitas)
