# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Lifecycle management for Vedium's bundled llama.cpp server.

The bundled server is the preferred local-GGUF engine.  Keeping llama.cpp in a
separate process lets Vedium update model-architecture support independently of
its private Python runtime and avoids native Python wheel/CPU ABI problems on
Windows.
"""

from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import urllib.request


_LOCK = threading.RLock()
_PROCESS: subprocess.Popen | None = None
_LOG_HANDLE = None
_MODEL_PATH = ""
_SERVER_URL = ""
_EXECUTABLE = ""


class ManagedServerError(RuntimeError):
    """Raised when the bundled llama-server cannot be found or started."""


def _app_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _candidate_executables(configured_path: str = "") -> list[Path]:
    """Return managed-server candidates in explicit-to-development order."""
    root = _app_root()
    raw = [
        configured_path,
        os.environ.get("VEDIUM_LLAMA_SERVER", ""),
        str(root / "llama-server" / "cuda" / "llama-server.exe"),
        str(root / "llama-server" / "vulkan" / "llama-server.exe"),
        str(root / "llama-server" / "cpu" / "llama-server.exe"),
        str(root / "llama-server" / "llama-server.exe"),
        str(root / "engine" / "llama-server" / "llama-server.exe"),
        # Development checkout: the compatibility-tested server is kept in
        # build_cache and is ignored by git.  Installations use the first path.
        str(root / "build_cache" / "llama-b10327-cuda124" / "llama-server.exe"),
    ]
    result = []
    seen = set()
    for value in raw:
        if not value:
            continue
        path = Path(value).expanduser()
        key = os.path.normcase(os.path.abspath(str(path)))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def find_managed_server(configured_path: str = "") -> str:
    """Return the first available managed llama-server executable, or ``""``."""
    for candidate in _candidate_executables(configured_path):
        if candidate.is_file():
            return str(candidate.resolve())
    return ""


def _pick_port() -> int:
    """Ask Windows for an unused localhost port.

    There is a small bind/release race, so ``start_managed_server`` retries with
    a fresh port when the child exits before becoming healthy.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _health_ok(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=timeout) as response:
            if getattr(response, "status", 200) != 200:
                return False
            data = json.loads(response.read() or b"{}")
            return data.get("status") == "ok"
    except Exception:
        return False


def _tail(path: Path, max_chars: int = 6000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[-max_chars:].strip()
    except Exception:
        return ""


def is_managed_server_running() -> bool:
    with _LOCK:
        return bool(_PROCESS is not None and _PROCESS.poll() is None and _SERVER_URL)


def managed_server_url() -> str:
    with _LOCK:
        return _SERVER_URL if is_managed_server_running() else ""


def stop_managed_server() -> None:
    """Stop only the llama-server process launched by this Vedium process."""
    global _PROCESS, _LOG_HANDLE, _MODEL_PATH, _SERVER_URL, _EXECUTABLE
    with _LOCK:
        process = _PROCESS
        _PROCESS = None
        _MODEL_PATH = ""
        _SERVER_URL = ""
        _EXECUTABLE = ""
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=3)
                except Exception:
                    pass
        if _LOG_HANDLE is not None:
            try:
                _LOG_HANDLE.close()
            except Exception:
                pass
            _LOG_HANDLE = None


def start_managed_server(
    model_path: str,
    *,
    n_ctx: int = 4096,
    gpu_mode: str = "Auto",
    configured_path: str = "",
    startup_timeout: float = 120.0,
) -> str:
    """Start the bundled server for ``model_path`` and return its base URL.

    The server's own ``--fit`` logic handles VRAM-aware layer offload.  Auto is
    deliberately preferred over a home-grown layer count because modern hybrid
    architectures (Qwen3.5/3.6, MoE models) do not have uniform layer sizes.
    """
    global _PROCESS, _LOG_HANDLE, _MODEL_PATH, _SERVER_URL, _EXECUTABLE

    model = str(Path(model_path).resolve())
    if not Path(model).is_file():
        raise ManagedServerError(f"GGUF model file was not found: {model_path}")

    executable = find_managed_server(configured_path)
    if not executable:
        raise ManagedServerError(
            "The managed llama-server engine is not installed. Re-run the "
            "Vedium installer and choose a local CPU or GPU engine."
        )

    with _LOCK:
        if (
            _PROCESS is not None
            and _PROCESS.poll() is None
            and _MODEL_PATH == model
            and os.path.normcase(_EXECUTABLE) == os.path.normcase(executable)
            and _health_ok(_SERVER_URL)
        ):
            return _SERVER_URL

        stop_managed_server()

        root = _app_root()
        log_path = root / "managed-server.log"
        try:
            _LOG_HANDLE = open(log_path, "w", encoding="utf-8", errors="replace")
        except TypeError:
            # ``errors`` is not accepted by a few older Python file wrappers.
            _LOG_HANDLE = open(log_path, "w", encoding="utf-8")

        gpu = str(gpu_mode or "Auto").strip().lower()
        if gpu == "off":
            gpu_layers = "0"
            fit = "off"
        elif gpu == "full":
            gpu_layers = "all"
            fit = "off"
        else:
            gpu_layers = "auto"
            fit = "on"

        last_error = ""
        for _attempt in range(3):
            if _LOG_HANDLE is None:
                _LOG_HANDLE = open(
                    log_path, "a", encoding="utf-8", errors="replace"
                )
            port = _pick_port()
            url = f"http://127.0.0.1:{port}"
            args = [
                executable,
                "--model", model,
                "--host", "127.0.0.1",
                "--port", str(port),
                "--ctx-size", str(max(512, int(n_ctx))),
                "--parallel", "1",
                "--gpu-layers", gpu_layers,
                "--fit", fit,
                "--fit-target", "1536",
                "--jinja",
                "--no-webui",
            ]
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                process = subprocess.Popen(
                    args,
                    cwd=str(Path(executable).parent),
                    stdin=subprocess.DEVNULL,
                    stdout=_LOG_HANDLE,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
            except OSError as exc:
                stop_managed_server()
                raise ManagedServerError(
                    f"Could not start the managed llama-server engine: {exc}"
                ) from exc

            _PROCESS = process
            _MODEL_PATH = model
            _SERVER_URL = url
            _EXECUTABLE = executable

            deadline = time.monotonic() + max(5.0, float(startup_timeout))
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    last_error = _tail(log_path) or f"server exited with code {process.returncode}"
                    break
                if _health_ok(url):
                    return url
                time.sleep(0.25)
            else:
                last_error = _tail(log_path) or "server startup timed out"

            stop_managed_server()
            # Only a port collision benefits from retrying. Other failures are
            # still retried twice because some CUDA initialisation failures are
            # transient immediately after unloading a previous model.

        raise ManagedServerError(
            "The managed llama-server could not load this GGUF model. "
            f"See {log_path}. Last server output: {last_error}"
        )


atexit.register(stop_managed_server)
