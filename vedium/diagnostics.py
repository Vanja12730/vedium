# Copyright (C) 2026 vehab
# SPDX-License-Identifier: GPL-3.0-only

import threading
from collections import deque
from datetime import datetime


_LOCK = threading.Lock()
_EVENTS = deque(maxlen=80)
_THROTTLE = {}
_STATE = {
    "enabled": True,
    "spell_check_mode": "Auto",
    "backend_mode": "local",
    "backend": "None",
    "backend_detail": "",
    "busy": False,
    "last_action": "App started.",
    "last_error": "",
}


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def set_status(**kwargs):
    """Update the shared app status shown in Settings."""
    with _LOCK:
        _STATE.update({k: v for k, v in kwargs.items() if k in _STATE})


def record(component: str, severity: str, message: str, detail: str = ""):
    """Record a short diagnostics event and update the status summary."""
    severity = (severity or "info").lower()
    event = {
        "time": _now(),
        "component": component,
        "severity": severity,
        "message": message,
        "detail": detail,
    }
    with _LOCK:
        _EVENTS.appendleft(event)
        if severity == "error":
            _STATE["last_error"] = message
        elif severity in ("info", "warning"):
            _STATE["last_action"] = message


def record_throttled(component: str, severity: str, message: str, detail: str = "",
                     cooldown_sec: float = 10.0):
    """Record an event at most once per cooldown window for noisy paths."""
    key = (component, severity, message, detail)
    now = datetime.now().timestamp()
    with _LOCK:
        last = _THROTTLE.get(key, 0.0)
        if now - last < cooldown_sec:
            return
        _THROTTLE[key] = now
    record(component, severity, message, detail)


def clear_last_error():
    with _LOCK:
        _STATE["last_error"] = ""


def format_snapshot(cfg: dict | None = None) -> str:
    """Return a plain-text diagnostics summary safe to copy into support notes."""
    snap = snapshot(cfg)
    state = snap["state"]
    lines = [
        "Vedium diagnostics",
        "",
        f"Enabled: {state.get('enabled')}",
        f"Spell-check mode: {state.get('spell_check_mode')}",
        f"Backend mode: {state.get('backend_mode')}",
        f"Backend: {state.get('backend')}",
        f"Busy: {state.get('busy')}",
        f"Last action: {state.get('last_action')}",
        f"Last error: {state.get('last_error') or 'None'}",
        "",
        "Recent events:",
    ]
    events = snap["events"]
    if not events:
        lines.append("No events yet.")
    for event in events[:30]:
        detail = f" - {event['detail']}" if event.get("detail") else ""
        lines.append(
            f"{event['time']} {event['severity'].upper()} "
            f"{event['component']}: {event['message']}{detail}"
        )
    return "\n".join(lines)


def snapshot(cfg: dict | None = None) -> dict:
    """Return a thread-safe status snapshot for UI rendering."""
    with _LOCK:
        state = dict(_STATE)
        events = list(_EVENTS)
    if cfg:
        state["enabled"] = bool(cfg.get("enabled", state["enabled"]))
        state["spell_check_mode"] = cfg.get("spell_check_mode", state["spell_check_mode"])
        state["backend_mode"] = cfg.get("backend_mode", state["backend_mode"])
    return {"state": state, "events": events}
