# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Exercise actual Windows password metadata on hidden, test-owned controls.

No foreground focus, desktop keystrokes, clipboard or user documents are used.
"""
import ctypes
from ctypes import wintypes as wt
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vedium import input_context


@unittest.skipUnless(sys.platform == "win32", "Windows UI Automation integration")
class WindowsMetadataTests(unittest.TestCase):
    def test_native_normal_and_password_controls_have_distinct_safe_metadata(self):
        user32 = ctypes.windll.user32
        user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                         ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                         wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p]
        user32.CreateWindowExW.restype = wt.HWND
        user32.DestroyWindow.argtypes = [wt.HWND]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
        ready = threading.Event()
        state = {}
        def windows_thread():
            state["thread"] = ctypes.windll.kernel32.GetCurrentThreadId()
            parent = user32.CreateWindowExW(0, "Static", "Vedium test", 0, 0, 0, 100, 100,
                                            None, None, None, None)
            try:
                state["parent"] = parent
                state["normal"] = user32.CreateWindowExW(0, "Edit", "", 0x40000000,
                                                        0, 0, 90, 20, parent, None, None, None)
                state["password"] = user32.CreateWindowExW(0, "Edit", "", 0x40000020,
                                                          0, 25, 90, 20, parent, None, None, None)
                ready.set()
                msg = wt.MSG()
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
            finally:
                user32.DestroyWindow(parent)
        worker = threading.Thread(target=windows_thread, daemon=True)
        worker.start()
        self.assertTrue(ready.wait(5))
        try:
            self.assertTrue(state["normal"] and state["password"])
            from comtypes.client import CreateObject, GetModule
            module = GetModule("UIAutomationCore.dll")
            automation = CreateObject(module.CUIAutomation, interface=module.IUIAutomation)
            cache = automation.CreateCacheRequest()
            for p in (30000, 30002, 30003, 30008, 30010, 30019):
                cache.AddProperty(p)
            results = []
            for name in ("normal", "password"):
                # Hidden controls intentionally never receive foreground focus.
                # Override only that eligibility bit and the test window token;
                # identity/password/editability come from the actual provider.
                element = automation.ElementFromHandleBuildCache(state[name], cache)
                class Element:
                    def GetCachedPropertyValueEx(self, p, ignore):
                        return True if p == 30008 else element.GetCachedPropertyValueEx(p, ignore)
                class Automation:
                    def GetFocusedElementBuildCache(self, _cache):
                        return Element()
                with mock.patch.object(input_context, "foreground_window", return_value=state["parent"]):
                    results.append(input_context._read_context(Automation(), cache))
            normal, password = results
            self.assertTrue(normal.can_capture)
            self.assertTrue(password.password)
            self.assertFalse(password.can_capture)
            self.assertNotEqual(normal.identity, password.identity)
        finally:
            user32.PostThreadMessageW(state["thread"], 0x0012, 0, 0)  # WM_QUIT
            worker.join(5)
            self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
