# Vedium

A Windows tray assistant for reading and writing. Vedium watches for your
hotkey, fixes or rephrases the text you just typed — in any application — and
shows you exactly what changed. It can run **fully offline** with a local AI
model, or use a cloud API / CLI agent you already have.

## Features

- Fix spelling and grammar of recently typed text with a hotkey, in any app
- Rephrase modes (tone, clarity) and a small chat window
- Toast popups that show a diff of what was changed
- Four interchangeable backends:
  - **Local model** (GGUF via llama-cpp-python) — no internet needed
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
   - ask which AI engine build you want (it detects your hardware and
     pre-selects the right one — or choose *Skip* if you'll use a cloud
     API or CLI agent);
   - offer to download the recommended local model (~2.5 GB, optional).
4. Double-click the **Vedium** shortcut on your Desktop. A tray icon
   appears — right-click it to open Settings.

No Python, no pip, no PATH changes — the install is fully self-contained,
and uninstalling from *Add/Remove Programs* removes everything.

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

To use a **local model** you also need llama-cpp-python — see the
commented instructions in [requirements.txt](requirements.txt) (plain
`pip install llama-cpp-python` will not work on Windows), or
[GPU_INSTALL_GUIDE.md](GPU_INSTALL_GUIDE.md) for the NVIDIA GPU build.
Cloud API and CLI backends need nothing extra.

## Choosing a model (local backend)

Recommended: **Gemma 3 4B instruct, Q4_K_M** (~2.5 GB) —
`unsloth/gemma-3-4b-it-GGUF` on Hugging Face. The installer can download
it for you. Qwen3 4B also works well.

> **Note:** Gemma 4 is *not* supported by the pinned engine builds — use
> Gemma 3.

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

## License

To be added before the first public release.
