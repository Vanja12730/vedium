# Vedium — GPU Install Guide

GPU-accelerated local models on Windows with llama-cpp-python.

> Tested configuration: Windows 10/11, Python 3.12, CUDA 12.8,
> llama-cpp-python 0.3.16 (GPU wheel), NVIDIA RTX 4060.
> Qwen3-4B-Instruct-2507-Q4_K_M confirmed working with full GPU offload.

The easiest way to get the GPU build is to let the **installer** do it — it
detects your GPU and offers the GPU engine automatically. Use this guide if
you run Vedium **from source**, chose *Skip* during install, or need to fix
a broken engine.

## Which pip?

Every `pip` command below must target the Python that actually runs Vedium:

- **Installed via the installer** (Vedium ships its own private Python):

  ```
  "%LOCALAPPDATA%\Vedium\python\python.exe" -m pip <command>
  ```

- **Running from source:**

  ```
  py -3.12 -m pip <command>
  ```

## Prerequisites

- An NVIDIA GPU with CUDA 12.x drivers — check with `nvidia-smi`
- CUDA Toolkit 12.8 — verify with `nvcc --version`; if missing, download it
  from <https://developer.nvidia.com/cuda-12-8-0-download-archive>
- Python 3.12 (from-source setups only — the installer bundles its own)
- A GGUF model file (see below)

## Step 1 — Install the GPU wheel

Download the pre-built GPU wheel (~226 MB):

```
curl -L -o llama_cpp_python-0.3.16-cp312-cp312-win_amd64.whl "https://huggingface.co/boneylizardwizard/llama_cpp_python-0.3.16-cp312-cp312-win_amd64/resolve/main/llama_cpp_python-0.3.16-cp312-cp312-win_amd64.whl"
```

Install it (using the right pip from above):

```
pip install llama_cpp_python-0.3.16-cp312-cp312-win_amd64.whl
```

> ℹ This wheel is a community build by *boneylizardwizard*, not an official
> abetlen release — the official repo stopped shipping Windows GPU wheels
> after v0.3.4. It is llama-cpp-python 0.3.16 built for CUDA 12.8 and is
> confirmed to support Qwen3 and Gemma 3 models on Windows. It requires
> Python 3.12 (`cp312`).

## Step 2 — Get a model

Confirmed working GGUF models (2–3 GB each) on Hugging Face:

- **Gemma 3 4B** — `unsloth/gemma-3-4b-it-GGUF` (the installer's default)
  or `lmstudio-community/gemma-3-4b-it-GGUF`
- **Qwen3 4B** — `chaparro2001/Qwen3-4B-Instruct-2507-Q4_K_M-GGUF`

> ℹ Make sure the download completes fully — a partial file causes a
> loading error even though the file appears to exist. Gemma 4 is **not**
> supported by these engine builds.

## Step 3 — Verify the GPU works

Confirm the model loads on the GPU before pointing Vedium at it (replace
the path and use the right python from above):

```
python -c "from llama_cpp import Llama; Llama(model_path=r'D:\models\your-model.gguf', n_ctx=384, n_gpu_layers=-1, verbose=True); print('GPU OK')"
```

Look for two things in the output:

- a line mentioning **CUDA0** and your GPU name — the GPU was detected
- **GPU OK** printed at the end — the model loaded successfully

## Step 4 — Point Vedium at the model

Start Vedium (Desktop shortcut, or `py -3.12 -m vedium` from source),
right-click the tray icon → **Settings**:

- **Model (.gguf)** — Browse to your model file
- **Prompt format** — leave on *Auto* (or pick *ChatML* for Qwen3 /
  *Gemma* for Gemma 3 manually)
- **GPU acceleration** — *Auto* or *Full*

Click **Save & Load Model**.

## If something breaks — rollback to the CPU build

Two commands restore a known-good state (right pip from above):

```
pip uninstall llama-cpp-python -y
pip install llama-cpp-python==0.3.19 --only-binary llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
```

**Important:** after rolling back, set *GPU acceleration* to **Off** in
Settings. Nothing breaks if you forget, but a harmless warning is logged
on startup.

## Troubleshooting

### `unknown model architecture: 'qwen3'`

The installed llama-cpp-python is too old for Qwen3. Redo Step 1 with the
exact wheel URL above.

### `Failed to load model from file`

Wrong model path in Settings, or an incomplete download. Check the file
exists and is roughly 2–3 GB; re-download if it is smaller.

### GPU not detected — falling back to CPU

CUDA Toolkit missing or drivers outdated — run `nvcc --version` to confirm
CUDA 12.8 is present. Also check *GPU acceleration* is *Auto* or *Full* in
Settings, not *Off*.

### CMAKE error during install

pip is compiling from source instead of using the pre-built wheel. Install
from the `.whl` file directly (Step 1) — never plain
`pip install llama-cpp-python`.

### Tray icon does not appear

A dependency is missing: `pip install pystray pillow pynput`
(from-source setups only — the installer always ships these).
