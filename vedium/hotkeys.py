# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

"""Shared helpers for configurable review-card keyboard shortcuts."""

import sys


DEFAULT_TOAST_ACCEPT_HOTKEY = "ctrl+enter"
TOAST_ACCEPT_HOTKEY_OPTIONS = (
    "ctrl+enter",
    "alt+enter",
    "ctrl+shift+enter",
    "alt+shift+enter",
)


def normalize_toast_accept_hotkey(value: str | None) -> str:
    """Return a supported canonical shortcut, falling back to the default."""
    normalized = str(value or "").strip().lower().replace("cmd+", "ctrl+")
    return (
        normalized
        if normalized in TOAST_ACCEPT_HOTKEY_OPTIONS
        else DEFAULT_TOAST_ACCEPT_HOTKEY
    )


def toast_accept_hotkey_display(value: str | None) -> str:
    """Turn a stored shortcut into a platform-appropriate readable label."""
    parts = normalize_toast_accept_hotkey(value).split("+")
    labels = {
        "ctrl": "Cmd" if sys.platform == "darwin" else "Ctrl",
        "alt": "Option" if sys.platform == "darwin" else "Alt",
        "shift": "Shift",
        "enter": "Enter",
    }
    return "+".join(labels[part] for part in parts)


def toast_accept_hotkey_tk_sequence(value: str | None) -> str:
    """Return the Tk event sequence for the configured shortcut."""
    parts = normalize_toast_accept_hotkey(value).split("+")
    names = {
        "ctrl": "Command" if sys.platform == "darwin" else "Control",
        "alt": "Option" if sys.platform == "darwin" else "Alt",
        "shift": "Shift",
        "enter": "Return",
    }
    return "<" + "-".join(names[part] for part in parts) + ">"


def toast_accept_hotkey_parts(value: str | None) -> tuple[frozenset[str], str]:
    """Split a configured shortcut into modifier names and its base key."""
    parts = normalize_toast_accept_hotkey(value).split("+")
    return frozenset(parts[:-1]), parts[-1]
