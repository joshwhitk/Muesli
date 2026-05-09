#!/usr/bin/env python3
"""Runtime-only shared state for the Muesli desktop app and sidecar."""

from __future__ import annotations

import datetime
import json
import os
import tempfile

from paths import RUNTIME_DIR, RUNTIME_STATE_FILE


def _default_state():
    return {
        "processing_paused": False,
        "status": "Idle",
        "recording": False,
        "processing": False,
        "summary_backend": "",
        "summary_model": "",
        "app_pid": None,
        "sidecar_pid": None,
        "updated_at": datetime.datetime.now().isoformat(),
    }


def load_runtime_state():
    state = _default_state()
    try:
        with open(RUNTIME_STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            state.update(raw)
    except Exception:
        pass
    return state


def save_runtime_state(state):
    payload = _default_state()
    payload.update(state or {})
    payload["updated_at"] = datetime.datetime.now().isoformat()
    fd, tmp_path = tempfile.mkstemp(prefix="muesli-runtime-", suffix=".tmp", dir=RUNTIME_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, RUNTIME_STATE_FILE)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return payload


def update_runtime_state(**changes):
    state = load_runtime_state()
    state.update(changes)
    return save_runtime_state(state)


def reset_runtime_state():
    return save_runtime_state(_default_state())


# ── Pause / resume ───────────────────────────────────────────────────────────
# Centralised here (not in muesli_gui.py) so muesli.py, muesli_service.py and
# any other worker can respect a pause without depending on the GUI module.

def is_processing_paused():
    return bool(load_runtime_state().get("processing_paused", False))


def set_processing_paused(paused):
    update_runtime_state(processing_paused=bool(paused))
    return bool(paused)


def wait_for_processing_resume(poll_interval=0.25, sleep_fn=None):
    """Block while pause is set. sleep_fn is the time.sleep injection point
    so tests can verify the loop runs without actually sleeping."""
    if sleep_fn is None:
        import time as _time
        sleep_fn = _time.sleep
    waited = False
    while is_processing_paused():
        waited = True
        sleep_fn(poll_interval)
    return waited
