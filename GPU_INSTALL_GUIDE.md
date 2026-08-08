# Vedium — local GGUF engine guide

Vedium runs local `.gguf` models through a managed `llama-server` process. This
keeps model-architecture support separate from Python and avoids Windows native
wheel, CUDA Toolkit, and CPU-instruction compatibility traps.

## Installer (recommended)

Run the Vedium installer and choose the NVIDIA GPU or CPU engine:

- The NVIDIA choice downloads the pinned llama.cpp CUDA build plus its matching
  CUDA runtime DLLs. A compatible NVIDIA driver is required; the CUDA Toolkit
  itself is not.
- The CPU choice works on machines without a compatible NVIDIA GPU.
- Skip leaves local GGUF loading unavailable but keeps cloud, CLI, and manually
  configured server modes usable.

Reinstalling into the same directory is incremental. Vedium verifies and reuses
the existing versioned server and compatible private Python bundle. It downloads
an engine again only when the pinned build changes, its files are missing, or
you perform a true uninstall.

## Choose and load a model

The installer offers two models it can download and select automatically:

- **Gemma 4 E4B instruct Q4_K_M** (~5.0 GB) is the best-quality choice,
  particularly for explanations and reading tasks. It is recommended by
  default on compatible NVIDIA systems. An 8 GB GPU is a practical fit for
  typical use, and llama.cpp can use system RAM if full offload does not fit.
- **Gemma 3 4B instruct Q4_K_M** (~2.5 GB) is the lightweight choice for
  CPU-only or lower-memory systems.

You can choose either model regardless of the recommendation, skip the model
download, or load another compatible GGUF later. **Both recommended models run
on the CPU and NVIDIA GPU engines**; CPU generation is simply slower. The
current managed engine also supports newer architectures such as Qwen3.5,
subject to the model fitting your system memory.

In Vedium, open **Settings** and:

1. Browse to the `.gguf` file under **Model (.gguf)**.
2. Leave **Prompt format** on **Auto**.
3. Leave **GPU acceleration** on **Auto** unless you intentionally want CPU.
4. Click **Save & Load Model**.

Vedium starts its private localhost server automatically, loads the selected
GGUF, and stops that child process when you switch backend or exit. You do not
need to enter a server URL. The separate manual llama-server backend still
exists for users who run their own server.

## Running Vedium from source

Download a current Windows binary release from the official llama.cpp releases.
Extract the complete distribution, not only `llama-server.exe`, because it needs
the adjacent llama/ggml libraries. For CUDA builds, extract the matching CUDA
runtime archive into the same directory.

Use either layout:

```
<repo>\llama-server\cuda\llama-server.exe
<repo>\llama-server\cpu\llama-server.exe
```

Or point Vedium to any complete distribution:

```powershell
$env:VEDIUM_LLAMA_SERVER = 'D:\path\to\llama-server.exe'
py -3.12 -m vedium
```

## Troubleshooting

### `unknown model architecture`

Re-run the installer and install the managed engine, or point
`VEDIUM_LLAMA_SERVER` at a current llama-server distribution.

### Model does not fit or loading is very slow

Architecture support and hardware capacity are different. Use a smaller model
or quantization. Auto mode lets llama.cpp fit as many layers as practical on the
GPU and offload the rest to system RAM, but a GGUF much larger than available
RAM/VRAM will still be slow or fail.

### GPU was not detected by the installer

Run `nvidia-smi`. If it is missing or reports an old driver, update the NVIDIA
driver and rerun setup. You do not need `nvcc` or a separately installed CUDA
Toolkit because the installer includes the matching runtime files.

### `Failed to load model from file`

Verify that the path still exists and the `.gguf` download completed. The local
server log is `managed-server.log` in the Vedium directory and contains the
exact llama.cpp loading error.
