# UI Polish Plan — Spell Daemon

A grounded, incremental plan to make the Settings window and Toasts feel polished
and "premium" **without a framework rewrite**.

## Current state (as-is, verified)

The UI is **plain Tkinter + `ttk`, themed with `sv_ttk` (Sun Valley)**. There is no
CustomTkinter anywhere in the codebase. Key building blocks already in place:

- `ui_settings.py` — `ttk.Notebook` with 4 tabs (Model / Behaviour / Rephrase /
  Appearance), each a `tk.Canvas` with a hover-reveal scrollbar; hand-drawn canvas
  pill toggles (`_make_toggle`, ~line 365); window geometry persisted across sessions.
- `ui_toasts.py` — borderless (`overrideredirect`) toasts with native Windows
  rounded corners (`_apply_windows_decorations`); suggestion text in a `tk.Text`;
  an inline chat panel; auto-close timer; global-hotkey accept/ignore that works
  even when an Electron app holds focus.
- Colors and fonts come from `constants.py` (`BG`, `SURFACE`, `SURF2`, `TEXT`,
  `MUTED`, `ACCENT`, `ACCENT2`, `WARN`, `ERR`, `SUCCESS`, `FONT_B`, `FONT_SIZE_OFFSET`).

The code is competent but **under-finished**: it lacks motion, hover feedback, and
focus affordances. That — not the widget toolkit — is what reads as unpolished.

## Decision: do NOT migrate to CustomTkinter

A CTk rewrite of `ui_settings.py` (~107KB) and `ui_toasts.py` (~39KB) would mean
re-implementing ~2,000 lines of working, subtle code (Electron focus handling,
hover-scroll jitter fixes, geometry persistence) for a styling result obtainable
incrementally. The custom canvas toggles, `tk.Text` diff surface, and borderless
toast windows don't map cleanly onto CTk and would force awkward `tk`-inside-CTk
seams. **Polish the current stack instead.** Revisit CTk only if a genuinely new
requirement (e.g. per-widget light/dark tuples at runtime) makes it worth it.

---

## Phase 1 — High impact, low risk

Ordered by visual payoff per line of code. Each item is self-contained.

### 1.1 Animate the toggle knob
`_make_toggle` (`ui_settings.py` ~line 365) snaps the knob instantly. Animate
`knob_x` over ~6–8 frames with `widget.after(~12ms)`, lerping between the off and
on positions; also cross-fade `track_col` between `SURF2` and `ACCENT2`. Cancel any
in-flight animation id before starting a new one so rapid toggling stays clean.
Self-contained in `_redraw`/`_click`. Biggest "feels premium" win for the effort.

### 1.2 Fade toasts in and out
Tk supports `root.attributes("-alpha", x)`. Ramp alpha `0 → 1` on show and
`1 → 0` inside `close_with` before `root.destroy()` (`ui_toasts.py`, both `_Toast`
and `_EditableToast`). Toasts are the most-seen surface, so this matters most.
Keep the ramp short (~120ms) so accept/ignore still feels instant.

### 1.3 Hover states on buttons
The `tk.Button`s (toast Replace at ~line 164, the Settings Save button) are flat
with no hover feedback. Bind `<Enter>`/`<Leave>` to swap `bg` between `ACCENT` and
`ACCENT2`. Factor a tiny `_hoverable(btn, base, hover)` helper to avoid repetition.

### 1.4 Inline diff in the suggestion toast
`_update_widget_text` (`ui_toasts.py` ~line 116) shows only the corrected sentence.
Both `_original` and `_corrected` are available — compute a word-level diff
(`difflib.SequenceMatcher`) and apply `tk.Text` tags: removed words struck through in
`ERR`, added/changed words in `SUCCESS`. Prettier **and** more useful: the user sees
*what* changed before accepting. Highest-value single feature in this plan.

---

## Phase 2 — Affordances and consistency

### 2.1 Focus ring on entries
Entries (e.g. model-path entry ~line 446) are flat with no focus cue. On
`<FocusIn>` set `highlightthickness=1` / `highlightcolor=ACCENT2`; clear on
`<FocusOut>`. Apply uniformly to all entry fields.

### 2.2 Keyboard-hint labels on toast buttons
Accept/ignore work via global hotkeys but the buttons don't advertise them. Label
them "Replace ✓  Enter" / "Ignore  Esc" so the shortcut is discoverable.

### 2.3 Centralize fonts
Fonts are hardcoded as string literals (`("Segoe UI", 9 + FONT_SIZE_OFFSET, "bold")`,
`("Georgia", ...)`) across both files. Promote to named constants in `constants.py`
(`FONT_TITLE`, `FONT_BODY`, `FONT_SUGGESTION`) beside `FONT_B`, so a theme/scale
change is one edit. Mechanical, do it alongside Phase 1 edits in files already open.

---

## Phase 3 — Optional, only if pushing further

### 3.1 Notebook tab restyle
The `ttk.Notebook` tabs (style config ~line 232) are the most "default-tkinter"
element left. A custom underline-indicator look (selected tab gets an `ACCENT2`
underline instead of a background swap) modernizes the strongest remaining tell.
Higher effort/risk than Phase 1–2 — defer unless the tabs specifically bother you.

### 3.2 Contrast audit for light schemes
Verify text/background contrast on light schemes (e.g. Parchment) meets a sane
threshold so `MUTED` text stays legible. Quick manual pass across all schemes.

---

## Execution notes

- **No new dependencies.** Everything above uses stock `tkinter` + `difflib`
  (stdlib) and existing `constants.py` values.
- **Per-widget, reversible changes.** Each item is independently shippable and easy
  to revert; none touch the buffer/LLM/replacement pipelines.
- **Keep the global-hotkey + Electron-focus behavior intact** when editing toasts —
  it's load-bearing and easy to break. Don't reorder `focus_force`/`after` calls.
- **Test by eye across schemes.** After Phase 1, open Settings and trigger a toast
  under at least one dark (Forest/Charcoal/Slate) and one light scheme.

## Suggested order

1. Toggle animation (1.1)
2. Inline-diff toast (1.4)
3. Toast fade (1.2)
4. Button hover (1.3)
5. Focus rings + keyboard hints + font constants (2.1–2.3)
6. Optional: tab restyle, contrast audit (3.x)
