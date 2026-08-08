# Vedium

A Windows tray assistant for reading and writing. Vedium watches for your
hotkey, fixes or rephrases the text you just typed — in any application — and
shows you exactly what changed. It can run **fully offline** with a local AI
model, or use a cloud API / CLI agent you already have.

## Features

- Fix spelling and grammar of recently typed text with a hotkey, in any app
- Rephrase modes (tone, clarity) and a small chat window
- Save personal reflections to a selectable local Markdown journal, grouped by source
- Save editable `Define This` results to a local vocabulary journal, grouped by term
- Toast popups that show a diff of what was changed
- Four interchangeable backends:
  - **Local model** (GGUF via Vedium's managed llama-server) — no internet needed
  - **llama-server** — point it at a server you already run
  - **Cloud API** — any OpenAI-compatible endpoint (e.g. Google Gemini)
  - **CLI agents** — Claude Code, Codex, or Antigravity, if installed
- Everything configured from the tray icon — right-click → Settings

## Install

### Option 1 — Installer (recommended)

1. Download `VediumSetup_v*.exe` from the
   [latest release](../../releases/latest).
2. Run it. **Windows SmartScreen will warn you** ("Windows protected your
   PC") because the installer is not code-signed — click **More info →
   Run anyway**. The installer is built from exactly the code in this
   repository; you can audit it or build it yourself (see below).
3. Follow the wizard. It will:
   - install Vedium with its own **private Python** — it never touches or
     even looks at any Python you may have installed;
   - ask which managed llama-server build you want (it detects NVIDIA CUDA
     and recommends GPU or CPU — or choose *Skip* for cloud / CLI only);
   - offer a choice of two tested local models (~2.5 or ~5.0 GB, optional).
4. Double-click the **Vedium** shortcut on your Desktop. A tray icon
   appears — right-click it to open Settings.

No Python, no pip, no PATH changes — the install is fully self-contained,
and uninstalling from *Add/Remove Programs* removes everything.

Reinstalling or upgrading into the same directory is incremental: Vedium
preserves a compatible private Python environment, reuses an already-working
versioned local engine, keeps the existing model/backend configuration, and
caches downloaded engine archives. A new engine is fetched only when its pinned
llama.cpp build changes; a clean Python replacement happens only when the
installer declares an incompatible private-runtime version.

### Option 2 — From source

Requires Python 3.10+ from [python.org](https://www.python.org/downloads/)
(3.12 recommended; tkinter is included in the python.org installer).

```
git clone <this-repo-url>
cd vedium
py -3.12 -m pip install -r requirements.txt
py -3.12 -m vedium
```

Or double-click `run.vbs` to start it without a console window.

To use a **local model** from a source checkout, put a current
`llama-server.exe` distribution in `llama-server\cuda` or
`llama-server\cpu`, or set the `VEDIUM_LLAMA_SERVER` environment variable to
its full path. See [GPU_INSTALL_GUIDE.md](GPU_INSTALL_GUIDE.md). Cloud API and
CLI backends need nothing extra.

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
  disk and never leaves your machine on its own.
- **Local model backend:** nothing is ever sent anywhere. The model runs on
  your machine and works with the internet unplugged.
- **Cloud API backend:** the text being corrected (and only that text) is
  sent to the provider you configured (e.g. Google). Their data policies
  apply to what you send.
- **CLI backend:** the text is passed to the CLI agent you selected
  (Claude Code / Codex / Antigravity), which sends it to its provider under
  your existing account and its policies.
- **API keys** are stored in the Windows Credential Manager, not in the
  config file.
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
