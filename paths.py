"""Canonical filesystem paths for Muesli.

The source tree lives in Dropbox, which is fine for code and saved sessions
but is hostile to transient runtime files: Dropbox can briefly lock files
during sync, which makes os.replace fail and breaks IPC writes. Anything
machine-local (runtime state, in-flight chunk WAVs, IPC artifacts) must live
outside the synced tree.

Layout:
  APP_DIR       — source folder (code, assets, config, saved sessions)
  RUNTIME_DIR   — %LOCALAPPDATA%/muesli on Windows, $XDG_STATE_HOME/muesli or
                  ~/.local/state/muesli elsewhere. Per-machine, never synced.
  CHUNK_DIR     — RUNTIME_DIR/chunks. In-flight live-transcription WAVs.

Override either with the MUESLI_HOME or MUESLI_RUNTIME_DIR env var.
"""

from __future__ import annotations

import os
import platform
import shutil


APP_DIR = os.environ.get("MUESLI_HOME") or os.path.dirname(os.path.abspath(__file__))


def _default_runtime_dir() -> str:
    if platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "muesli")
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "muesli")


RUNTIME_DIR = os.environ.get("MUESLI_RUNTIME_DIR") or _default_runtime_dir()
CHUNK_DIR = os.path.join(RUNTIME_DIR, "chunks")
RUNTIME_STATE_FILE = os.path.join(RUNTIME_DIR, "runtime_state.json")

os.makedirs(RUNTIME_DIR, exist_ok=True)
os.makedirs(CHUNK_DIR, exist_ok=True)


def migrate_legacy_file(legacy_path: str, new_path: str) -> None:
    """One-shot migration from the old in-source location to RUNTIME_DIR.

    Best-effort: if the legacy file is locked or missing, do nothing. The
    runtime state is transient anyway, so failing the migration just means
    the next write recreates it in the new location.
    """
    if not legacy_path or not os.path.exists(legacy_path):
        return
    if os.path.exists(new_path):
        try:
            os.remove(legacy_path)
        except OSError:
            pass
        return
    try:
        shutil.move(legacy_path, new_path)
    except OSError:
        pass


# Migrate the old in-source runtime_state.json on first import.
migrate_legacy_file(os.path.join(APP_DIR, "runtime_state.json"), RUNTIME_STATE_FILE)
