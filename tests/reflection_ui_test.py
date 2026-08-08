# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Behavioural verification of the reflection and vocabulary save flows.

Builds the real answer card and its two child dialogs in-process (no daemon,
no keyboard listener, no model) and drives them the way a user would: press
the button, fill the fields, press Save, then read the Markdown that landed
on disk.

Complements the source-level assertions in refactor_smoke_tests.py, which can
only prove the wiring exists — these prove it behaves.

Run:  python tests/reflection_ui_test.py

The config file is redirected to a scratch copy and every journal is written
under a temporary directory, so the user's real config and Documents folder
are never touched.  A Tk window is created and immediately withdrawn, so
expect a brief flicker.  Requires the app's dependencies.
"""
import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import tkinter as tk

repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, repo)

from vedium import constants as c
from vedium import config as sd_config
from vedium import reflections
from vedium import ui_thread
from vedium import ui_toasts

failures = []


def check(name, cond, note=""):
    if cond:
        print(f"PASS {name}")
    else:
        failures.append(name)
        print(f"FAIL {name}: {note}")


# ── Redirect config + silence the confirmation toasts ────────────────────────
tmpdir = tempfile.mkdtemp(prefix="vedium_reflect_")
c.CONFIG_FILE = os.path.join(tmpdir, "vedium_config.json")

info_toasts = []
ui_toasts._info_toast = lambda message, duration_ms=2200: info_toasts.append(message)

# ── Fake the UI thread: everything runs inline on this thread ────────────────
root = tk.Tk()
root.withdraw()
ui_thread._UI_ROOT = root
ui_thread._UI_THREAD_ID = threading.get_ident()
ui_thread._UI_STARTED = True
ui_thread._UI_READY.set()


def pump(cycles=1):
    """Run Tk events plus the cross-thread callback queue.

    Work handed back from a worker thread goes through ui_thread._UI_QUEUE,
    which the real app drains from its mainloop. This harness has no mainloop,
    so anything touching a background thread has to drain it here or the
    callback never runs.
    """
    for _ in range(cycles):
        while True:
            try:
                fn, args, kwargs, done, box = ui_thread._UI_QUEUE.get_nowait()
            except queue.Empty:
                break
            try:
                box["result"] = fn(*args, **kwargs)
            except Exception as exc:
                box["error"] = exc
            finally:
                if done is not None:
                    done.set()
        root.update()


def find_widgets(parent, klass, out=None):
    out = out if out is not None else []
    for ch in parent.winfo_children():
        if isinstance(ch, klass):
            out.append(ch)
        find_widgets(ch, klass, out)
    return out


# Both card actions swap to a confirmation label for ~1.8s after a save, so
# they have to be located by either spelling.
REFLECT = ("Reflect…", "Reflection saved  ✓")
VOCABULARY = ("Save to Vocabulary…", "Saved to Vocabulary  ✓")


def buttons_titled(labels):
    """Every button carrying one of these labels anywhere under the Tk root."""
    labels = (labels,) if isinstance(labels, str) else labels
    return [b for b in find_widgets(root, tk.Button) if b.cget("text") in labels]


def drive_dialog(title, action, attempts=60):
    """Run `action(dialog)` once a Toplevel with `title` appears.

    The dialogs block their caller in `wait_window`, so the interaction has to
    be scheduled beforehand and run from inside that nested event loop.
    """
    def attempt(left=attempts):
        matches = [
            w for w in find_widgets(root, tk.Toplevel)
            if w.winfo_exists() and w.title() == title
        ]
        if not matches:
            if left:
                root.after(20, lambda: attempt(left - 1))
            else:
                failures.append(f"dialog '{title}' never appeared")
                print(f"FAIL dialog '{title}' never appeared")
            return
        action(matches[0])
    root.after(20, attempt)


def press(window, text):
    for b in find_widgets(window, tk.Button):
        if b.cget("text") == text:
            b.invoke()
            return True
    return False


def make_card(label, original, answer, cfg, answer_only=True, on_save_style=None):
    card = ui_toasts._EditableToast(
        "Generating suggestion...",
        lambda _text=None: None,
        lambda: None,
        label=label,
        original=original,
        answer_only=answer_only,
        cfg=cfg,
        on_save_style=on_save_style,
    )
    card.set_text(answer, select=True)
    root.update()
    return card


def close_card(card):
    if card._close_fn is not None:
        card._close_fn()
    root.update()


# ── Which actions each kind of card offers ───────────────────────────────────
journal = os.path.join(tmpdir, "Journal", "Reflections.md")
vocabulary = os.path.join(tmpdir, "Journal", "Vocabulary.md")
cfg = {
    "reflection_journal_path": journal,
    "vocabulary_journal_path": vocabulary,
    "reflection_source": "",
    "reflection_window_position": None,
}

define_card = make_card("Define This", "liminal", "At a boundary or threshold.", cfg)
check("Define This offers vocabulary saving", len(buttons_titled(VOCABULARY)) == 1)
check("Define This offers reflection", len(buttons_titled(REFLECT)) == 1)
check("vocabulary action enabled once generated",
      str(buttons_titled(VOCABULARY)[0].cget("state")) == "normal")

# Re-entering the streaming state must take the action away again, so a
# half-generated definition can never be saved.
define_card.set_text("At a bound", select=False)
root.update()
check("vocabulary action disabled while streaming",
      str(buttons_titled(VOCABULARY)[0].cget("state")) == "disabled")
define_card.set_text("At a boundary or threshold.", select=True)
root.update()

close_card(define_card)

summary_card = make_card("What's This About?", "A long passage.", "It argues X.", cfg)
check("other answer modes offer reflection", len(buttons_titled(REFLECT)) == 1)
check("vocabulary saving is Define This only", len(buttons_titled(VOCABULARY)) == 0)
close_card(summary_card)

style_card = make_card(
    "Extract Writing Style", "Reference prose.", "STYLE NAME: X\nSTYLE PROFILE:\nY", cfg,
    on_save_style=lambda text, parent: False,
)
check("style card offers neither journal action",
      not buttons_titled(REFLECT) and not buttons_titled(VOCABULARY))
close_card(style_card)

writing_card = make_card(
    "Improve selected text  (Polish My Writing)", "some text", "Some text.", cfg,
    answer_only=False,
)
check("writing toasts offer neither journal action",
      not buttons_titled(REFLECT) and not buttons_titled(VOCABULARY))
close_card(writing_card)

# ── Saving a reflection writes grouped Markdown to the configured path ───────
card = make_card("What's This About?", "The selected passage.", "It argues X.", cfg)


def fill_reflection(dialog, text, source):
    texts = find_widgets(dialog, tk.Text)
    entries = find_widgets(dialog, tk.Entry)
    entries[0].delete(0, "end")
    entries[0].insert(0, source)
    texts[-1].insert("1.0", text)
    press(dialog, "Save reflection  ✓")


drive_dialog("Add reflection",
             lambda d: fill_reflection(d, "This reframes the argument.", "Example Book"))
buttons_titled(REFLECT)[0].invoke()
root.update()

check("reflection journal created at the configured path", os.path.exists(journal))
content = open(journal, encoding="utf-8").read() if os.path.exists(journal) else ""
check("reflection grouped under its source", "## Source: Example Book" in content)
check("reflection body written", "This reframes the argument." in content)
check("passage stored as a Markdown quote", "> The selected passage." in content)
check("originating mode recorded", "**Mode:** What's This About?" in content)
check("source remembered for next time", cfg.get("reflection_source") == "Example Book")
check("save confirmed to the user",
      any("Reflection saved" in m for m in info_toasts), str(info_toasts))

# A second reflection from the same source joins the existing group rather
# than starting a duplicate one — including when the user types it differently.
drive_dialog("Add reflection",
             lambda d: fill_reflection(d, "A later thought.", "example book"))
buttons_titled(REFLECT)[0].invoke()
root.update()
content = open(journal, encoding="utf-8").read()
check("same source is not duplicated", content.count("<!-- vedium-source -->") == 1,
      f"markers: {content.count('<!-- vedium-source -->')}")
check("both reflections retained",
      "This reframes the argument." in content and "A later thought." in content)

# A different source starts its own group.
drive_dialog("Add reflection",
             lambda d: fill_reflection(d, "From elsewhere.", "Other Book"))
buttons_titled(REFLECT)[0].invoke()
root.update()
content = open(journal, encoding="utf-8").read()
check("a new source starts a new group", content.count("<!-- vedium-source -->") == 2)

# ── The dialog reopens where the user left it ────────────────────────────────
check("window position persisted", isinstance(cfg.get("reflection_window_position"), dict),
      str(cfg.get("reflection_window_position")))

cfg["reflection_window_position"] = {"x": 271, "y": 143}
seen_position = {}


def record_position(dialog):
    dialog.update_idletasks()
    seen_position["x"] = dialog.winfo_x()
    seen_position["y"] = dialog.winfo_y()
    press(dialog, "Cancel")


drive_dialog("Add reflection", record_position)
buttons_titled(REFLECT)[0].invoke()
root.update()
check("dialog reopens at the saved position",
      seen_position.get("x") == 271 and seen_position.get("y") == 143, str(seen_position))

# An off-screen position is discarded rather than opening an unreachable window.
cfg["reflection_window_position"] = {"x": -30000, "y": -30000}
seen_position.clear()
drive_dialog("Add reflection", record_position)
buttons_titled(REFLECT)[0].invoke()
root.update()
check("off-screen position falls back to centred",
      seen_position.get("x", -30000) > -1000, str(seen_position))

# A corrupt saved position must not stop the dialog opening.
cfg["reflection_window_position"] = {"x": "left", "y": None}
seen_position.clear()
drive_dialog("Add reflection", record_position)
buttons_titled(REFLECT)[0].invoke()
root.update()
check("corrupt saved position is ignored", bool(seen_position), str(seen_position))

# ── "Change…" repoints the journal and persists the choice ───────────────────
moved = os.path.join(tmpdir, "Elsewhere", "My Reading.md")
os.makedirs(os.path.dirname(moved), exist_ok=True)
ui_toasts.filedialog.asksaveasfilename = lambda *a, **k: moved


def change_then_save(dialog):
    press(dialog, "Change…")
    fill_reflection(dialog, "Saved after moving.", "Example Book")


drive_dialog("Add reflection", change_then_save)
buttons_titled(REFLECT)[0].invoke()
root.update()
check("chosen journal path persisted to config",
      os.path.normcase(cfg.get("reflection_journal_path", "")) == os.path.normcase(moved),
      cfg.get("reflection_journal_path"))
check("reflection written to the chosen file",
      os.path.exists(moved) and "Saved after moving." in open(moved, encoding="utf-8").read())
check("previous journal left intact",
      "This reframes the argument." in open(journal, encoding="utf-8").read())
close_card(card)

# ── Saving vocabulary writes grouped Markdown, keyed by term ─────────────────
card = make_card("Define This", "Liminal", "At a boundary or threshold.", cfg)


def save_vocabulary(dialog, term=None, source="Example Book"):
    entries = find_widgets(dialog, tk.Entry)
    if term is not None:
        entries[0].delete(0, "end")
        entries[0].insert(0, term)
    entries[1].delete(0, "end")
    entries[1].insert(0, source)
    press(dialog, "Save to vocabulary  ✓")


drive_dialog("Save to vocabulary", save_vocabulary)
buttons_titled(VOCABULARY)[0].invoke()
root.update()

check("vocabulary journal created", os.path.exists(vocabulary))
vocab = open(vocabulary, encoding="utf-8").read() if os.path.exists(vocabulary) else ""
check("term prefilled from the selection", "## Term: Liminal" in vocab)
check("definition carried over from the card",
      "At a boundary or threshold." in vocab)
check("vocabulary source recorded", "**Source:** Example Book" in vocab)
check("vocabulary save confirmed",
      any("vocabulary" in m for m in info_toasts), str(info_toasts))
close_card(card)

# A second sense of the same word joins the existing term, case-insensitively.
card = make_card("Define This", "liminal", "Anthropology: a rite-of-passage stage.", cfg)
drive_dialog("Save to vocabulary", save_vocabulary)
buttons_titled(VOCABULARY)[0].invoke()
root.update()
vocab = open(vocabulary, encoding="utf-8").read()
check("same term is not duplicated", vocab.count("<!-- vedium-term -->") == 1,
      f"markers: {vocab.count('<!-- vedium-term -->')}")
check("both senses retained",
      "At a boundary or threshold." in vocab and "rite-of-passage" in vocab)
close_card(card)

# An empty term is refused rather than writing a headingless entry.
card = make_card("Define This", "liminal", "A definition.", cfg)
refused = []


def save_blank_term(dialog):
    ui_toasts.messagebox.showerror = lambda *a, **k: refused.append(a)
    save_vocabulary(dialog, term="   ")
    press(dialog, "Cancel")


before = open(vocabulary, encoding="utf-8").read()
drive_dialog("Save to vocabulary", save_blank_term)
buttons_titled(VOCABULARY)[0].invoke()
root.update()
check("blank term refused", bool(refused))
check("journal untouched after a refused save",
      open(vocabulary, encoding="utf-8").read() == before)
close_card(card)

# ── A passage-length selection is not prefilled as a term ────────────────────
passage = "This is an entire paragraph the user happened to select. " * 5
card = make_card("Define This", passage, "What the passage is getting at.", cfg)
seen_term = {}
too_long = []


def inspect_prefill(dialog):
    entries = find_widgets(dialog, tk.Entry)
    seen_term["value"] = entries[0].get()
    seen_term["labels"] = [
        w.cget("text") for w in find_widgets(dialog, tk.Label)
        if "Word or phrase" in str(w.cget("text"))
    ]
    # Typing something still too long must be refused, not silently written.
    ui_toasts.messagebox.showerror = lambda *a, **k: too_long.append(a)
    save_vocabulary(dialog, term=passage[:200])
    press(dialog, "Cancel")


before = open(vocabulary, encoding="utf-8").read()
drive_dialog("Save to vocabulary", inspect_prefill)
buttons_titled(VOCABULARY)[0].invoke()
root.update()
check("passage-length selection is not prefilled", seen_term.get("value") == "",
      repr(seen_term.get("value"))[:120])
check("the field explains why it is empty",
      any("too long" in t for t in seen_term.get("labels", [])),
      str(seen_term.get("labels")))
check("an over-long term is refused", bool(too_long))
check("refusal names the limit",
      bool(too_long) and "80" in str(too_long[0]), str(too_long[:1]))
check("journal untouched after an over-long term",
      open(vocabulary, encoding="utf-8").read() == before)

# The user retypes a real term and the save goes through as normal.
drive_dialog("Save to vocabulary", lambda d: save_vocabulary(d, term="paragraph"))
buttons_titled(VOCABULARY)[0].invoke()
root.update()
check("a retyped term saves normally",
      "## Term: paragraph" in open(vocabulary, encoding="utf-8").read())

# A short selection is still prefilled exactly as before.
close_card(card)
card = make_card("Define This", "  ontology  ", "The study of being.", cfg)
seen_term.clear()


def inspect_only(dialog):
    seen_term["value"] = find_widgets(dialog, tk.Entry)[0].get()
    press(dialog, "Cancel")


drive_dialog("Save to vocabulary", inspect_only)
buttons_titled(VOCABULARY)[0].invoke()
root.update()
check("a short selection is still prefilled (and whitespace-normalised)",
      seen_term.get("value") == "ontology", repr(seen_term.get("value")))
close_card(card)

# ── The vocabulary dialog remembers its own position, separately ─────────────
card = make_card("Define This", "liminal", "A definition.", cfg)
cfg["vocabulary_window_position"] = {"x": 188, "y": 96}
seen_position.clear()
drive_dialog("Save to vocabulary", record_position)
buttons_titled(VOCABULARY)[0].invoke()
root.update()
check("vocabulary dialog reopens at its saved position",
      seen_position.get("x") == 188 and seen_position.get("y") == 96, str(seen_position))
check("vocabulary position kept separate from the reflection one",
      cfg.get("reflection_window_position") != cfg.get("vocabulary_window_position"),
      f"{cfg.get('reflection_window_position')} vs {cfg.get('vocabulary_window_position')}")

cfg["vocabulary_window_position"] = {"x": -30000, "y": -30000}
seen_position.clear()
drive_dialog("Save to vocabulary", record_position)
buttons_titled(VOCABULARY)[0].invoke()
root.update()
check("vocabulary off-screen position falls back to centred",
      seen_position.get("x", -30000) > -1000, str(seen_position))
check("vocabulary position persisted on close",
      isinstance(cfg.get("vocabulary_window_position"), dict)
      and cfg["vocabulary_window_position"]["x"] > -1000,
      str(cfg.get("vocabulary_window_position")))
close_card(card)

# ── Off-screen detection reports a real answer, not a truncated handle ───────
check("an on-screen position is recognised",
      ui_toasts._window_position_visible(root, 120, 120, 540, 500))
check("an off-screen position is rejected",
      not ui_toasts._window_position_visible(root, -30000, -30000, 540, 500))
check("a position straddling the screen edge still counts",
      ui_toasts._window_position_visible(root, -60, 0, 540, 500))

# ── The remembered source reaches disk, not just the live config dict ────────
card = make_card("What's This About?", "Another passage.", "It argues Y.", cfg)
drive_dialog("Add reflection",
             lambda d: fill_reflection(d, "Persisted thought.", "Disk Check Book"))
buttons_titled(REFLECT)[0].invoke()
root.update()
on_disk = json.load(open(c.CONFIG_FILE, encoding="utf-8"))
check("reflection source persisted to the config file",
      on_disk.get("reflection_source") == "Disk Check Book",
      on_disk.get("reflection_source"))
check("reflection position persisted to the config file",
      isinstance(on_disk.get("reflection_window_position"), dict),
      str(on_disk.get("reflection_window_position")))
close_card(card)

# ── A failed follow-up chat must not offer to save an empty definition ───────
card = make_card("Define This", "liminal", "A definition.", cfg)
card.set_text("", select=True)
root.update()
check("an empty generation leaves vocabulary saving disabled",
      str(buttons_titled(VOCABULARY)[0].cget("state")) == "disabled")

toast_win = [w for w in find_widgets(root, tk.Toplevel)
             if w.winfo_exists() and w.title() == "Define This"]
chat_entries = find_widgets(toast_win[0], tk.Entry) if toast_win else []
send_buttons = [b for b in find_widgets(toast_win[0], tk.Button)
                if "Send" in b.cget("text")] if toast_win else []


def _explode(*a, **k):
    raise RuntimeError("backend down")


original_chat = ui_toasts.chat_with_text_stream
ui_toasts.chat_with_text_stream = _explode
if chat_entries and send_buttons:
    chat_entries[0].delete(0, "end")
    chat_entries[0].insert(0, "make it shorter")
    send_buttons[0].invoke()
    for _ in range(150):        # the chat runs on a worker thread
        pump()
        if str(send_buttons[0].cget("state")) == "normal":
            break
        time.sleep(0.02)
ui_toasts.chat_with_text_stream = original_chat
check("the failed chat finished", bool(send_buttons) and
      str(send_buttons[0].cget("state")) == "normal")
check("a failed chat over empty text keeps vocabulary saving disabled",
      str(buttons_titled(VOCABULARY)[0].cget("state")) == "disabled",
      str(buttons_titled(VOCABULARY)[0].cget("state")))
close_card(card)

# ── The journals stay independent of each other ──────────────────────────────
check("vocabulary defaults beside the reflection journal",
      reflections.vocabulary_journal_path(reflection_path=moved)
      == reflections.reflection_journal_path(configured_path=moved).with_name("Vocabulary.md"))
check("an explicitly chosen vocabulary path wins",
      str(reflections.vocabulary_journal_path(
          configured_path=vocabulary, reflection_path=moved)) == vocabulary)

# ── Nothing was written outside the scratch directory ────────────────────────
check("real config never written", not os.path.exists(
    os.path.join(repo, "vedium_config.json.tmp")))
check("config redirected to scratch", c.CONFIG_FILE.startswith(tmpdir))

root.destroy()
shutil.rmtree(tmpdir, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED: {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
