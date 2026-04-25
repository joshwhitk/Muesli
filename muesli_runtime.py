#!/usr/bin/env python3
"""Runtime-only shared state for the Muesli desktop app and sidecar."""

from __future__ import annotations

import datetime
import json
import os
import tempfile


APP_DIR = os.environ.get("MUESLI_HOME") or os.path.dirname(os.path.abspath(__file__))
RUNTIME_STATE_FILE = os.path.join(APP_DIR, "runtime_state.json")


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
    fd, tmp_path = tempfile.mkstemp(prefix="muesli-runtime-", suffix=".tmp", dir=APP_DIR)
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
