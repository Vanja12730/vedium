# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Focused Tkinter checks for the AI CLI settings and consent flow.

Optional tray/keyboard dependencies are stubbed before importing Vedium. Model
and CLI calls are stubbed, so this test spends no subscription usage.
"""

import copy
import json
import queue
import sys
import tempfile
import threading
import time
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _Keys:
    def __getattr__(self, name):
        return f"<{name}>"


class _KeyCode:
    @staticmethod
    def from_char(char):
        return char


class _Controller:
    def press(self, *_args):
        pass

    def release(self, *_args):
        pass


class _Listener:
    def __init__(self, *args, **kwargs):
        pass


keyboard = types.ModuleType("pynput.keyboard")
keyboard.Key = _Keys()
keyboard.KeyCode = _KeyCode
keyboard.Controller = _Controller
keyboard.Listener = _Listener
pynput = types.ModuleType("pynput")
pynput.keyboard = keyboard
sys.modules.setdefault("pynput", pynput)
sys.modules.setdefault("pynput.keyboard", keyboard)
sys.modules.setdefault("pystray", types.ModuleType("pystray"))

import tkinter as tk
import tkinter.ttk as ttk

from vedium import constants as c
from vedium import ui_settings
from vedium import ui_thread
from vedium.ui_settings import model_tab, bottom_bar


def _walk(parent):
    result = []
    for child in parent.winfo_children():
        result.append(child)
        result.extend(_walk(child))
    return result


def _pump_ui_callbacks():
    """Mirror ui_thread's mainloop pump in this single-threaded UI harness."""
    while True:
        try:
            fn, args, kwargs, done, box = ui_thread._UI_QUEUE.get_nowait()
        except queue.Empty:
            return
        try:
            box["result"] = fn(*args, **kwargs)
        except Exception as exc:
            box["error"] = exc
            raise
        finally:
            if done is not None:
                done.set()


def main() -> int:
    tmp = tempfile.TemporaryDirectory(prefix="vedium_cli_ui_")
    c.CONFIG_FILE = str(Path(tmp.name) / "vedium_config.json")
    cfg = copy.deepcopy(c.DEFAULT_CFG)
    cfg["backend_mode"] = "local"
    Path(c.CONFIG_FILE).write_text(json.dumps(cfg), encoding="utf-8")

    model_tab.store_cloud_api_key = lambda *a, **k: True
    model_tab.delete_stored_cloud_api_key = lambda *a, **k: True
    model_tab.get_stored_cloud_api_key = lambda *a, **k: ""
    model_tab.fetch_cloud_models = lambda *a, **k: []
    model_tab.connect_cloud = lambda *a, **k: "Stub cloud"
    model_tab.connect_server = lambda *a, **k: "Stub server"
    model_tab.detect_local_server = lambda *a, **k: ""
    bottom_bar.connect_cloud = model_tab.connect_cloud
    bottom_bar.connect_server = model_tab.connect_server
    detect_calls = []

    def _detect_clis(**kwargs):
        detect_calls.append(kwargs)
        return {
            "claude": {
                "path": r"C:\stub\claude.cmd",
                "models": [{
                    "id": "claude-haiku",
                    "label": "Claude Code - Haiku",
                    "efforts": ["low", "medium", "high"],
                }],
            },
            "codex": {"path": None, "default_model": "", "models": []},
            "antigravity": {"path": None, "models": []},
        }

    model_tab.detect_clis = _detect_clis
    connect_calls = []
    connected = threading.Event()

    def _connect_cli(*args):
        connect_calls.append(args)
        connected.set()
        return "AI CLI (Claude Code - Haiku)"

    bottom_bar.connect_cli = _connect_cli

    answers = [False, True]
    warnings = []

    def _ask(title, message, **_kwargs):
        warnings.append((title, message))
        return answers.pop(0)

    model_tab.messagebox.askokcancel = _ask

    root = tk.Tk()
    root.withdraw()
    ui_thread._UI_ROOT = root
    ui_thread._UI_THREAD_ID = threading.get_ident()
    ui_thread._UI_STARTED = True
    ui_thread._UI_READY.set()
    saved = []
    ui_settings._open_settings_ui(cfg, lambda new: saved.append(dict(new)), lambda _path: "Local")
    win = ui_settings._settings_win
    assert win is not None and win.winfo_exists()
    win.withdraw()
    win.update()

    widgets = _walk(win)
    labels = [widget.cget("text") for widget in widgets if isinstance(widget, tk.Label)]
    assert any("Claude Code: not checked" in text for text in labels)
    assert any("Codex CLI: not checked" in text for text in labels)
    assert any("Antigravity CLI: not checked" in text for text in labels)
    assert detect_calls == [], "opening Settings must not launch disabled AI CLIs"

    toggles = {
        widget.label.cget("text"): widget
        for widget in widgets
        if isinstance(widget, tk.Frame) and hasattr(widget, "label")
    }
    main_toggle = toggles["Use installed AI CLIs as models"]
    # The automatic CLI spell-check toggle was removed with the feature.
    assert "Allow automatic spell-check with AI CLI" not in toggles
    assert main_toggle.variable.get() is False

    main_toggle.invoke()
    win.update()
    assert main_toggle.variable.get() is False
    assert detect_calls == [], "declining consent must not launch AI CLIs"
    main_toggle.invoke()
    deadline = time.time() + 2.0
    while time.time() < deadline:
        _pump_ui_callbacks()
        win.update()
        if detect_calls and any(
            isinstance(widget, tk.Label) and "Claude Code: found" in widget.cget("text")
            for widget in _walk(win)
        ):
            break
        time.sleep(0.01)
    assert main_toggle.variable.get() is True
    assert detect_calls == [{"refresh": True}]

    cli_model = next(
        widget for widget in _walk(win)
        if isinstance(widget, ttk.Combobox)
        and "claude-haiku" in tuple(widget.cget("values"))
    )
    assert str(cli_model.cget("state")) == "readonly"
    cli_model.set("claude-haiku")
    effort = next(
        widget for widget in _walk(win)
        if isinstance(widget, ttk.Combobox)
        and tuple(widget.cget("values")) == ("Default", "low", "medium", "high", "xhigh", "max")
    )
    effort.set("low")

    assert warnings[0][0] == "Enable AI CLI models?"
    assert "subscription's usage" in warnings[0][1]

    cli_radio = next(
        widget for widget in _walk(win)
        if isinstance(widget, tk.Radiobutton) and widget.cget("text") == "AI CLI"
    )
    cli_radio.invoke()
    save = next(
        widget for widget in _walk(win)
        if isinstance(widget, tk.Button) and "∞" in widget.cget("text")
    )
    save.invoke()
    win.update()
    assert saved
    assert saved[-1]["backend_mode"] == "cli"
    assert saved[-1]["cli_models_enabled"] is True
    assert saved[-1]["cli_model"] == "claude-haiku"
    assert saved[-1]["cli_reasoning_effort"] == "low"
    assert "cli_spell_check_enabled" not in saved[-1]
    assert connected.wait(2.0)
    assert connect_calls == [("claude-haiku", True, "low")]

    main_toggle.invoke()
    win.update()
    assert main_toggle.variable.get() is False

    source = (ROOT / "vedium" / "ui_chat.py").read_text(encoding="utf-8")
    label_pos = source.index('_append_display("\\nAI\\n", "ai_name")')
    wait_pos = source.index('_append_display("💭 Thinking…", "ai_stream")')
    worker_pos = source.index("def _stream_worker():", label_pos)
    assert label_pos < wait_pos < worker_pos

    win.tk.eval(f"eval [wm protocol {win} WM_DELETE_WINDOW]")
    root.destroy()
    tmp.cleanup()
    print("PASS CLI settings detection, consent, persistence, connect, and waiting state")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
