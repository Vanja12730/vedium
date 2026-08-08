# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Focused checks for the bundled llama-server transport and lifecycle.

Run with: python tests/managed_server_tests.py
"""

import json
from pathlib import Path
import sys
import tempfile
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vedium import managed_server
from vedium import llm_backends
from vedium.llm_backends import _LlamaServerClient, _backend


class _FakeProcess:
    def __init__(self, args):
        self.args = args
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class _Response:
    status = 200

    def __init__(self, data):
        self._data = data

    def read(self):
        return json.dumps(self._data).encode("utf-8")

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_discovers_installed_cuda_engine():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        exe = root / "llama-server" / "cuda" / "llama-server.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"test")
        with mock.patch.object(managed_server, "_app_root", return_value=root):
            assert managed_server.find_managed_server() == str(exe.resolve())


def test_start_builds_safe_local_auto_fit_command_and_stop_owns_process():
    managed_server.stop_managed_server()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        exe = root / "llama-server.exe"
        model = root / "model.gguf"
        exe.write_bytes(b"test")
        model.write_bytes(b"GGUF")
        created = []

        def fake_popen(args, **kwargs):
            process = _FakeProcess(args)
            created.append(process)
            return process

        with (
            mock.patch.object(managed_server, "_app_root", return_value=root),
            mock.patch.object(managed_server, "_pick_port", return_value=43123),
            mock.patch.object(managed_server, "_health_ok", return_value=True),
            mock.patch.object(managed_server.subprocess, "Popen", side_effect=fake_popen),
        ):
            url = managed_server.start_managed_server(
                str(model), configured_path=str(exe), n_ctx=8192, gpu_mode="Auto"
            )
            assert url == "http://127.0.0.1:43123"
            args = created[0].args
            assert args[args.index("--host") + 1] == "127.0.0.1"
            assert args[args.index("--ctx-size") + 1] == "8192"
            assert args[args.index("--gpu-layers") + 1] == "auto"
            assert args[args.index("--fit") + 1] == "on"
            assert "--jinja" in args and "--no-webui" in args
            managed_server.stop_managed_server()
            assert created[0].terminated
            assert not managed_server.is_managed_server_running()


def test_native_chat_uses_model_template_and_disables_thinking():
    client = object.__new__(_LlamaServerClient)
    client.base_url = "http://127.0.0.1:8080"
    client.supports_chat_template_kwargs = True
    previous = _backend.server_disable_thinking
    requests = []

    def fake_urlopen(request, timeout=0):
        requests.append(request)
        return _Response({
            "choices": [{
                "message": {"content": "OK"},
                "finish_reason": "stop",
            }]
        })

    try:
        _backend.server_disable_thinking = True
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = client.complete_messages(
                [{"role": "user", "content": "Reply with OK."}],
                max_tokens=16,
                temperature=0.0,
            )
        payload = json.loads(requests[0].data)
        assert requests[0].full_url.endswith("/v1/chat/completions")
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        assert payload["messages"][0]["content"] == "Reply with OK."
        assert result["choices"][0]["text"] == "OK"
    finally:
        _backend.server_disable_thinking = previous


def test_missing_managed_engine_fails_without_python_fallback():
    with (
        mock.patch.object(llm_backends, "load_cfg", return_value={"n_ctx": 4096}),
        mock.patch.object(llm_backends, "find_managed_server", return_value=""),
    ):
        try:
            llm_backends.load_model("missing.gguf")
        except RuntimeError as exc:
            assert "managed local-model engine is not installed" in str(exc)
        else:
            raise AssertionError("A missing managed engine must fail explicitly")

    source = (ROOT / "vedium" / "llm_backends.py").read_text(encoding="utf-8")
    assert "llama_cpp" not in source
    assert "trying the Python engine" not in source


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS  {test.__name__}")
    print(f"\n{len(tests)} managed-server tests passed.")
