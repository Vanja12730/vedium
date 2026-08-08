# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Structural verification harness for the Settings window.

Builds the real Settings UI in-process (no daemon, no keyboard listener),
walks every tab, presses Connect, and exercises the singleton and
close paths.  Network, credential-store, and model-loading calls are
stubbed so the test never touches the real system state; the config file
is redirected to a scratch copy so the user's real config is untouched.

Run:  python tests/settings_ui_test.py

A Tk window is created and immediately withdrawn, so expect a brief
flicker.  Requires the app's dependencies (pynput, pystray, Pillow).
"""
import sys
import os
import json
import shutil
import tempfile
import threading

repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, repo)

import tkinter as tk

from vedium import constants as c
from vedium import config as sd_config
from vedium import ui_settings
from vedium import ui_chat
from vedium import ui_toasts
from vedium import ui_thread
from vedium.ui_settings import model_tab, bottom_bar

failures = []


def check(name, cond, note=""):
    if cond:
        print(f"PASS {name}")
    else:
        failures.append(name)
        print(f"FAIL {name}: {note}")


# ── Redirect config + stub out anything with side effects ────────────────────
tmpdir = tempfile.mkdtemp(prefix="vedium_harness_")
tmp_cfg_path = os.path.join(tmpdir, "vedium_config.json")
# The live config may still be under its pre-rename name if the app hasn't
# run since the Vedium rename — load_cfg() adopts it lazily.
src_cfg_path = c.CONFIG_FILE if os.path.exists(c.CONFIG_FILE) else c.LEGACY_CONFIG_FILE
shutil.copyfile(src_cfg_path, tmp_cfg_path)
c.CONFIG_FILE = tmp_cfg_path

model_tab.store_cloud_api_key = lambda *a, **k: True
model_tab.delete_stored_cloud_api_key = lambda *a, **k: True
model_tab.get_stored_cloud_api_key = lambda *a, **k: ""
model_tab.fetch_cloud_models = lambda *a, **k: []
model_tab.connect_cloud = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stubbed"))
model_tab.connect_server = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stubbed"))
model_tab.detect_local_server = lambda *a, **k: ""
bottom_bar.connect_cloud = model_tab.connect_cloud
bottom_bar.connect_server = model_tab.connect_server
bottom_bar.connect_cli = lambda *a, **k: "Stub CLI backend"
model_tab.detect_clis = lambda **_kwargs: {
    "claude": {"path": r"C:\stub\claude.cmd", "models": [
        {"id": "claude-haiku", "label": "Claude Code - Haiku", "efforts": ["low"]},
    ]},
    "codex": {"path": None, "default_model": "", "models": []},
    "antigravity": {"path": None, "models": []},
}

cfg = sd_config.load_cfg()

# ── Fake the UI thread: everything runs inline on this thread ────────────────
root = tk.Tk()
root.withdraw()
ui_thread._UI_ROOT = root
ui_thread._UI_THREAD_ID = threading.get_ident()
ui_thread._UI_STARTED = True
ui_thread._UI_READY.set()

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
for expected in ("Setup", "Status", "Model", "Behaviour", "Assistant", "Appearance"):
    check(f"tab '{expected}' exists", any(expected in t for t in tab_texts), str(tab_texts))

# The mode panels (Reading + Writing listboxes) must together show every
# built-in mode (the old blank-tab bug).  Labels may carry trigger/hotkey
# badges after "  ·  "; separator rows ("── … ──") don't count.
listboxes = find_widgets(win, tk.Listbox)
shown_modes = set()
for lb in listboxes:
    for i in range(lb.size()):
        label = lb.get(i)
        if not label.startswith("──"):
            shown_modes.add(label.split("  ·  ")[0])
missing = [m for m in c._BUILTIN_MODES if m not in shown_modes]
check("mode panels populated", not missing,
      f"missing modes: {missing}; listbox sizes: {[lb.size() for lb in listboxes]}")

# A style can be created from an answer card while Settings is already open.
# The live Writing picker must pull it in immediately rather than keeping the
# custom-mode snapshot it took when the window was built.
original_custom_modes = dict(cfg.get("custom_rephrase_modes", {}))
original_writing_mode = cfg.get("rephrase_writing_mode", "Polish My Writing")
sd_config._add_generated_style_mode(
    cfg, "Live Dropdown Refresh", "Uses short, measured sentences.")
ui_settings._refresh_open_rephrase_modes()
writing_pickers = [
    cb for cb in find_widgets(win, ttk.Combobox)
    if "Polish My Writing" in cb.cget("values")
]
check("open Writing picker refreshes after generated style save",
      bool(writing_pickers) and
      "Live Dropdown Refresh" in writing_pickers[0].cget("values") and
      writing_pickers[0].get() == "Live Dropdown Refresh")
cfg["custom_rephrase_modes"] = original_custom_modes
cfg["rephrase_writing_mode"] = original_writing_mode
sd_config._merge_custom_modes(cfg)
ui_settings._refresh_open_rephrase_modes()

# ── Press Connect (saves + applies connection settings only) ─────────────────
save_btns = [b for b in find_widgets(win, tk.Button) if "∞" in b.cget("text")]
check("connect button present", len(save_btns) == 1, f"found {len(save_btns)}")
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
ui_chat._open_chat_window_ui(cfg)
chat = ui_chat._chat_win
check("chat window built", chat is not None and chat.winfo_exists())
chat.withdraw()
chat.update_idletasks()
chat.destroy()
root.update()
check("chat window closed", ui_chat._chat_win is None)

# ── Extracted-style card saves an editable custom writing mode ──────────────
style_cfg = {
    "custom_rephrase_modes": {},
    "builtin_prompt_overrides": {},
    "rephrase_writing_mode": "Polish My Writing",
}
style_ignored = []
original_askstring = ui_toasts.simpledialog.askstring
ui_toasts.simpledialog.askstring = lambda *a, **k: "Edited Quiet Prose"
style_toast = ui_toasts._EditableToast(
    "Generating suggestion...",
    lambda _text: None,
    lambda: style_ignored.append(True),
    label="Extract Writing Style",
    original="Reference prose.",
    answer_only=True,
    cfg=style_cfg,
    on_save_style=lambda text, parent: ui_toasts._save_generated_style(
        style_cfg, text, parent),
)
style_toast.set_text(
    "STYLE NAME: Quiet Analytical Prose\n"
    "STYLE PROFILE:\n"
    "- Uses restrained diction and measured sentence rhythms.",
    select=True,
)
root.update()
style_windows = [
    child for child in root.winfo_children()
    if isinstance(child, tk.Toplevel) and child.title() == "Extract Writing Style"
]
style_buttons = find_widgets(style_windows[0], tk.Button) if style_windows else []
save_style_buttons = [b for b in style_buttons if b.cget("text") == "Save as Writing Style…"]
check("style answer card has save action", len(save_style_buttons) == 1)
check("style save action enabled after generation",
      bool(save_style_buttons) and str(save_style_buttons[0].cget("state")) == "normal")

if save_style_buttons:
    save_style_buttons[0].invoke()
    root.update()
ui_toasts.simpledialog.askstring = original_askstring
check("generated style saved as custom writing mode",
      "Edited Quiet Prose" in style_cfg["custom_rephrase_modes"])
check("saved style becomes Improve Writing default",
      style_cfg["rephrase_writing_mode"] == "Edited Quiet Prose")
check("style answer card closes after save", bool(style_ignored))
sd_config._merge_custom_modes(cfg)

root.destroy()
shutil.rmtree(tmpdir, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
