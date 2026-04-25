#!/usr/bin/env python3
"""Small bootstrap so launch status can update before heavy GUI imports."""

import datetime
import json
import os
import runpy
import sys
import time
import traceback


LAUNCH_STATUS_PREFIX = ".launch_status_"
LAUNCH_TRACE_PREFIX = ".launch_trace_"


def _token_from_argv(argv):
    args = list(argv or [])
    for idx, arg in enumerate(args):
        if arg == "--launch-token" and idx + 1 < len(args):
            return str(args[idx + 1]).strip()
        if arg.startswith("--launch-token="):
            return arg.split("=", 1)[1].strip()
    return ""


def _status_path(root, token):
    if not token:
        return ""
    return os.path.join(root, f"{LAUNCH_STATUS_PREFIX}{token}.json")


def _trace_path(root, token):
    if not token:
        return ""
    return os.path.join(root, f"{LAUNCH_TRACE_PREFIX}{token}.log")


def _append_trace(root, token, event, detail=""):
    path = _trace_path(root, token)
    if not path:
        return
    line = "\t".join([
        datetime.datetime.now().isoformat(timespec="milliseconds"),
        str(event or "").strip(),
        str(detail or "").replace("\r", " ").replace("\n", " ").strip(),
    ])
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _update_status(root, token, stage, progress=None, close=False, detail=""):
    path = _status_path(root, token)
    if not path:
        return
    payload = {
        "stage": str(stage or "").strip(),
        "progress": progress,
        "close": bool(close),
        "detail": str(detail or "").strip(),
        "updated_at": time.time(),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception:
        pass
    _append_trace(root, token, "bootstrap_status_update", f"stage={payload['stage']} progress={progress} {payload['detail']}")


if __name__ == "__main__":
    root = os.path.dirname(os.path.abspath(__file__))
    token = _token_from_argv(sys.argv[1:])
    os.environ["MUESLI_BOOTSTRAP_STARTED_AT"] = str(time.perf_counter())
    _append_trace(root, token, "python_bootstrap_started", "Python process started before importing muesli_gui.py.")
    _update_status(
        root,
        token,
        "Starting Python...",
        progress=8,
        detail="Python process started. Importing Muesli modules now.",
    )
    gui_path = os.path.join(root, "muesli_gui.py")
    sys.argv[0] = gui_path
    _append_trace(root, token, "python_bootstrap_importing_gui", os.path.basename(gui_path))
    try:
        runpy.run_path(gui_path, run_name="__main__")
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        _append_trace(root, token, "python_bootstrap_failed", detail)
        _append_trace(
            root,
            token,
            "python_bootstrap_traceback",
            traceback.format_exc().replace("\r", " ").replace("\n", " | ")[:2000],
        )
        _update_status(
            root,
            token,
            "Startup failed",
            progress=100,
            detail=detail,
        )
        raise
