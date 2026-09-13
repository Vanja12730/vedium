# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Field identity and password protection, without reading any field's text.

UI Automation calls belong on a dedicated COM thread, never a Windows hook or
Tk thread. Each request gets a fresh snapshot; a slow/unavailable provider fails
closed instead of reusing a possibly non-password snapshot from an earlier field.
"""

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import queue
import sys
import threading
from . import diagnostics

try:
    import comtypes
except ImportError:
    comtypes = None


def foreground_window() -> int | None:
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    return user32.GetForegroundWindow() or None


def window_at_point(x: int, y: int) -> int | None:
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    return user32.WindowFromPoint(wintypes.POINT(int(x), int(y))) or None


@dataclass(frozen=True)
class InputContext:
    window: int
    process_id: int
    runtime_id: tuple[int, ...]
    password: bool | None
    editable: bool

    @property
    def can_capture(self) -> bool:
        return self.password is False and self.editable

    @property
    def identity(self) -> tuple:
        return self.window, self.process_id, self.runtime_id


def _read_context(automation, cache) -> InputContext | None:
    before = foreground_window()
    if not before:
        return None
    element = automation.GetFocusedElementBuildCache(cache)
    if not element:
        return None
    # ignoreDefaultValue=True distinguishes "unsupported" from a real False.
    prop = lambda property_id: element.GetCachedPropertyValueEx(property_id, True)
    runtime = prop(30000)  # RuntimeId
    process = prop(30002)  # ProcessId
    password = prop(30019)  # IsPassword
    control = prop(30003)  # ControlType
    focused = prop(30008)  # HasKeyboardFocus
    enabled = prop(30010)  # IsEnabled
    if (foreground_window() != before or focused is not True
            or not isinstance(runtime, (tuple, list)) or not runtime
            or not isinstance(process, int) or process <= 0):
        return None
    return InputContext(
        before, process, tuple(runtime),
        password if isinstance(password, bool) else None,
        # Capture only text editors/documents, never arbitrary controls.
        editable=control in (50004, 50030) and enabled is True,
    )


class _FocusProbe:
    def __init__(self):
        self._requests = queue.Queue(maxsize=1)
        self._start_lock = threading.Lock()
        self._started = False

    def _run(self):
        reader = lambda: None
        initialized = False
        try:
            if comtypes is not None and sys.platform == "win32":
                comtypes.CoInitializeEx(0)  # COINIT_MULTITHREADED
                initialized = True
                from comtypes.client import CreateObject, GetModule
                module = GetModule("UIAutomationCore.dll")
                automation = CreateObject(module.CUIAutomation, interface=module.IUIAutomation)
                cache = automation.CreateCacheRequest()
                for property_id in (30000, 30002, 30003, 30008, 30010, 30019):
                    cache.AddProperty(property_id)
                reader = lambda: _read_context(automation, cache)
        except Exception as exc:
            # Missing/broken accessibility support must never enable capture.
            diagnostics.record("privacy", "warning",
                               "Field safety checks unavailable; typing capture paused.", str(exc))
        try:
            while True:
                done, result = self._requests.get()
                if done.is_set():  # caller timed out before this request ran
                    continue
                try:
                    result.append(reader())
                except Exception:
                    result.append(None)
                finally:
                    done.set()
        finally:
            if initialized:
                comtypes.CoUninitialize()

    def read(self, timeout: float = 0.075) -> InputContext | None:
        with self._start_lock:
            if not self._started:
                threading.Thread(target=self._run, name="VediumFocus", daemon=True).start()
                self._started = True
        done, result = threading.Event(), []
        try:
            self._requests.put_nowait((done, result))
        except queue.Full:
            return None
        if not done.wait(timeout):
            done.set()
            return None
        return result[0] if result else None


_probe = _FocusProbe()


def focused_context() -> InputContext | None:
    return _probe.read()
