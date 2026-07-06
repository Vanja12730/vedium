"""Structural verification harness for the Settings window.

Builds the real Settings UI in-process (no daemon, no keyboard listener),
walks every tab, presses Save & Connect, and exercises the singleton and
close paths.  Network, credential-store, and model-loading calls are
stubbed so the test never touches the real system state; the config file
is redirected to a scratch copy so the user's real config is untouched.

Run:  python settings_ui_test.py

A Tk window is created and immediately withdrawn, so expect a brief
flicker.  Requires the app's dependencies (pynput, pystray, Pillow).
"""
import sys
import os
import json
import shutil
import tempfile
import threading

repo = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, repo)

import tkinter as tk

from spell_daemon import constants as c
from spell_daemon import config as sd_config
from spell_daemon import ui_settings

failures = []


def check(name, cond, note=""):
    if cond:
        print(f"PASS {name}")
    else:
        failures.append(name)
        print(f"FAIL {name}: {note}")


# ── Redirect config + stub out anything with side effects ────────────────────
tmpdir = tempfile.mkdtemp(prefix="ga_harness_")
tmp_cfg_path = os.path.join(tmpdir, "spell_daemon_config.json")
shutil.copyfile(c.CONFIG_FILE, tmp_cfg_path)
c.CONFIG_FILE = tmp_cfg_path

ui_settings.store_cloud_api_key = lambda *a, **k: True
ui_settings.delete_stored_cloud_api_key = lambda *a, **k: True
ui_settings.get_stored_cloud_api_key = lambda *a, **k: ""
ui_settings.fetch_cloud_models = lambda *a, **k: []
ui_settings.connect_cloud = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stubbed"))
ui_settings.connect_server = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stubbed"))
ui_settings.detect_local_server = lambda *a, **k: ""

cfg = sd_config.load_cfg()

# ── Fake the UI thread: everything runs inline on this thread ────────────────
root = tk.Tk()
root.withdraw()
ui_settings._UI_ROOT = root
ui_settings._UI_THREAD_ID = threading.get_ident()
ui_settings._UI_STARTED = True
ui_settings._UI_READY.set()

saved_cfgs = []


def on_cfg_saved(new_cfg):
    saved_cfgs.append(dict(new_cfg))


def on_model_loaded(path):
    return "Stub backend"


# ── Build the window ──────────────────────────────────────────────────────────
ui_settings._open_settings_ui(cfg, on_cfg_saved, on_model_loaded)
win = ui_settings._settings_win
check("window built", win is not None and win.winfo_exists())
win.withdraw()
win.update_idletasks()


def find_widgets(parent, klass, out=None):
    out = out if out is not None else []
    for ch in parent.winfo_children():
        if isinstance(ch, klass):
            out.append(ch)
        find_widgets(ch, klass, out)
    return out


import tkinter.ttk as ttk

notebooks = find_widgets(win, ttk.Notebook)
check("notebook present", len(notebooks) == 1, f"found {len(notebooks)}")
nb = notebooks[0]
tabs = nb.tabs()
check("six tabs", len(tabs) == 6, f"found {len(tabs)}")

# Walk every tab; each select fires <<NotebookTabChanged>> and any lazy code.
for i, tab_id in enumerate(tabs):
    nb.select(tab_id)
    win.update()
tab_texts = [nb.tab(t, "text") for t in tabs]
for expected in ("Setup", "Status", "Model", "Behaviour", "Rephrase", "Appearance"):
    check(f"tab '{expected}' exists", any(expected in t for t in tab_texts), str(tab_texts))

# Rephrase mode listbox must show all built-in modes (the old blank-tab bug).
listboxes = find_widgets(win, tk.Listbox)
mode_lb = None
for lb in listboxes:
    if lb.size() >= len(c._BUILTIN_MODES):
        mode_lb = lb
check("mode listbox populated", mode_lb is not None,
      f"listbox sizes: {[lb.size() for lb in listboxes]}")

# ── Press Save & Connect ──────────────────────────────────────────────────────
save_btns = [b for b in find_widgets(win, tk.Button) if "Save & Connect" in b.cget("text")]
check("save button present", len(save_btns) == 1, f"found {len(save_btns)}")
if save_btns:
    save_btns[0].invoke()
    win.update()
    check("on_cfg_saved fired", len(saved_cfgs) == 1, f"calls: {len(saved_cfgs)}")
    with open(tmp_cfg_path, encoding="utf-8") as f:
        written = json.load(f)
    check("config written", written.get("backend_mode") == cfg.get("backend_mode"))
    check("secrets stripped on save",
          written.get("cloud_api_key") == "" and written.get("cloud_api_keys") == {},
          str({k: written.get(k) for k in ("cloud_api_key", "cloud_api_keys")}))

# ── Singleton: second open must reuse the window ──────────────────────────────
ui_settings._open_settings_ui(cfg, on_cfg_saved, on_model_loaded)
check("singleton reuse", ui_settings._settings_win is win)
win.withdraw()

# ── Close path (WM_DELETE handler persists geometry) ─────────────────────────
win.update_idletasks()
# invoke the registered close handler exactly as the WM would
win.tk.eval(f"eval [wm protocol {win} WM_DELETE_WINDOW]")
check("window closed", ui_settings._settings_win is None)
with open(tmp_cfg_path, encoding="utf-8") as f:
    written = json.load(f)
check("geometry persisted on close", "settings_window_width" in written)

# ── Chat window builds and closes ─────────────────────────────────────────────
ui_settings._open_chat_window_ui(cfg)
chat = ui_settings._chat_win
check("chat window built", chat is not None and chat.winfo_exists())
chat.withdraw()
chat.update_idletasks()
chat.destroy()
root.update()
check("chat window closed", ui_settings._chat_win is None)

root.destroy()
shutil.rmtree(tmpdir, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
