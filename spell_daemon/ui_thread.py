"""Single Tk UI thread shared by settings, toasts, and clipboard access.

Tk is not thread-safe: every widget, Tk variable, popup, and clipboard call
must run on the one thread that owns the Tk interpreter. This module owns
that thread. Callers on worker threads marshal work onto it via `_ui_call`.

`_UI_ROOT` is rebound at runtime (set once the UI thread starts), so
consumers must read it via the module (`ui_thread._UI_ROOT`), never
`from ... import _UI_ROOT`.
"""

import queue
import threading
import tkinter as tk
import tkinter.ttk as ttk

from . import diagnostics

_UI_ROOT = None       # hidden root owned by the UI thread
_UI_THREAD_ID = None
_UI_QUEUE = queue.Queue(maxsize=64)
_UI_READY = threading.Event()
_UI_STARTED = False
_UI_START_LOCK = threading.Lock()

def _update_sv_ttk_theme():
    """Use Tk's lightweight recolorable theme.

    The third-party sv_ttk theme makes live resizing of this widget-heavy
    Settings window several times slower on Windows.  ``clam`` still honours
    the custom ttk colours below without the expensive resize path.
    """
    try:
        style = ttk.Style()
        if "clam" in style.theme_names() and style.theme_use() != "clam":
            style.theme_use("clam")
    except Exception:
        pass

def _ui_thread_main():
    """Own the single Tk interpreter used by settings, popups, and clipboard."""
    global _UI_ROOT, _UI_THREAD_ID
    _UI_THREAD_ID = threading.get_ident()
    root = tk.Tk()
    root.withdraw()
    _UI_ROOT = root
    _UI_READY.set()
    _update_sv_ttk_theme()

    def pump_queue():
        while True:
            try:
                fn, args, kwargs, done, box = _UI_QUEUE.get_nowait()
            except queue.Empty:
                break
            try:
                box["result"] = fn(*args, **kwargs)
            except Exception as e:
                box["error"] = e
                try:
                    diagnostics.record("ui", "error", "UI action failed.", f"{fn.__name__}: {e}")
                except Exception:
                    pass
            finally:
                if done is not None:
                    done.set()
        root.after(30, pump_queue)

    root.after(30, pump_queue)
    root.mainloop()


def _ensure_ui_thread():
    """Start the Tk UI thread once and wait until its hidden root exists."""
    global _UI_STARTED
    with _UI_START_LOCK:
        if not _UI_STARTED:
            threading.Thread(target=_ui_thread_main, daemon=True).start()
            _UI_STARTED = True
    _UI_READY.wait(timeout=5)


def _ui_call(fn, *args, wait: bool = False, **kwargs):
    """
    Run a callable on the single Tk UI thread.

    This prevents Tk widgets, Tk variables, popups, settings windows, and
    clipboard calls from being created/destroyed by different worker threads.
    """
    _ensure_ui_thread()
    if threading.get_ident() == _UI_THREAD_ID:
        return fn(*args, **kwargs)

    done = threading.Event() if wait else None
    box = {}
    try:
        _UI_QUEUE.put_nowait((fn, args, kwargs, done, box))
    except queue.Full:
        # Queue is full (> 64 pending callbacks). Fire-and-forget calls are
        # dropped rather than blocking the caller (which may be the pynput
        # listener thread or a streaming LLM thread holding _llm_lock).
        # Blocking calls fall back to a short-timeout put so they still
        # eventually execute without deadlocking indefinitely.
        if wait:
            _UI_QUEUE.put((fn, args, kwargs, done, box), timeout=10)
        else:
            return None  # drop the fire-and-forget call
    if wait:
        done.wait()
        if "error" in box:
            raise box["error"]
        return box.get("result")
    return None
