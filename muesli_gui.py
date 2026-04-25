#!/usr/bin/env python3
"""Muesli - AI call recorder and transcriber."""

import tkinter as tk
from tkinter import simpledialog, filedialog, messagebox, ttk
import tkinter.font as tkfont
import threading
import queue
import wave
import subprocess
import os
import sys
import json
import time
import datetime
import re
import math
import base64
import platform
import socket
import ctypes
import importlib.util
import shutil
import urllib.request
import urllib.error
import uuid
import pygame
from muesli_runtime import load_runtime_state, update_runtime_state

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

try:
    import pyaudio
except ImportError:
    pyaudio = None

try:
    import sounddevice as sd
except ImportError:
    sd = None

try:
    import audioop
except ImportError:
    audioop = None

try:
    import numpy as np
except ImportError:
    np = None

try:
    import websocket
except ImportError:
    websocket = None

# ── Single-instance socket ───────────────────────────────────────────────────
_SOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".muesli.sock")
_WIN_IPC_HOST = "127.0.0.1"
_WIN_IPC_PORT = 45873
_WIN_SINGLE_INSTANCE_NAME = "Local\\Muesli.SingleInstance"
_WIN_ERROR_ALREADY_EXISTS = 183
_single_instance_handle = None

IS_WIN = platform.system() == "Windows"

# ── Paths ─────────────────────────────────────────────────────────────────────
APP_DIR      = os.environ.get("MUESLI_HOME") or os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR   = os.path.join(APP_DIR, "assets")
REC_DIR      = os.path.join(APP_DIR, "recordings")   # JSON metadata
CONFIG_FILE  = os.path.join(APP_DIR, "config.json")
PROMPT_FILE  = os.path.join(APP_DIR, "prompt.txt")
ICON_PNG     = os.path.join(ASSETS_DIR, "muesli-icon.png")
ICON_ICO     = os.path.join(ASSETS_DIR, "muesli-icon.ico")
NOTEPAD_ICON_PNG = os.path.join(ASSETS_DIR, "notepad-win.png")
COPY_ICON_PNG = os.path.join(ASSETS_DIR, "copy-win.png")
DESKTOP_SHORTCUT = os.path.join(os.path.expanduser("~"), "Desktop", "Muesli.lnk")
START_MENU_DIR   = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs")
STARTUP_DIR      = os.path.join(START_MENU_DIR, "Startup")
RECORD_SHORTCUT  = os.path.join(START_MENU_DIR, "Muesli Record.lnk")
GUI_LAUNCHER = os.path.join(APP_DIR, "muesli_gui_launcher.vbs")
HOTKEY_AGENT_SCRIPT = os.path.join(APP_DIR, "muesli_hotkey.py")
HOTKEY_AGENT_LAUNCHER = os.path.join(APP_DIR, "muesli_hotkey_launcher.vbs")
HOTKEY_AGENT_PID = os.path.join(APP_DIR, "muesli_hotkey.pid")
HOTKEY_AGENT_SHORTCUT = os.path.join(STARTUP_DIR, "Muesli Hotkey.lnk")
TASKBAR_PIN_DIR  = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Internet Explorer", "Quick Launch", "User Pinned", "TaskBar")
TASKBAR_SHORTCUT = os.path.join(TASKBAR_PIN_DIR, "Muesli.lnk")
LAUNCHER_EXE     = os.path.join(APP_DIR, "dist", "Muesli.exe")
DEFAULT_LAUNCH_HOTKEY = "Ctrl+Shift+`"
APP_USER_MODEL_ID = "Muesli.App"
LAUNCH_STATUS_PREFIX = ".launch_status_"
LAUNCH_TRACE_PREFIX = ".launch_trace_"
os.makedirs(REC_DIR, exist_ok=True)

# ── Audio constants ───────────────────────────────────────────────────────────
RATE     = 16000
CHANNELS = 1
CHUNK    = 1024
FORMAT   = pyaudio.paInt16 if pyaudio else None
LIVE_CHUNK_SECONDS = 10
OPENAI_TRANSCRIPTION_DEFAULT_MODEL = "gpt-4o-transcribe"
OPENAI_TRANSCRIPTION_MODELS = (
    OPENAI_TRANSCRIPTION_DEFAULT_MODEL,
    "gpt-4o-mini-transcribe",
)
OPENAI_TRANSCRIPTION_TIMEOUT_SEC = 900
WHISPER_QUALITY_MODELS = {
    "fast": "medium",
    "high": "large-v3",
}
WHISPER_QUALITY_LABELS = {
    "fast": "Fast",
    "high": "High Quality",
}
_MANAGED_WHISPER_MODELS = set(WHISPER_QUALITY_MODELS.values())
HIGH_QUALITY_MIN_FREE_GB = 20
_CUDA_RUNTIME_PREPPED = False


def _launch_status_path(token):
    token = str(token or "").strip()
    if not token:
        return ""
    return os.path.join(APP_DIR, f"{LAUNCH_STATUS_PREFIX}{token}.json")


def _launch_trace_path(token):
    token = str(token or "").strip()
    if not token:
        return ""
    return os.path.join(APP_DIR, f"{LAUNCH_TRACE_PREFIX}{token}.log")


def _append_launch_trace(token, event, stage="", detail="", elapsed_ms=None, threshold_ms=None, progress=None, flagged=None):
    path = _launch_trace_path(token)
    if not path:
        return
    parts = [
        datetime.datetime.now().isoformat(timespec="milliseconds"),
        str(event or "").strip(),
    ]
    if stage:
        parts.append(f"stage={str(stage).strip()}")
    if progress is not None:
        parts.append(f"progress={progress}")
    if elapsed_ms is not None:
        parts.append(f"elapsed_ms={int(elapsed_ms)}")
    if threshold_ms is not None:
        parts.append(f"threshold_ms={int(threshold_ms)}")
    if flagged is not None:
        parts.append(f"flagged={'yes' if flagged else 'no'}")
    if detail:
        parts.append(str(detail or "").replace("\r", " ").replace("\n", " ").strip())
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\t".join(parts) + "\n")
    except Exception:
        pass


class _LaunchProbe:
    def __init__(self, token):
        self.token = str(token or "").strip()
        self._last_mark = time.perf_counter()

    def begin(self, stage, progress=None, detail=""):
        self._last_mark = time.perf_counter()
        _update_launch_status(self.token, stage, progress=progress, detail=detail)
        _append_launch_trace(self.token, "step_started", stage=stage, detail=detail, progress=progress)

    def end(self, event, stage="", detail="", threshold_ms=None, progress=None):
        elapsed_ms = int((time.perf_counter() - self._last_mark) * 1000)
        flagged = bool(threshold_ms and elapsed_ms > threshold_ms)
        _append_launch_trace(
            self.token,
            event,
            stage=stage,
            detail=detail,
            elapsed_ms=elapsed_ms,
            threshold_ms=threshold_ms,
            progress=progress,
            flagged=flagged,
        )
        return elapsed_ms, flagged


def _update_launch_status(token, stage, progress=None, close=False, detail=""):
    path = _launch_status_path(token)
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
    _append_launch_trace(
        token,
        "status_update",
        stage=payload["stage"],
        detail=payload["detail"],
        progress=progress,
    )


def _finish_launch_status(token):
    path = _launch_status_path(token)
    if not path:
        return
    _update_launch_status(token, "Ready", progress=100, close=True, detail="Startup complete.")
    try:
        os.remove(path)
    except OSError:
        pass
    _append_launch_trace(token, "status_closed", stage="Ready", detail="Launch status file removed.")


def _launch_token_from_argv(argv=None):
    args = list(argv or sys.argv[1:])
    for idx, arg in enumerate(args):
        if arg == "--launch-token" and idx + 1 < len(args):
            return str(args[idx + 1]).strip()
        if arg.startswith("--launch-token="):
            return arg.split("=", 1)[1].strip()
    return ""


def is_processing_paused():
    return bool(load_runtime_state().get("processing_paused", False))


def set_processing_paused(paused):
    update_runtime_state(processing_paused=bool(paused))


def _pid_alive(pid):
    if not IS_WIN:
        return False
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _acquire_single_instance():
    global _single_instance_handle
    if not IS_WIN:
        return True
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, _WIN_SINGLE_INSTANCE_NAME)
    if not handle:
        return True
    if ctypes.windll.kernel32.GetLastError() == _WIN_ERROR_ALREADY_EXISTS:
        ctypes.windll.kernel32.CloseHandle(handle)
        return False
    _single_instance_handle = handle
    return True


def _release_single_instance():
    global _single_instance_handle
    if _single_instance_handle:
        ctypes.windll.kernel32.CloseHandle(_single_instance_handle)
        _single_instance_handle = None


def _wait_for_processing_resume():
    while is_processing_paused():
        time.sleep(0.25)


def _apply_windows_app_identity():
    if not IS_WIN:
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass

# ── Config ────────────────────────────────────────────────────────────────────
def load_config():
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    return _normalize_config(cfg)

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(_normalize_config(cfg), f, indent=2)


def _free_disk_gb(path=None):
    base = path or os.path.expanduser("~")
    try:
        return shutil.disk_usage(base).free / (1024 ** 3)
    except OSError:
        return 0.0


def _default_whisper_quality():
    return "high" if _free_disk_gb() >= HIGH_QUALITY_MIN_FREE_GB else "fast"


def _infer_whisper_quality(model_name):
    model_name = str(model_name or "").lower()
    if model_name in ("large", "large-v2", "large-v3", "turbo"):
        return "high"
    return "fast"


def _normalize_config(cfg):
    normalized = dict(cfg or {})
    if not normalized.get("launch_hotkey"):
        normalized["launch_hotkey"] = DEFAULT_LAUNCH_HOTKEY

    quality = normalized.get("whisper_quality")
    model_name = normalized.get("whisper_model")
    if quality not in WHISPER_QUALITY_MODELS:
        quality = _infer_whisper_quality(model_name) if model_name else _default_whisper_quality()
    normalized["whisper_quality"] = quality
    if not model_name or model_name in _MANAGED_WHISPER_MODELS:
        normalized["whisper_model"] = WHISPER_QUALITY_MODELS[quality]

    whisper_device = str(normalized.get("whisper_device", "auto")).lower()
    normalized["whisper_device"] = whisper_device if whisper_device in ("auto", "cpu", "cuda") else "auto"

    transcription_backend = str(normalized.get("transcription_backend", "local")).lower()
    normalized["transcription_backend"] = transcription_backend if transcription_backend in ("local", "openai", "openai_realtime") else "local"
    openai_model = str(normalized.get("openai_transcription_model", OPENAI_TRANSCRIPTION_DEFAULT_MODEL)).strip()
    normalized["openai_transcription_model"] = openai_model if openai_model in OPENAI_TRANSCRIPTION_MODELS else OPENAI_TRANSCRIPTION_DEFAULT_MODEL
    normalized["openai_api_key"] = str(normalized.get("openai_api_key", "")).strip()

    backend = str(normalized.get("llm_backend", "auto")).lower()
    normalized["llm_backend"] = backend if backend in ("auto", "anthropic", "ollama", "local") else "auto"
    recording_backend = str(normalized.get("audio_input_backend", "auto")).lower()
    normalized["audio_input_backend"] = recording_backend if recording_backend in ("auto", "pyaudio", "sounddevice") else "auto"
    normalized["audio_input_device"] = str(normalized.get("audio_input_device", "")).strip()
    normalized["obsidian_vault_dir"] = str(normalized.get("obsidian_vault_dir", "")).strip()
    normalized["obsidian_export_folder"] = str(normalized.get("obsidian_export_folder", "Muesli")).strip() or "Muesli"
    legacy_prompt = _read_legacy_prompt() if "summary_modes" not in normalized else ""
    normalized["summary_modes"] = _coerce_summary_modes(normalized.get("summary_modes"), legacy_prompt)
    active_mode = str(normalized.get("active_summary_mode") or SUMMARY_MODE_GENERAL_ID).strip().lower()
    if active_mode not in {mode["id"] for mode in normalized["summary_modes"]}:
        active_mode = SUMMARY_MODE_GENERAL_ID
    normalized["active_summary_mode"] = active_mode
    return normalized


def _whisper_quality_summary():
    free_gb = _free_disk_gb()
    drive, _ = os.path.splitdrive(os.path.expanduser("~"))
    drive_label = drive + "\\" if drive else os.path.expanduser("~")
    quality = _default_whisper_quality()
    label = WHISPER_QUALITY_LABELS[quality]
    return f"Default on this PC: {label} ({free_gb:.0f} GB free on {drive_label})"


def _preferred_recording_backend():
    if pyaudio is not None:
        return "pyaudio"
    if sd is not None:
        return "sounddevice"
    return ""


def _list_audio_input_devices(backend=None):
    backend = backend or _preferred_recording_backend()
    options = [{
        "id": "",
        "label": "System Default",
        "backend": backend,
        "name": "System Default",
    }]
    if backend == "pyaudio" and pyaudio is not None:
        pa = pyaudio.PyAudio()
        try:
            try:
                default_index = int(pa.get_default_input_device_info().get("index"))
            except Exception:
                default_index = None
            for idx in range(pa.get_device_count()):
                try:
                    info = pa.get_device_info_by_index(idx)
                except Exception:
                    continue
                if int(info.get("maxInputChannels", 0) or 0) <= 0:
                    continue
                label = str(info.get("name") or f"Input {idx}").strip()
                try:
                    host_name = pa.get_host_api_info_by_index(int(info.get("hostApi", 0))).get("name", "")
                except Exception:
                    host_name = ""
                if host_name:
                    label += f" ({host_name})"
                if default_index == idx:
                    label += " [Default]"
                options.append({
                    "id": str(idx),
                    "label": label,
                    "backend": "pyaudio",
                    "name": str(info.get("name") or label),
                })
        finally:
            pa.terminate()
    elif backend == "sounddevice" and sd is not None:
        try:
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()
            default_raw = sd.default.device
            default_index = default_raw[0] if isinstance(default_raw, (list, tuple)) else default_raw
        except Exception:
            devices = []
            hostapis = []
            default_index = None
        for idx, info in enumerate(devices):
            if int(info.get("max_input_channels", 0) or 0) <= 0:
                continue
            label = str(info.get("name") or f"Input {idx}").strip()
            host_name = ""
            try:
                host_name = hostapis[int(info.get("hostapi", 0))].get("name", "")
            except Exception:
                host_name = ""
            if host_name:
                label += f" ({host_name})"
            if default_index == idx:
                label += " [Default]"
            options.append({
                "id": str(idx),
                "label": label,
                "backend": "sounddevice",
                "name": str(info.get("name") or label),
            })
    return options


def _configured_audio_input():
    cfg = load_config()
    backend = cfg.get("audio_input_backend", "auto")
    device_id = str(cfg.get("audio_input_device", "")).strip()
    return backend, device_id


def _resolve_audio_input_device(backend):
    configured_backend, device_id = _configured_audio_input()
    if not device_id:
        return None
    preferred = _preferred_recording_backend()
    effective_backend = preferred if configured_backend == "auto" else configured_backend
    if effective_backend != backend:
        return None
    options = _list_audio_input_devices(backend)
    valid_ids = {item["id"] for item in options}
    if device_id not in valid_ids:
        return None
    try:
        return int(device_id)
    except ValueError:
        return None


def get_launch_hotkey():
    cfg = load_config()
    return cfg.get("launch_hotkey", DEFAULT_LAUNCH_HOTKEY)


def _windows_shortcut_hotkey(combo):
    parts = [part.strip().upper() for part in combo.split("+") if part.strip()]
    return "+".join(parts)


def _script_target(record=False):
    if os.path.exists(LAUNCHER_EXE):
        args = "--record" if record else ""
        return LAUNCHER_EXE, args
    launcher = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "wscript.exe")
    args = f'"{GUI_LAUNCHER}"'
    if record:
        args += ' "--record"'
    return launcher, args


def _hotkey_agent_target():
    launcher = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "wscript.exe")
    return launcher, f'"{HOTKEY_AGENT_LAUNCHER}"'


def _hidden_subprocess_kwargs():
    if not IS_WIN:
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def restart_windows_hotkey_agent():
    if not IS_WIN or not os.path.exists(HOTKEY_AGENT_SCRIPT):
        return
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Where-Object { ($_.Name -eq 'pythonw.exe' -or $_.Name -eq 'python.exe') -and $_.CommandLine -match 'muesli_hotkey.py' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
            ],
            check=False,
            capture_output=True,
            timeout=10,
            **_hidden_subprocess_kwargs(),
        )
        time.sleep(0.3)
    except Exception:
        pass
    try:
        with open(HOTKEY_AGENT_PID, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except Exception:
        pid = None
    if pid and pid != os.getpid():
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
                timeout=5,
                **_hidden_subprocess_kwargs(),
            )
            time.sleep(0.2)
        except Exception:
            pass
    target = os.path.join(APP_DIR, ".venv", "Scripts", "python.exe")
    if not os.path.exists(target):
        return
    subprocess.Popen(
        [target, HOTKEY_AGENT_SCRIPT],
        cwd=APP_DIR,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True,
    )


def ensure_windows_hotkey_agent_current():
    if not IS_WIN or not os.path.exists(HOTKEY_AGENT_SCRIPT):
        return
    state = load_runtime_state()
    if _pid_alive(state.get("sidecar_pid")):
        return
    restart_windows_hotkey_agent()


def ensure_windows_shortcuts(hotkey=None):
    if not IS_WIN:
        return
    hotkey = hotkey or get_launch_hotkey()
    os.makedirs(START_MENU_DIR, exist_ok=True)
    os.makedirs(STARTUP_DIR, exist_ok=True)
    os.makedirs(TASKBAR_PIN_DIR, exist_ok=True)
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)

    def _write_shortcut(path, record=False, shortcut_hotkey=None, target=None, args=None, description="Muesli"):
        if target is None or args is None:
            target, args = _script_target(record=record)
        icon = ICON_ICO if os.path.exists(ICON_ICO) else "shell32.dll,168"
        ps = f"""
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut('{path}')
$s.TargetPath = '{target}'
$s.Arguments = '{args}'
$s.WorkingDirectory = '{APP_DIR}'
$s.IconLocation = '{icon}'
$s.Description = '{description}'
"""
        if shortcut_hotkey:
            ps += f"$s.Hotkey = '{_windows_shortcut_hotkey(shortcut_hotkey)}'\n"
        else:
            ps += "$s.Hotkey = ''\n"
        ps += "$s.Save()"
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                check=True,
                capture_output=True,
                **_hidden_subprocess_kwargs(),
            )
        except Exception:
            return

    _write_shortcut(DESKTOP_SHORTCUT, record=False)
    _write_shortcut(RECORD_SHORTCUT, record=True, description="Muesli Record")
    hotkey_target, hotkey_args = _hotkey_agent_target()
    _write_shortcut(
        HOTKEY_AGENT_SHORTCUT,
        target=hotkey_target,
        args=hotkey_args,
        description=f"Muesli Hotkey ({hotkey})",
    )
    try:
        import shutil
        shutil.copy2(DESKTOP_SHORTCUT, TASKBAR_SHORTCUT)
    except Exception:
        pass
    restart_windows_hotkey_agent()


def ensure_windows_shortcuts_if_missing():
    if not IS_WIN:
        return
    required = (DESKTOP_SHORTCUT, RECORD_SHORTCUT, HOTKEY_AGENT_SHORTCUT)
    if any(not os.path.exists(path) for path in required):
        ensure_windows_shortcuts()


def capture_hotkey(master, initial_value):
    dialog = tk.Toplevel(master)
    dialog.title("Set Shortcut")
    dialog.configure(bg=DARK_BG)
    dialog.resizable(False, False)
    dialog.transient(master)
    dialog.grab_set()

    modifiers = set()
    result = {"value": None}
    display = tk.StringVar(value=initial_value)

    tk.Label(
        dialog,
        text="Press the shortcut to launch Muesli and start recording",
        bg=DARK_BG,
        fg=FG,
        padx=16,
        pady=12,
    ).pack(fill="x")
    tk.Label(
        dialog,
        textvariable=display,
        font=("Ubuntu Mono", 12, "bold"),
        bg=DARK_BG,
        fg=GREEN,
        padx=16,
        pady=4,
    ).pack(fill="x")

    def _normalize_key(keysym):
        if keysym in ("grave", "quoteleft", "asciitilde"):
            return "`"
        if len(keysym) == 1:
            return keysym.upper()
        if keysym.startswith("F") and keysym[1:].isdigit():
            return keysym.upper()
        return keysym.title()

    def _compose(keysym):
        ordered = [name for name in ("Ctrl", "Alt", "Shift", "Win") if name in modifiers]
        ordered.append(_normalize_key(keysym))
        return "+".join(ordered)

    def _key_press(event):
        key = event.keysym
        lower = key.lower()
        if lower in ("control_l", "control_r"):
            modifiers.add("Ctrl")
            display.set("+".join([name for name in ("Ctrl", "Alt", "Shift", "Win") if name in modifiers]) or initial_value)
            return
        if lower in ("alt_l", "alt_r", "option_l", "option_r"):
            modifiers.add("Alt")
            display.set("+".join([name for name in ("Ctrl", "Alt", "Shift", "Win") if name in modifiers]) or initial_value)
            return
        if lower in ("shift_l", "shift_r"):
            modifiers.add("Shift")
            display.set("+".join([name for name in ("Ctrl", "Alt", "Shift", "Win") if name in modifiers]) or initial_value)
            return
        if lower in ("super_l", "super_r", "win_l", "win_r", "meta_l", "meta_r"):
            modifiers.add("Win")
            display.set("+".join([name for name in ("Ctrl", "Alt", "Shift", "Win") if name in modifiers]) or initial_value)
            return
        if key == "Escape":
            dialog.destroy()
            return
        result["value"] = _compose(key)
        dialog.destroy()

    def _key_release(event):
        lower = event.keysym.lower()
        if lower in ("control_l", "control_r"):
            modifiers.discard("Ctrl")
        elif lower in ("alt_l", "alt_r", "option_l", "option_r"):
            modifiers.discard("Alt")
        elif lower in ("shift_l", "shift_r"):
            modifiers.discard("Shift")
        elif lower in ("super_l", "super_r", "win_l", "win_r", "meta_l", "meta_r"):
            modifiers.discard("Win")

    tk.Button(
        dialog,
        text="Cancel",
        command=dialog.destroy,
        bg=ITEM_BG,
        fg=FG,
        relief="flat",
        padx=12,
        pady=6,
    ).pack(pady=(8, 16))

    dialog.bind("<KeyPress>", _key_press)
    dialog.bind("<KeyRelease>", _key_release)
    dialog.focus_force()
    master.wait_window(dialog)
    return result["value"]


def edit_multiline_text(master, title, label, initial_text="", allow_empty=True):
    dialog = tk.Toplevel(master)
    dialog.title(title)
    dialog.configure(bg=PANEL_BG)
    dialog.geometry("600x360")
    dialog.minsize(480, 280)
    dialog.transient(master)
    dialog.grab_set()

    result = {"value": None}
    body = tk.Frame(dialog, bg=PANEL_BG, padx=18, pady=16)
    body.pack(fill="both", expand=True)

    tk.Label(
        body,
        text=label,
        font=("Segoe UI Semibold", 10),
        bg=PANEL_BG,
        fg=FG,
        anchor="w",
    ).pack(fill="x")
    editor = tk.Text(
        body,
        wrap="word",
        bg=ITEM_BG,
        fg=FG,
        relief="solid",
        bd=1,
        font=("Segoe UI", 10),
        padx=10,
        pady=10,
    )
    editor.pack(fill="both", expand=True, pady=(8, 0))
    editor.insert("1.0", initial_text or "")

    footer = tk.Frame(dialog, bg=PANEL_BG, padx=18, pady=14)
    footer.pack(fill="x")

    def _cancel():
        dialog.destroy()

    def _save():
        value = editor.get("1.0", "end-1c").strip()
        if not allow_empty and not value:
            messagebox.showerror(title, f"{label} cannot be empty.", parent=dialog)
            return
        result["value"] = value
        dialog.destroy()

    tk.Button(
        footer,
        text="Cancel",
        command=_cancel,
        bg=ITEM_BG,
        fg=FG,
        relief="flat",
        padx=12,
        pady=6,
    ).pack(side="right")
    tk.Button(
        footer,
        text="Save",
        command=_save,
        bg=GREEN,
        fg="white",
        relief="flat",
        padx=12,
        pady=6,
    ).pack(side="right", padx=(0, 8))

    dialog.bind("<Escape>", lambda _event: _cancel())
    dialog.bind("<Control-Return>", lambda _event: _save())
    editor.focus_force()
    master.wait_window(dialog)
    return result["value"]


def open_settings_dialog(master):
    cfg = load_config()
    dialog = tk.Toplevel(master)
    dialog.title("Settings")
    dialog.configure(bg=DARK_BG)
    dialog.resizable(False, False)
    dialog.transient(master)
    dialog.grab_set()

    whisper_quality = tk.StringVar(value=cfg.get("whisper_quality", _default_whisper_quality()))
    transcription_backend = tk.StringVar(value=cfg.get("transcription_backend", "local"))
    openai_transcription_model = tk.StringVar(value=cfg.get("openai_transcription_model", OPENAI_TRANSCRIPTION_DEFAULT_MODEL))
    openai_api_key = tk.StringVar(value=cfg.get("openai_api_key", ""))
    llm_backend = tk.StringVar(value=cfg.get("llm_backend", "auto"))
    gguf_models = _list_gguf_models()
    ollama_models = _list_ollama_models()
    llm_local_model = tk.StringVar(value=cfg.get("llm_local_model", gguf_models[0] if gguf_models else ""))
    ollama_model = tk.StringVar(value=cfg.get("ollama_model", _recommend_ollama_model(ollama_models) or ""))
    obsidian_vault_dir = tk.StringVar(value=cfg.get("obsidian_vault_dir", ""))
    obsidian_export_folder = tk.StringVar(value=cfg.get("obsidian_export_folder", "Muesli"))
    input_backend = _preferred_recording_backend()
    input_devices = _list_audio_input_devices(input_backend)
    configured_backend = cfg.get("audio_input_backend", "auto")
    configured_device = str(cfg.get("audio_input_device", "")).strip()
    selected_device_label = "System Default"
    effective_input_backend = input_backend if configured_backend == "auto" else configured_backend
    if effective_input_backend == input_backend:
        for option in input_devices:
            if option["id"] == configured_device:
                selected_device_label = option["label"]
                break
    audio_input = tk.StringVar(value=selected_device_label)
    runtime_transcription_backend_var = tk.StringVar()
    runtime_transcription_model_var = tk.StringVar()
    runtime_backend_var = tk.StringVar()
    runtime_model_var = tk.StringVar()
    runtime_audio_var = tk.StringVar()
    saved = {"ok": False}

    def _section(parent, title, subtitle):
        frame = tk.Frame(parent, bg=PANEL_BG, bd=0, highlightthickness=1, highlightbackground=LINE)
        frame.pack(fill="x", padx=18, pady=(0, 14))
        tk.Label(frame, text=title, font=("Segoe UI Semibold", 10), bg=PANEL_BG, fg=FG).pack(anchor="w", padx=14, pady=(12, 2))
        tk.Label(frame, text=subtitle, font=FONT_SM, bg=PANEL_BG, fg=FG_DIM, wraplength=430, justify="left").pack(anchor="w", padx=14, pady=(0, 10))
        return frame

    def _refresh_runtime_labels(*_args):
        transcription_choice = transcription_backend_map.get(transcription_backend_display.get(), "local")
        backend_choice = backend_map.get(backend_display.get(), "auto")
        preview_cfg = dict(cfg)
        preview_cfg["transcription_backend"] = transcription_choice
        preview_cfg["openai_transcription_model"] = openai_transcription_model.get().strip()
        preview_cfg["openai_api_key"] = openai_api_key.get().strip()
        preview_cfg["llm_backend"] = backend_choice
        preview_cfg["ollama_model"] = ollama_model.get().strip()
        preview_cfg["llm_local_model"] = llm_local_model.get().strip()
        runtime_transcription_backend, runtime_transcription_model = _configured_transcription_runtime_description(preview_cfg)
        runtime_transcription_backend_var.set(runtime_transcription_backend)
        runtime_transcription_model_var.set(runtime_transcription_model or "None")
        runtime_backend, runtime_model = _configured_summary_runtime_description(preview_cfg)
        runtime_backend_var.set(runtime_backend)
        runtime_model_var.set(runtime_model or "None")
        runtime_audio_var.set(f"{backend_label}: {audio_input.get()}")

    tk.Label(
        dialog,
        text="Settings",
        font=("Segoe UI Semibold", 14),
        bg=DARK_BG,
        fg=FG,
        padx=18,
        pady=14,
    ).pack(anchor="w")

    whisper_frame = _section(
        dialog,
        "Transcription",
        "Choose the final transcript backend. Local Whisper keeps everything on this PC. OpenAI Cloud uses GPT-4o Transcribe for the authoritative full transcript after recording stops or when you reprocess/import audio.",
    )
    transcription_backend_map = {
        "Local Whisper": "local",
        "OpenAI Cloud": "openai",
        "OpenAI Realtime": "openai_realtime",
    }
    transcription_backend_display = tk.StringVar(
        value=next((label for label, value in transcription_backend_map.items() if value == transcription_backend.get()), "Local Whisper")
    )
    tk.Label(whisper_frame, text="Transcription backend", bg=PANEL_BG, fg=FG_DIM, font=FONT_SM).pack(anchor="w", padx=14)
    ttk.Combobox(
        whisper_frame,
        textvariable=transcription_backend_display,
        values=list(transcription_backend_map.keys()),
        state="readonly",
        width=18,
    ).pack(anchor="w", padx=14, pady=(4, 10))
    tk.Label(
        whisper_frame,
        text="Whisper quality only affects the local backend and live chunk transcription during recording.",
        bg=PANEL_BG,
        fg=FG_DIM,
        font=FONT_SM,
        wraplength=430,
        justify="left",
    ).pack(anchor="w", padx=14, pady=(0, 8))
    for value, title, detail in (
        ("high", "High Quality", "Uses large-v3 for the best transcript quality."),
        ("fast", "Fast", "Uses medium for quicker turnaround with lower model size."),
    ):
        row = tk.Frame(whisper_frame, bg=PANEL_BG)
        row.pack(fill="x", padx=10, pady=4)
        tk.Radiobutton(
            row,
            text=title,
            value=value,
            variable=whisper_quality,
            bg=PANEL_BG,
            fg=FG,
            selectcolor=ITEM_BG,
            activebackground=PANEL_BG,
            activeforeground=FG,
            font=("Segoe UI Semibold", 9),
        ).pack(side="left")
        tk.Label(row, text=detail, bg=PANEL_BG, fg=FG_DIM, font=FONT_SM).pack(side="left", padx=(8, 0))
    tk.Label(whisper_frame, text=_whisper_quality_summary(), bg=PANEL_BG, fg=FG_LINK, font=FONT_SM).pack(anchor="w", padx=14, pady=(6, 12))
    tk.Label(whisper_frame, text="OpenAI transcription model", bg=PANEL_BG, fg=FG_DIM, font=FONT_SM).pack(anchor="w", padx=14)
    ttk.Combobox(
        whisper_frame,
        textvariable=openai_transcription_model,
        values=list(OPENAI_TRANSCRIPTION_MODELS),
        state="readonly",
        width=32,
    ).pack(anchor="w", padx=14, pady=(4, 8))
    tk.Label(whisper_frame, text="OpenAI API key", bg=PANEL_BG, fg=FG_DIM, font=FONT_SM).pack(anchor="w", padx=14)
    tk.Entry(whisper_frame, textvariable=openai_api_key, font=FONT_SM, relief="flat", bd=0, show="*").pack(fill="x", padx=14, pady=(4, 4), ipady=6)
    tk.Label(
        whisper_frame,
        text="Leave the key blank here if you already provide OPENAI_API_KEY in the environment.",
        bg=PANEL_BG,
        fg=FG_DIM,
        font=FONT_SM,
        wraplength=430,
        justify="left",
    ).pack(anchor="w", padx=14, pady=(0, 12))

    audio_frame = _section(
        dialog,
        "Recording Input",
        "Pick the microphone/input device Muesli should use for new recordings. If the selected device is unavailable later, Muesli falls back to the Windows default input.",
    )
    backend_label = {
        "pyaudio": "PyAudio",
        "sounddevice": "sounddevice",
        "": "Unavailable",
    }.get(input_backend, input_backend)
    tk.Label(
        audio_frame,
        text=f"Active recording backend: {backend_label}",
        bg=PANEL_BG,
        fg=FG_DIM,
        font=FONT_SM,
    ).pack(anchor="w", padx=14, pady=(0, 6))
    ttk.Combobox(
        audio_frame,
        textvariable=audio_input,
        values=[item["label"] for item in input_devices],
        state="readonly",
        width=54,
    ).pack(anchor="w", padx=14, pady=(0, 12))

    llm_frame = _section(
        dialog,
        "Summary LLM",
        "Choose where note summaries come from. If the selected backend is unavailable, Muesli falls back to transcript-only summaries.",
    )
    backend_map = {
        "Auto": "auto",
        "Anthropic": "anthropic",
        "Ollama": "ollama",
        "Local GGUF": "local",
    }
    backend_labels = list(backend_map.keys())
    backend_display = tk.StringVar(value=next((label for label, value in backend_map.items() if value == llm_backend.get()), "Auto"))
    ttk.Combobox(
        llm_frame,
        textvariable=backend_display,
        values=backend_labels,
        state="readonly",
        width=18,
    ).pack(anchor="w", padx=14, pady=(0, 10))
    if ollama_models:
        recommended = _recommend_ollama_model(ollama_models)
        ollama_note = f"Installed models: {len(ollama_models)}"
        if recommended:
            ollama_note += f"  |  Recommended: {recommended}"
        tk.Label(llm_frame, text=ollama_note, bg=PANEL_BG, fg=FG_DIM, font=FONT_SM).pack(anchor="w", padx=14)
        ttk.Combobox(
            llm_frame,
            textvariable=ollama_model,
            values=ollama_models,
            state="readonly",
            width=42,
        ).pack(anchor="w", padx=14, pady=(4, 12))
    else:
        tk.Label(
            llm_frame,
            text="Ollama not detected or no Ollama models are installed.",
            bg=PANEL_BG,
            fg=FG_DIM,
            font=FONT_SM,
        ).pack(anchor="w", padx=14, pady=(4, 12))
    if gguf_models:
        tk.Label(llm_frame, text="Local GGUF model", bg=PANEL_BG, fg=FG_DIM, font=FONT_SM).pack(anchor="w", padx=14)
        ttk.Combobox(
            llm_frame,
            textvariable=llm_local_model,
            values=gguf_models,
            state="readonly",
            width=42,
        ).pack(anchor="w", padx=14, pady=(4, 12))
    else:
        tk.Label(
            llm_frame,
            text="No local GGUF models found in models\\.",
            bg=PANEL_BG,
            fg=FG_DIM,
            font=FONT_SM,
        ).pack(anchor="w", padx=14, pady=(4, 12))

    runtime_frame = _section(
        dialog,
        "Runtime Visibility",
        "Use this to confirm which transcription and summary backends Muesli will use. When summaries run through Ollama, the GPU-heavy work appears in Task Manager as `ollama.exe` / `ollama.exe runner`, not as the Muesli process.",
    )
    tk.Label(runtime_frame, text="Effective transcription backend", bg=PANEL_BG, fg=FG_DIM, font=FONT_SM).pack(anchor="w", padx=14)
    tk.Label(runtime_frame, textvariable=runtime_transcription_backend_var, bg=PANEL_BG, fg=FG, font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=14, pady=(2, 8))
    tk.Label(runtime_frame, text="Effective transcription model", bg=PANEL_BG, fg=FG_DIM, font=FONT_SM).pack(anchor="w", padx=14)
    tk.Label(runtime_frame, textvariable=runtime_transcription_model_var, bg=PANEL_BG, fg=FG, font=("Segoe UI Semibold", 9), wraplength=430, justify="left").pack(anchor="w", padx=14, pady=(2, 8))
    tk.Label(runtime_frame, text="Effective summary backend", bg=PANEL_BG, fg=FG_DIM, font=FONT_SM).pack(anchor="w", padx=14)
    tk.Label(runtime_frame, textvariable=runtime_backend_var, bg=PANEL_BG, fg=FG, font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=14, pady=(2, 8))
    tk.Label(runtime_frame, text="Effective summary model", bg=PANEL_BG, fg=FG_DIM, font=FONT_SM).pack(anchor="w", padx=14)
    tk.Label(runtime_frame, textvariable=runtime_model_var, bg=PANEL_BG, fg=FG, font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=14, pady=(2, 8))
    tk.Label(runtime_frame, text="Recording input", bg=PANEL_BG, fg=FG_DIM, font=FONT_SM).pack(anchor="w", padx=14)
    tk.Label(runtime_frame, textvariable=runtime_audio_var, bg=PANEL_BG, fg=FG, font=("Segoe UI Semibold", 9), wraplength=430, justify="left").pack(anchor="w", padx=14, pady=(2, 12))

    transcription_backend_display.trace_add("write", _refresh_runtime_labels)
    openai_transcription_model.trace_add("write", _refresh_runtime_labels)
    openai_api_key.trace_add("write", _refresh_runtime_labels)
    backend_display.trace_add("write", _refresh_runtime_labels)
    audio_input.trace_add("write", _refresh_runtime_labels)
    ollama_model.trace_add("write", _refresh_runtime_labels)
    llm_local_model.trace_add("write", _refresh_runtime_labels)
    _refresh_runtime_labels()

    obsidian_frame = _section(
        dialog,
        "Obsidian Export",
        "Export the selected session as an Obsidian note with transcript, summary, and copied audio.",
    )
    vault_row = tk.Frame(obsidian_frame, bg=PANEL_BG)
    vault_row.pack(fill="x", padx=14, pady=(0, 8))
    tk.Label(vault_row, text="Vault folder", bg=PANEL_BG, fg=FG_DIM, font=FONT_SM).pack(anchor="w")
    vault_entry_row = tk.Frame(vault_row, bg=PANEL_BG)
    vault_entry_row.pack(fill="x", pady=(4, 0))
    tk.Entry(vault_entry_row, textvariable=obsidian_vault_dir, font=FONT_SM, relief="flat", bd=0).pack(side="left", fill="x", expand=True, ipady=6)

    def _pick_obsidian_vault():
        chosen = filedialog.askdirectory(
            parent=dialog,
            title="Choose Obsidian vault folder",
            initialdir=obsidian_vault_dir.get().strip() or os.path.expanduser("~"),
        )
        if chosen:
            obsidian_vault_dir.set(chosen)

    RoundedButton(
        vault_entry_row,
        text="Browse",
        command=_pick_obsidian_vault,
        font=FONT_SM,
        bg=ITEM_BG,
        fg=FG,
        active_bg=ITEM_ALT,
        shadow="#d7dde7",
        min_width=90,
    ).pack(side="left", padx=(10, 0))
    tk.Label(obsidian_frame, text="Export subfolder inside the vault", bg=PANEL_BG, fg=FG_DIM, font=FONT_SM).pack(anchor="w", padx=14)
    tk.Entry(obsidian_frame, textvariable=obsidian_export_folder, font=FONT_SM, relief="flat", bd=0).pack(fill="x", padx=14, pady=(4, 12), ipady=6)

    buttons = tk.Frame(dialog, bg=DARK_BG)
    buttons.pack(fill="x", padx=18, pady=(4, 18))

    def _save():
        updated = load_config()
        updated["whisper_quality"] = whisper_quality.get()
        updated["whisper_model"] = WHISPER_QUALITY_MODELS[updated["whisper_quality"]]
        updated["transcription_backend"] = transcription_backend_map.get(transcription_backend_display.get(), "local")
        updated["openai_transcription_model"] = openai_transcription_model.get().strip() or OPENAI_TRANSCRIPTION_DEFAULT_MODEL
        updated["openai_api_key"] = openai_api_key.get().strip()
        updated["llm_backend"] = backend_map.get(backend_display.get(), "auto")
        if ollama_models and ollama_model.get() in ollama_models:
            updated["ollama_model"] = ollama_model.get()
        if gguf_models and llm_local_model.get() in gguf_models:
            updated["llm_local_model"] = llm_local_model.get()
        selected_input = next((item for item in input_devices if item["label"] == audio_input.get()), input_devices[0] if input_devices else None)
        if selected_input:
            updated["audio_input_backend"] = selected_input.get("backend", input_backend or "auto") or "auto"
            updated["audio_input_device"] = selected_input.get("id", "")
        updated["obsidian_vault_dir"] = obsidian_vault_dir.get().strip()
        updated["obsidian_export_folder"] = obsidian_export_folder.get().strip() or "Muesli"
        save_config(updated)
        saved["ok"] = True
        dialog.destroy()

    RoundedButton(
        buttons,
        text="Save",
        command=_save,
        font=FONT_SM,
        bg=FG_LINK,
        fg="white",
        active_bg="#1d4ed8",
        shadow="#bfdbfe",
        pad_x=14,
        pad_y=7,
    ).pack(side="right")
    RoundedButton(
        buttons,
        text="Cancel",
        command=dialog.destroy,
        font=FONT_SM,
        bg=ITEM_BG,
        fg=FG,
        active_bg=ITEM_ALT,
        shadow="#d7dde7",
        pad_x=14,
        pad_y=7,
    ).pack(side="right", padx=(0, 10))

    dialog.focus_force()
    master.wait_window(dialog)
    return saved["ok"]

# SHARED_DIR holds exported audio/transcript artifacts.
_LOCAL_SHARED = os.path.join(os.path.expanduser("~"), "Documents", "MuesliData", "analytics", "audio")
_DEFAULT_SHARED = (
    _LOCAL_SHARED
    if IS_WIN else
    "/srv/muesli"
)

def get_shared_dir():
    cfg = load_config()
    d   = cfg.get("shared_dir", _DEFAULT_SHARED)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass  # network share may not be available
    return d

SHARED_DIR = _DEFAULT_SHARED

def ensure_shared_dir_configured():
    """On first Windows run, ask user to confirm or change the shared folder."""
    if not IS_WIN:
        return
    cfg = load_config()
    if "shared_dir" not in cfg:
        chosen = filedialog.askdirectory(
            title="Muesli shared folder (where audio and transcripts are saved)",
            initialdir=_DEFAULT_SHARED if os.path.exists(_DEFAULT_SHARED) else "C:\\"
        )
        if chosen:
            cfg["shared_dir"] = chosen
            save_config(cfg)

_DEFAULT_PROMPT = (
    "Below is a transcript of a recorded conversation.\n\n"
    "{transcript}\n\n"
    "Respond with ONLY a JSON object (no other text, no markdown). "
    "Use exactly these keys:\n"
    '{"title": "1-5 word Title Case name", '
    '"summary": "2-4 sentence summary", '
    '"speakers": 1, '
    '"corrections": "list any likely transcription errors and suggested corrections", '
    '"bugs": "list any software issues or bugs mentioned explicitly in the conversation"}\n'
    "Example response:\n"
    '{"title": "Weekly Team Standup", '
    '"summary": "The team discussed sprint progress and blockers.", '
    '"speakers": 3, '
    '"corrections": "\'postgrest\' should be \'Postgres\', \'rebase\' heard as \'re-base\'", '
    '"bugs": "Login page crashes on Safari. API timeout on large uploads."}'
)

SUMMARY_MODE_GENERAL_ID = "general"


def _summary_prompt(extra_instructions):
    return (
        "Below is a transcript of a recorded conversation.\n\n"
        "{transcript}\n\n"
        "Respond with ONLY a JSON object (no other text, no markdown). "
        "Use exactly these keys:\n"
        '{"title": "1-5 word Title Case name", '
        '"summary": "2-4 sentence summary", '
        '"speakers": 1, '
        '"corrections": "list any likely transcription errors and suggested corrections", '
        '"bugs": "list any software issues or bugs mentioned explicitly in the conversation"}\n'
        f"{extra_instructions}\n"
        "Example response:\n"
        '{"title": "Weekly Team Standup", '
        '"summary": "The team discussed sprint progress and blockers.", '
        '"speakers": 3, '
        '"corrections": "\'postgrest\' should be \'Postgres\', \'rebase\' heard as \'re-base\'", '
        '"bugs": "Login page crashes on Safari. API timeout on large uploads."}'
    )


_DEFAULT_SUMMARY_MODES = [
    {
        "id": SUMMARY_MODE_GENERAL_ID,
        "title": "General",
        "prompt": _DEFAULT_PROMPT,
    },
    {
        "id": "work_meeting",
        "title": "Work Meeting",
        "prompt": _summary_prompt(
            "Optimize for work meetings. Emphasize decisions, owners, deadlines, blockers, and follow-up actions."
        ),
    },
    {
        "id": "user_research",
        "title": "User Research",
        "prompt": _summary_prompt(
            "Optimize for user research. Emphasize participant goals, pain points, notable quotes, observed behavior, and product insights."
        ),
    },
    {
        "id": "accountability_call",
        "title": "Accountability Call",
        "prompt": _summary_prompt(
            "Optimize for an accountability call. Emphasize commitments, promises, deadlines, progress updates, risks, and next check-in items."
        ),
    },
    {
        "id": "negotiation_agreement_call",
        "title": "Negotiation Agreement Call",
        "prompt": _summary_prompt(
            "Optimize for a negotiation or agreement call. Emphasize terms discussed, points of alignment, open issues, concessions, and explicit commitments."
        ),
    },
]


def _read_legacy_prompt():
    if os.path.exists(PROMPT_FILE):
        try:
            with open(PROMPT_FILE, encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return ""
    return ""


def _default_summary_modes(legacy_general_prompt=None):
    modes = [dict(mode) for mode in _DEFAULT_SUMMARY_MODES]
    if legacy_general_prompt:
        modes[0]["prompt"] = legacy_general_prompt
    return modes


def _coerce_summary_modes(raw_modes, legacy_general_prompt=None):
    defaults = _default_summary_modes(legacy_general_prompt)
    normalized = []
    seen_ids = set()
    general_prompt = legacy_general_prompt or defaults[0]["prompt"]
    if isinstance(raw_modes, list):
        for idx, item in enumerate(raw_modes):
            if not isinstance(item, dict):
                continue
            mode_id = str(item.get("id") or "").strip().lower()
            title = str(item.get("title") or "").strip()
            prompt = str(item.get("prompt") or "").strip()
            if not prompt:
                continue
            if mode_id == SUMMARY_MODE_GENERAL_ID:
                general_prompt = prompt
            if not mode_id:
                mode_id = f"custom_{idx + 1}"
            if mode_id in seen_ids:
                continue
            seen_ids.add(mode_id)
            normalized.append({
                "id": mode_id,
                "title": "General" if mode_id == SUMMARY_MODE_GENERAL_ID else (title or f"Mode {idx + 1}"),
                "prompt": prompt,
            })
    result = [{
        "id": SUMMARY_MODE_GENERAL_ID,
        "title": "General",
        "prompt": general_prompt,
    }]
    seen = {SUMMARY_MODE_GENERAL_ID}
    for item in normalized:
        if item["id"] == SUMMARY_MODE_GENERAL_ID:
            continue
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        result.append(item)
    default_ids = {mode["id"] for mode in result}
    for item in defaults[1:]:
        if item["id"] not in default_ids:
            result.append(dict(item))
    return result


def get_summary_modes():
    return [dict(mode) for mode in load_config().get("summary_modes", _default_summary_modes())]


def get_summary_mode(mode_id=None):
    cfg = load_config()
    modes = cfg.get("summary_modes", _default_summary_modes())
    active_id = str(mode_id or cfg.get("active_summary_mode") or SUMMARY_MODE_GENERAL_ID).strip().lower()
    for mode in modes:
        if mode.get("id") == active_id:
            return dict(mode)
    return dict(modes[0])


def get_summary_prompt(mode_id=None):
    return get_summary_mode(mode_id).get("prompt", _DEFAULT_PROMPT)


SHARED_DIR = get_shared_dir()

def open_file(path):
    """Open a file with the system default application."""
    if IS_WIN:
        os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", path])

# ── Helpers ───────────────────────────────────────────────────────────────────
def meta_title(meta):
    """Return display title, handling both old display_name and new title fields."""
    return meta.get("title") or meta.get("display_name") or meta.get("slug", "")


def _can_reprocess_meta(meta):
    slug = str((meta or {}).get("slug", "")).strip()
    return bool(slug and _audio_exists(slug))


def slug_from_title(title):
    s = title.lower().strip()
    s = re.sub(r"[^a-z0-9\s\-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return (s[:60] or "recording")


def default_session_title(started_at):
    try:
        dt = datetime.datetime.fromisoformat(started_at)
        return dt.strftime("%Y-%m-%d %I-%M %p").replace(" 0", " ")
    except Exception:
        return "Recording"


def _clean_sentence(text):
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text.strip(" -,:;")


def _fallback_ai_fields(transcript):
    return {
        "title": "",
        "summary": "",
        "speakers": 0,
        "corrections": "",
        "bugs": "",
    }


def _apply_manual_note_overrides(meta, title, summary):
    if bool(meta.get("title_manual")):
        manual_title = str(meta.get("title", "")).strip()
        if manual_title:
            title = manual_title
    if bool(meta.get("summary_manual")):
        summary = str(meta.get("summary", ""))
    return title, summary


def _build_session_text_body(title, summary, transcript, corrections="", bugs=""):
    txt_body = f"# {title}\n\n"
    if summary:
        txt_body += f"## Summary\n\n{summary}\n\n"
    if corrections:
        txt_body += f"## Transcription Corrections\n\n{corrections}\n\n"
    if bugs:
        txt_body += f"## Bugs / Issues Mentioned\n\n{bugs}\n\n"
    txt_body += f"## Transcript\n\n{transcript}\n"
    return txt_body


def _write_session_text_file(slug, title, summary, transcript, corrections="", bugs=""):
    with open(_txt_path(slug), "w", encoding="utf-8") as f:
        f.write(_build_session_text_body(title, summary, transcript, corrections, bugs))


def _sync_session_text_file(meta):
    slug = str(meta.get("slug", "")).strip()
    if not slug:
        return
    transcript = str(meta.get("transcript", "") or "")
    txt_path = _txt_path(slug)
    if not transcript and not os.path.exists(txt_path):
        return
    _write_session_text_file(
        slug,
        meta_title(meta),
        str(meta.get("summary", "") or ""),
        transcript,
        str(meta.get("corrections", "") or ""),
        str(meta.get("bugs", "") or ""),
    )


def _parse_ai_response(raw):
    text = str(raw or "").strip()
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return json.loads(text)


def _generate_ai_fields(transcript, ollama_timeout=None):
    prompt_text = get_summary_prompt().replace("{transcript}", transcript or "")
    try:
        ai = _parse_ai_response(_llm_generate(prompt_text, ollama_timeout=ollama_timeout))
        if not isinstance(ai, dict):
            raise ValueError("LLM response was not a JSON object")
        return ai
    except Exception:
        return _fallback_ai_fields(transcript)


def datetime_slug():
    return datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

def format_duration(seconds):
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_eta(seconds):
    seconds = max(0, int(math.ceil(seconds or 0)))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"

def format_age(dt):
    delta = datetime.datetime.now().date() - dt.date()
    if delta.days == 0:   return "Today"
    if delta.days == 1:   return "Yesterday"
    if delta.days < 7:    return f"{delta.days} days ago"
    return dt.strftime("%b %d")

def load_recording(meta_path):
    try:
        with open(meta_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def list_recordings():
    results = []
    for fname in os.listdir(REC_DIR):
        if fname.endswith(".json"):
            m = load_recording(os.path.join(REC_DIR, fname))
            if m:
                results.append(m)
    results.sort(key=lambda m: m.get("started_at", ""), reverse=True)
    return results

def _save_meta(meta):
    path = os.path.join(REC_DIR, meta["slug"] + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

def _audio_path(slug):
    """Return MP3 path; fall back to WAV if MP3 doesn't exist."""
    mp3 = os.path.join(SHARED_DIR, slug + ".mp3")
    if os.path.exists(mp3):
        return mp3
    wav = os.path.join(SHARED_DIR, slug + ".wav")
    return wav  # may not exist yet — caller checks

def _audio_exists(slug):
    return (os.path.exists(os.path.join(SHARED_DIR, slug + ".mp3")) or
            os.path.exists(os.path.join(SHARED_DIR, slug + ".wav")))

def _txt_path(slug):
    return os.path.join(SHARED_DIR, slug + ".txt")

def delete_recording(meta):
    """Delete all files associated with a recording (audio, transcript, metadata)."""
    slug = meta.get("slug", "")
    if not slug:
        return
    # Delete audio files (mp3 and wav in shared dir)
    for ext in (".mp3", ".wav"):
        path = os.path.join(SHARED_DIR, slug + ext)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    # Delete transcript txt
    txt = _txt_path(slug)
    if os.path.exists(txt):
        try:
            os.remove(txt)
        except OSError:
            pass
    # Delete metadata JSON
    meta_path = os.path.join(REC_DIR, slug + ".json")
    if os.path.exists(meta_path):
        try:
            os.remove(meta_path)
        except OSError:
            pass
    # Delete local WAV if still present
    local_wav = os.path.join(REC_DIR, slug + ".wav")
    if os.path.exists(local_wav):
        try:
            os.remove(local_wav)
        except OSError:
            pass

# ── AI Processing ─────────────────────────────────────────────────────────────
_whisper_model = None


def _missing_cuda_runtime(exc):
    text = str(exc).lower()
    return any(token in text for token in ("cublas", "cudnn", "cuda", "cufft", "curand"))


def _iter_windows_cuda_dirs():
    if os.name != "nt":
        return []
    dirs = []
    seen = set()
    for path in sys.path:
        if not path:
            continue
        site_packages = os.path.abspath(path)
        if not os.path.isdir(site_packages):
            continue
        if os.path.basename(site_packages).lower() != "site-packages":
            continue
        for rel in (
            os.path.join("nvidia", "cublas", "bin"),
            os.path.join("nvidia", "cuda_runtime", "bin"),
            os.path.join("nvidia", "cuda_nvrtc", "bin"),
            "ctranslate2",
        ):
            dll_dir = os.path.join(site_packages, rel)
            if os.path.isdir(dll_dir) and dll_dir not in seen:
                seen.add(dll_dir)
                dirs.append(dll_dir)
    return dirs


def _prepare_windows_cuda_runtime():
    global _CUDA_RUNTIME_PREPPED
    if os.name != "nt" or _CUDA_RUNTIME_PREPPED:
        return

    dll_dirs = _iter_windows_cuda_dirs()
    if dll_dirs:
        current_path = os.environ.get("PATH", "")
        current_parts = current_path.split(os.pathsep) if current_path else []
        prepend = [dll_dir for dll_dir in dll_dirs if dll_dir not in current_parts]
        if prepend:
            os.environ["PATH"] = os.pathsep.join(prepend + [current_path]) if current_path else os.pathsep.join(prepend)
        for dll_dir in dll_dirs:
            try:
                os.add_dll_directory(dll_dir)
            except (AttributeError, FileNotFoundError, OSError):
                pass

    _CUDA_RUNTIME_PREPPED = True


def _default_whisper_candidates(prefer_cpu=False):
    cfg = load_config()
    model_name = cfg.get("whisper_model", "large-v3")
    force_device = cfg.get("whisper_device", "auto")
    candidates = []

    allow_cuda = not prefer_cpu and force_device in ("auto", "cuda")
    if allow_cuda:
        _prepare_windows_cuda_runtime()
        candidates.append((model_name, "cuda", "float16"))
    if force_device in ("auto", "cpu") or prefer_cpu:
        candidates.append((model_name, "cpu", "int8"))
    fast_model = WHISPER_QUALITY_MODELS["fast"]
    if model_name != fast_model:
        candidates.append((fast_model, "cpu", "int8"))
    return candidates


def _load_whisper_model(prefer_cpu=False):
    if WhisperModel is None:
        raise RuntimeError("faster-whisper is not installed in this environment")
    last_error = None
    for model_name, device, compute_type in _default_whisper_candidates(prefer_cpu=prefer_cpu):
        try:
            return WhisperModel(
                model_name,
                device=device,
                compute_type=compute_type,
            )
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Unable to load Whisper model: {last_error}")


def _get_whisper_model(prefer_cpu=False, reset=False):
    global _whisper_model
    if reset:
        _whisper_model = None
    if _whisper_model is None:
        _whisper_model = _load_whisper_model(prefer_cpu=prefer_cpu)
    return _whisper_model


def _reset_whisper_cache():
    global _whisper_model
    _whisper_model = None


def _transcribe_segments(audio_path, **kwargs):
    try:
        model = _get_whisper_model()
        return model.transcribe(audio_path, **kwargs)
    except Exception as exc:
        if not _missing_cuda_runtime(exc):
            raise
        model = _get_whisper_model(prefer_cpu=True, reset=True)
        return model.transcribe(audio_path, **kwargs)


def _encode_multipart_form(fields, file_field_name, file_path):
    boundary = f"----MuesliBoundary{uuid.uuid4().hex}"
    body = bytearray()

    def _append_text(name, value):
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    for field_name, field_value in fields.items():
        if field_value is None:
            continue
        _append_text(field_name, field_value)

    filename = os.path.basename(file_path)
    ext = os.path.splitext(filename)[1].lower()
    content_type = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".mpga": "audio/mpeg",
        ".mpeg": "audio/mpeg",
        ".webm": "audio/webm",
    }.get(ext, "application/octet-stream")
    with open(file_path, "rb") as f:
        file_bytes = f.read()

    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        f'Content-Disposition: form-data; name="{file_field_name}"; filename="{filename}"\r\n'.encode("utf-8")
    )
    body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
    body.extend(file_bytes)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return boundary, bytes(body)


def _transcribe_openai(audio_path):
    cfg = load_config()
    api_key = _openai_transcription_api_key(cfg)
    if not api_key:
        raise RuntimeError("OpenAI transcription is selected but no OPENAI_API_KEY or saved OpenAI key is configured")

    model_name = _selected_openai_transcription_model(cfg)
    boundary, body = _encode_multipart_form(
        {
            "model": model_name,
            "response_format": "json",
        },
        "file",
        audio_path,
    )
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=OPENAI_TRANSCRIPTION_TIMEOUT_SEC) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI transcription failed ({exc.code}): {error_body[:240]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI transcription failed: {exc.reason}") from exc

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("OpenAI transcription returned invalid JSON") from exc

    transcript = str(data.get("text", "")).strip()
    if not transcript:
        raise RuntimeError("OpenAI transcription returned no text")
    return transcript

# ── Local LLM ────────────────────────────────────────────────────────────────
_llm_model = None
_LLM_MODEL_DIR = os.path.join(APP_DIR, "models")
_OLLAMA_BASE_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")

def _list_gguf_models():
    if not os.path.isdir(_LLM_MODEL_DIR):
        return []
    return [f for f in sorted(os.listdir(_LLM_MODEL_DIR)) if f.endswith(".gguf")]


def _selected_gguf_model_path():
    cfg = load_config()
    selected = cfg.get("llm_local_model", "")
    models = _list_gguf_models()
    if selected and selected in models:
        return os.path.join(_LLM_MODEL_DIR, selected)
    if models:
        return os.path.join(_LLM_MODEL_DIR, models[0])
    return None


def _ollama_request(path, payload=None, timeout=3):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{_OLLAMA_BASE_URL}{path}", data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body else {}


def _list_ollama_models():
    try:
        payload = _ollama_request("/api/tags")
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    models = [m.get("name", "").strip() for m in payload.get("models", [])]
    return [m for m in models if m]


def _recommend_ollama_model(models):
    if not models:
        return None
    lowered = {name.lower(): name for name in models}
    for candidate in (
        "deepseek-r1:8b",
        "qwen3:8b",
        "qwen3.5:4b",
        "qwen3.5:9b",
        "gemma4:e4b",
        "gemma4:4b",
        "llama3.1:8b",
        "mistral:7b",
        "phi4",
    ):
        if candidate in lowered:
            return lowered[candidate]
    filtered = [
        name for name in models
        if not any(token in name.lower() for token in ("coder", "code", "vl", "vision", "embed", "cloud"))
    ]
    return filtered[0] if filtered else models[0]


def _selected_ollama_model():
    cfg = load_config()
    selected = str(cfg.get("ollama_model", "")).strip()
    models = _list_ollama_models()
    if selected and selected in models:
        return selected
    return _recommend_ollama_model(models)


def _transcription_backend(cfg=None):
    cfg = cfg or load_config()
    return str(cfg.get("transcription_backend", "local")).lower()


def _selected_openai_transcription_model(cfg=None):
    cfg = cfg or load_config()
    model_name = str(cfg.get("openai_transcription_model", OPENAI_TRANSCRIPTION_DEFAULT_MODEL)).strip()
    return model_name if model_name in OPENAI_TRANSCRIPTION_MODELS else OPENAI_TRANSCRIPTION_DEFAULT_MODEL


def _openai_transcription_api_key(cfg=None):
    cfg = cfg or load_config()
    return str(cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY") or "").strip()


def _configured_transcription_runtime_description(cfg=None):
    cfg = cfg or load_config()
    backend = _transcription_backend(cfg)
    if backend == "openai":
        model_name = _selected_openai_transcription_model(cfg)
        if _openai_transcription_api_key(cfg):
            return "OpenAI Cloud", model_name
        return "OpenAI Cloud (missing key)", model_name
    if backend == "openai_realtime":
        model_name = _selected_openai_transcription_model(cfg)
        if not _openai_transcription_api_key(cfg):
            return "OpenAI Realtime (missing key)", model_name
        if websocket is None:
            return "OpenAI Realtime (missing websocket-client)", model_name
        return "OpenAI Realtime", model_name
    return "Local Whisper", str(cfg.get("whisper_model", "large-v3")).strip() or "large-v3"


def _configured_summary_runtime_description(cfg=None):
    cfg = cfg or load_config()
    backend = str(cfg.get("llm_backend", "auto")).lower()
    api_key = cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if backend == "anthropic":
        return "Anthropic", "claude-sonnet-4-6"
    if backend == "ollama":
        return "Ollama", _selected_ollama_model() or "(no model found)"
    if backend == "local":
        model_path = _selected_gguf_model_path()
        return "Local GGUF", os.path.basename(model_path) if model_path else "(no GGUF model found)"
    if api_key and importlib.util.find_spec("anthropic") is not None:
        return "Anthropic", "claude-sonnet-4-6"
    ollama_model = _selected_ollama_model()
    if ollama_model:
        return "Ollama", ollama_model
    model_path = _selected_gguf_model_path()
    if model_path and importlib.util.find_spec("llama_cpp") is not None:
        return "Local GGUF", os.path.basename(model_path)
    return "Unavailable", ""


def _obsidian_export_base_dir(cfg=None):
    cfg = cfg or load_config()
    vault_dir = str(cfg.get("obsidian_vault_dir", "")).strip()
    subdir = str(cfg.get("obsidian_export_folder", "Muesli")).strip() or "Muesli"
    if not vault_dir:
        return ""
    return os.path.join(vault_dir, subdir)


def _export_session_to_obsidian(meta, cfg=None):
    cfg = cfg or load_config()
    base_dir = _obsidian_export_base_dir(cfg)
    if not base_dir:
        raise ValueError("Choose an Obsidian vault in Settings before exporting.")
    os.makedirs(base_dir, exist_ok=True)
    audio_dir = os.path.join(base_dir, "audio")
    os.makedirs(audio_dir, exist_ok=True)

    slug = meta.get("slug", "")
    title = meta_title(meta)
    started_at = meta.get("started_at", "")
    duration = format_duration(meta.get("duration", 0))
    status = meta.get("status", "")
    speakers = int(meta.get("speakers", 0) or 0)
    summary_mode_title = meta.get("summary_mode_title", "") or meta.get("summary_mode", "")
    summary = meta.get("summary", "")
    transcript = meta.get("transcript", "")
    note_path = os.path.join(base_dir, slug + ".md")

    audio_path = _audio_path(slug)
    audio_embed = ""
    if audio_path and os.path.exists(audio_path):
        audio_name = os.path.basename(audio_path)
        audio_dest = os.path.join(audio_dir, audio_name)
        if os.path.abspath(audio_path) != os.path.abspath(audio_dest):
            shutil.copy2(audio_path, audio_dest)
        audio_embed = f"![[{os.path.basename(base_dir)}/audio/{audio_name}]]"

    lines = [
        "---",
        f'title: "{title.replace(chr(34), chr(39))}"',
        f'started_at: "{started_at}"',
        f'duration: "{duration}"',
        f'status: "{status}"',
        f"speakers: {speakers}",
        f'slug: "{slug}"',
    ]
    if summary_mode_title:
        lines.append(f'summary_mode: "{summary_mode_title.replace(chr(34), chr(39))}"')
    lines.extend([
        "---",
        "",
        f"# {title}",
        "",
        f"- Recorded: `{started_at}`" if started_at else "- Recorded: unknown",
        f"- Duration: `{duration}`",
    ])
    if speakers:
        lines.append(f"- Speakers: `{speakers}`")
    if summary_mode_title:
        lines.append(f"- Summary mode: `{summary_mode_title}`")
    if audio_embed:
        lines.extend(["", "## Audio", "", audio_embed])
    if summary:
        lines.extend(["", "## Summary", "", summary.strip()])
    if transcript:
        lines.extend(["", "## Transcript", "", transcript.strip()])

    with open(note_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).strip() + "\n")
    return note_path


def _llm_backend():
    cfg = load_config()
    return str(cfg.get("llm_backend", "auto")).lower()


def _llm_status_message():
    backend = _llm_backend()
    api_key = load_config().get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    has_anthropic_pkg = importlib.util.find_spec("anthropic") is not None
    has_llama_pkg = importlib.util.find_spec("llama_cpp") is not None
    local_model_path = _selected_gguf_model_path()
    ollama_model = _selected_ollama_model()
    local_ready = bool(local_model_path and has_llama_pkg)
    anthropic_ready = bool(api_key and has_anthropic_pkg)
    ollama_ready = bool(ollama_model)

    if backend == "anthropic":
        if anthropic_ready:
            return None
        return "LLM unavailable: Anthropic is selected, but the API key or Anthropic package is missing. Summaries will use transcript-only fallback."
    if backend == "ollama":
        if ollama_ready:
            return None
        return "LLM unavailable: Ollama is selected, but the local Ollama service or model is unavailable. Summaries will use transcript-only fallback."
    if backend == "local":
        if local_ready:
            return None
        return "LLM unavailable: Local GGUF is selected, but no usable local model is configured. Summaries will use transcript-only fallback."
    if anthropic_ready or ollama_ready or local_ready:
        return None
    return "LLM unavailable: no Anthropic key, Ollama model, or local GGUF model detected. Summaries will use transcript-only fallback."


def _reset_llm_cache():
    global _llm_model
    _llm_model = None

def _get_llm_model():
    global _llm_model
    if _llm_model is None:
        from llama_cpp import Llama
        model_path = _selected_gguf_model_path()
        if not model_path:
            raise FileNotFoundError(
                f"No .gguf model found in {_LLM_MODEL_DIR}. "
                "Download one, e.g.: huggingface-cli download "
                "bartowski/Phi-3.1-mini-4k-instruct-GGUF "
                "Phi-3.1-mini-4k-instruct-Q4_K_M.gguf "
                f"--local-dir {_LLM_MODEL_DIR}")
        _llm_model = Llama(
            model_path=model_path,
            n_ctx=4096,
            n_threads=4,
            verbose=False,
        )
    return _llm_model

def _llm_generate(prompt_text, ollama_timeout=None):
    """Run prompt through the configured LLM backend."""
    cfg = load_config()
    backend = _llm_backend()
    api_key = cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    anthropic_allowed = backend in ("auto", "anthropic")
    ollama_allowed = backend in ("auto", "ollama")
    local_allowed = backend in ("auto", "local")

    if anthropic_allowed and api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model="claude-sonnet-4-6", max_tokens=512,
                messages=[{"role": "user", "content": prompt_text}]
            )
            return resp.content[0].text.strip()
        except Exception:
            pass

    if backend == "anthropic" and not api_key:
        raise RuntimeError("Anthropic is selected but no API key is configured")

    if ollama_allowed:
        try:
            model_name = _selected_ollama_model()
            if not model_name:
                raise RuntimeError("No Ollama model is installed")
            resp = _ollama_request(
                "/api/generate",
                {"model": model_name, "prompt": prompt_text, "stream": False},
                timeout=ollama_timeout or 420,
            )
            text = str(resp.get("response", "")).strip()
            if text:
                return text
        except Exception:
            pass

    if backend == "ollama":
        raise RuntimeError("Ollama is selected but no usable Ollama model is available")

    if local_allowed:
        try:
            model = _get_llm_model()
            resp = model.create_chat_completion(
                messages=[{"role": "user", "content": prompt_text}],
                max_tokens=512,
                temperature=0.3,
            )
            return resp["choices"][0]["message"]["content"].strip()
        except Exception:
            pass

    if backend == "local":
        raise RuntimeError("Local GGUF is selected but no local model is available")

    raise RuntimeError("No LLM available for the configured backend")


class ChunkPipeline:
    """Process short live audio chunks: whisper transcribe → local LLM summarise.

    Callbacks:
        on_transcribed(n, text) — called after each chunk is transcribed (for live display)
        on_summarised(n)        — called after each chunk is summarised
    """
    def __init__(self, on_transcribed=None, on_summarised=None):
        self._transcripts     = []
        self._summaries       = []
        self._queue           = queue.Queue()
        self._on_transcribed  = on_transcribed
        self._on_summarised   = on_summarised
        self._done            = threading.Event()
        self._submitted       = 0
        self._processed       = 0
        self._total_seconds   = 0.0
        self._stats_lock      = threading.Lock()
        self._worker          = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def submit(self, wav_path):
        with self._stats_lock:
            self._submitted += 1
        self._queue.put(wav_path)

    def finish(self):
        """Signal no more chunks and block until all are processed."""
        self._queue.put(None)
        self._done.wait()

    def _run(self):
        while True:
            path = self._queue.get()
            if path is None:
                self._done.set()
                return
            _wait_for_processing_resume()
            # 1. Transcribe
            started = time.perf_counter()
            try:
                segments, _ = _transcribe_segments(path, beam_size=5)
                text = " ".join(s.text.strip() for s in segments).strip()
            except Exception:
                text = ""
            elapsed = time.perf_counter() - started
            self._transcripts.append(text)
            try:
                os.remove(path)
            except OSError:
                pass
            n = len(self._transcripts)
            with self._stats_lock:
                self._processed = n
                self._total_seconds += elapsed
            if self._on_transcribed:
                self._on_transcribed(n, text)

            # Keep live transcript delivery independent from slower LLM work.
            self._summaries.append("")
            if self._on_summarised:
                self._on_summarised(n)

    def get_transcript(self):
        return " ".join(t for t in self._transcripts if t)

    def get_summaries(self):
        return list(self._summaries)

    def progress_snapshot(self):
        with self._stats_lock:
            submitted = self._submitted
            processed = self._processed
            avg_seconds = (self._total_seconds / processed) if processed else 0.0
        return {
            "submitted": submitted,
            "processed": processed,
            "remaining": max(0, submitted - processed),
            "avg_seconds": avg_seconds,
        }


def _resample_pcm16_mono(pcm_bytes, src_rate, dst_rate):
    if src_rate == dst_rate or not pcm_bytes:
        return pcm_bytes
    if np is None:
        raise RuntimeError("Realtime transcription needs numpy when audioop is unavailable on this Python build")
    samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    if samples.size == 0:
        return b""
    target_len = max(1, int(round(samples.size * float(dst_rate) / float(src_rate))))
    if target_len == samples.size:
        return pcm_bytes
    old_x = np.arange(samples.size, dtype=np.float32)
    new_x = np.linspace(0, samples.size - 1, num=target_len, dtype=np.float32)
    resampled = np.interp(new_x, old_x, samples.astype(np.float32))
    resampled = np.clip(np.rint(resampled), -32768, 32767).astype(np.int16)
    return resampled.tobytes()


class OpenAIRealtimeTranscriber:
    """Stream live PCM audio to OpenAI Realtime transcription and emit incremental turns."""

    WS_URL = "wss://api.openai.com/v1/realtime?intent=transcription"

    def __init__(self, model, api_key, on_delta=None, on_completed=None, on_error=None, on_status=None):
        self._model = model
        self._api_key = api_key
        self._on_delta = on_delta
        self._on_completed = on_completed
        self._on_error = on_error
        self._on_status = on_status
        self._queue = queue.Queue()
        self._closed = threading.Event()
        self._connected = threading.Event()
        self._resample_state = None
        self._ws_app = None
        self._worker = None
        self._running = False

    def start(self):
        if websocket is None:
            raise RuntimeError("OpenAI Realtime transcription requires the websocket-client package")
        headers = [
            f"Authorization: Bearer {self._api_key}",
            "OpenAI-Beta: realtime=v1",
        ]
        self._ws_app = websocket.WebSocketApp(
            self.WS_URL,
            header=headers,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_ws_error,
            on_close=self._on_close,
        )
        self._running = True
        self._worker = threading.Thread(target=self._run_forever, daemon=True)
        self._worker.start()
        if not self._connected.wait(timeout=10):
            raise RuntimeError("OpenAI Realtime transcription failed to connect within 10 seconds")

    def append_audio(self, pcm16_16khz_bytes):
        if not self._running:
            return
        if audioop is not None:
            converted, self._resample_state = audioop.ratecv(
                pcm16_16khz_bytes,
                2,
                CHANNELS,
                RATE,
                24000,
                self._resample_state,
            )
        else:
            converted = _resample_pcm16_mono(pcm16_16khz_bytes, RATE, 24000)
        payload = {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(converted).decode("ascii"),
        }
        self._queue.put(payload)

    def finish(self, timeout=8):
        if not self._running:
            return
        self._queue.put({"type": "input_audio_buffer.commit"})
        self._queue.put({"type": "__close__"})
        self._closed.wait(timeout=timeout)

    def _run_forever(self):
        try:
            self._ws_app.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as exc:
            self._emit_error(str(exc))

    def _on_open(self, ws):
        session_update = {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "noise_reduction": {"type": "near_field"},
                        "transcription": {
                            "model": self._model,
                            "language": "en",
                        },
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.5,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 500,
                        },
                    }
                },
            },
        }
        ws.send(json.dumps(session_update))
        self._connected.set()
        if self._on_status:
            self._on_status("Connected to OpenAI Realtime transcription.")
        sender = threading.Thread(target=self._sender_loop, daemon=True)
        sender.start()

    def _sender_loop(self):
        while self._running:
            payload = self._queue.get()
            if payload.get("type") == "__close__":
                self._running = False
                try:
                    self._ws_app.close()
                except Exception:
                    pass
                return
            try:
                self._ws_app.send(json.dumps(payload))
            except Exception as exc:
                self._emit_error(str(exc))
                self._running = False
                return

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        event_type = str(data.get("type", ""))
        if event_type == "conversation.item.input_audio_transcription.delta":
            if self._on_delta:
                self._on_delta(data.get("item_id"), str(data.get("delta", "")))
        elif event_type == "conversation.item.input_audio_transcription.completed":
            if self._on_completed:
                self._on_completed(data.get("item_id"), str(data.get("transcript", "")))
        elif event_type == "error":
            detail = data.get("error") or data.get("message") or data
            self._emit_error(str(detail))

    def _on_ws_error(self, ws, error):
        self._emit_error(str(error))

    def _on_close(self, ws, status_code, message):
        self._running = False
        self._closed.set()
        if self._on_status and status_code not in (None, 1000):
            self._on_status(f"OpenAI Realtime closed ({status_code}).")

    def _emit_error(self, text):
        if self._on_error:
            self._on_error(text)


def _ffmpeg_available():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True, **_hidden_subprocess_kwargs())
        return True
    except Exception:
        return False

def _file_size_mb(path):
    """Return file size in MB, or 0 if file doesn't exist."""
    try:
        return os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return 0

def process_recording(meta, on_update, transcript=None, summaries=None):
    """Process a recording: convert audio, transcribe (unless provided), summarise with local LLM.

    If `transcript` is provided (e.g. from chunked live transcription), the
    whisper transcription step is skipped.  If `summaries` is provided (from
    chunked LLM processing), the final LLM call merges those instead of
    summarising the full transcript from scratch.
    """
    slug     = meta["slug"]
    wav_path = os.path.join(REC_DIR, slug + ".wav")
    mp3_path = os.path.join(SHARED_DIR, slug + ".mp3")
    wav_dest = os.path.join(SHARED_DIR, slug + ".wav")

    # Check if source file is large (> 1 MB) for progress reporting
    source_size = _file_size_mb(wav_path)
    large_file  = source_size > 1.0

    # WAV → MP3 into shared folder (skip if already done; fall back to WAV copy)
    if not os.path.exists(mp3_path) and not os.path.exists(wav_dest):
        if large_file:
            on_update(meta, f"converting… ({source_size:.1f} MB)")
        else:
            on_update(meta, "converting…")
        if _ffmpeg_available():
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", wav_path,
                     "-ac", "1", "-ar", "16000", "-b:a", "64k", mp3_path],
                    capture_output=True, check=True, **_hidden_subprocess_kwargs()
                )
                if os.path.exists(wav_path):
                    os.remove(wav_path)
            except subprocess.CalledProcessError as e:
                meta["status"] = "error"
                meta["error"]  = f"ffmpeg: {e.stderr.decode()[:120]}"
                _save_meta(meta); on_update(meta, meta["error"]); return
        else:
            # No ffmpeg — copy WAV directly to shared folder
            import shutil
            shutil.copy2(wav_path, wav_dest)
            if os.path.exists(wav_path):
                os.remove(wav_path)

    # Transcribe the authoritative full file when needed.
    transcription_backend = _transcription_backend()
    if transcript is None or transcription_backend == "openai":
        _wait_for_processing_resume()
        # Re-check size of the audio file to be transcribed
        audio_file = mp3_path if os.path.exists(mp3_path) else wav_dest
        if not large_file:
            large_file = _file_size_mb(audio_file) > 1.0

        try:
            if transcription_backend in ("openai", "openai_realtime"):
                prefix = "retranscribing with OpenAI…" if transcript else "transcribing with OpenAI…"
                on_update(meta, f"{prefix} 5%")
                transcript = _transcribe_openai(audio_file)
                on_update(meta, f"{prefix} 100%")
            else:
                if large_file:
                    on_update(meta, "transcribing… 0%")
                else:
                    on_update(meta, "transcribing…")
                segments, info = _transcribe_segments(audio_file, beam_size=5)
                total_duration = info.duration or meta.get("duration", 0) or 1
                collected = []
                for seg in segments:
                    collected.append(seg)
                    if large_file and total_duration > 0:
                        pct = min(int(seg.end / total_duration * 100), 99)
                        on_update(meta, f"transcribing… {pct}%")
                transcript = " ".join(s.text.strip() for s in collected).strip()
        except Exception as e:
            meta["status"] = "error"
            engine_label = "OpenAI" if transcription_backend in ("openai", "openai_realtime") else "Whisper"
            meta["error"]  = f"{engine_label}: {str(e)[:120]}"
            _save_meta(meta); on_update(meta, meta["error"]); return

    # Summarise with local LLM
    _wait_for_processing_resume()
    on_update(meta, "summarising…")
    llm_used = False
    active_summary_mode = get_summary_mode()
    try:
        if summaries and any(summaries):
            # Merge chunk summaries (short input — fast)
            merged = "\n".join(f"- {s}" for s in summaries if s)
            prompt_text = active_summary_mode["prompt"].replace(
                "{transcript}",
                f"[Chunk summaries from a longer recording]\n{merged}")
        else:
            # Full transcript (fallback for non-chunked recordings)
            prompt_text = active_summary_mode["prompt"].replace("{transcript}", transcript)
        ai = _parse_ai_response(_llm_generate(prompt_text))
        llm_used = True
    except Exception:
        ai = _fallback_ai_fields(transcript)

    default_title = default_session_title(meta.get("started_at", ""))
    title       = ai.get("title", "").strip() or default_title
    summary     = ai.get("summary", "").strip() if llm_used else ""
    speakers    = int(ai.get("speakers", 0) or 0) if llm_used else 0
    corrections = ai.get("corrections", "")
    bugs        = ai.get("bugs",        "")
    title, summary = _apply_manual_note_overrides(meta, title, summary)

    # Only rename files when an LLM supplied a real title.
    new_slug = slug
    if (llm_used or meta.get("title_manual")) and title:
        base_slug = slug_from_title(title)
        candidate = base_slug
        i = 2
        while (os.path.exists(os.path.join(REC_DIR, candidate + ".json")) and
               candidate != slug):
            candidate = f"{base_slug}-{i}"
            i += 1
        new_slug = candidate

    # Rename audio file if slug changed
    if new_slug != slug:
        for ext in (".mp3", ".wav"):
            old_a = os.path.join(SHARED_DIR, slug     + ext)
            new_a = os.path.join(SHARED_DIR, new_slug + ext)
            if os.path.exists(old_a):
                os.rename(old_a, new_a)
                break

    _write_session_text_file(new_slug, title, summary, transcript, corrections, bugs)

    # Update & save metadata
    meta.update({
        "slug":         new_slug,
        "title":        title,
        "summary":      summary,
        "transcript":   transcript,
        "speakers":     speakers,
        "corrections":  corrections,
        "bugs":         bugs,
        "status":       "done",
        "summary_mode": active_summary_mode["id"],
        "summary_mode_title": active_summary_mode["title"],
    })
    meta.pop("error", None)
    # Remove old JSON if slug changed
    old_json = os.path.join(REC_DIR, slug + ".json")
    if new_slug != slug and os.path.exists(old_json):
        os.remove(old_json)
    _save_meta(meta)
    on_update(meta, "done")


# ── Recorder ──────────────────────────────────────────────────────────────────
# Reads per live chunk interval (RATE / CHUNK = ~15.6 reads/sec)
_READS_PER_CHUNK = int(LIVE_CHUNK_SECONDS * RATE / CHUNK)

class Recorder:
    def __init__(self):
        if pyaudio is None and sd is None:
            raise RuntimeError("No recording backend is installed on this PC.")
        self._backend   = "pyaudio" if pyaudio is not None else "sounddevice"
        self._pa        = pyaudio.PyAudio() if self._backend == "pyaudio" else None
        self._stream    = None
        self._frames    = []
        self._thread    = None
        self._running   = False
        self._slug      = None
        self._started   = None
        self._on_chunk  = None
        self._on_frame  = None
        self._chunk_idx = 0
        self._sample_width = 2
        self._sd_chunk_frames = []

    def start(self, on_chunk=None, on_frame=None):
        """Begin recording. If `on_chunk` is provided, short WAV chunks are
        saved and passed to the callback for live transcription."""
        self._slug      = datetime_slug()
        self._frames    = []
        self._on_chunk  = on_chunk
        self._on_frame  = on_frame
        self._chunk_idx = 0
        self._running   = True
        self._started   = time.time()
        self._sd_chunk_frames = []
        selected_device = _resolve_audio_input_device(self._backend)
        if self._backend == "pyaudio":
            self._sample_width = self._pa.get_sample_size(FORMAT)
            open_kwargs = dict(
                format=FORMAT,
                channels=CHANNELS,
                rate=RATE,
                input=True,
                frames_per_buffer=CHUNK,
            )
            if selected_device is not None:
                open_kwargs["input_device_index"] = selected_device
            try:
                self._stream = self._pa.open(**open_kwargs)
            except Exception:
                open_kwargs.pop("input_device_index", None)
                self._stream = self._pa.open(**open_kwargs)
            self._thread = threading.Thread(target=self._record, daemon=True)
            self._thread.start()
        else:
            self._sample_width = 2
            stream_kwargs = dict(
                samplerate=RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK,
                callback=self._record_sounddevice,
            )
            if selected_device is not None:
                stream_kwargs["device"] = selected_device
            try:
                self._stream = sd.InputStream(**stream_kwargs)
            except Exception:
                stream_kwargs.pop("device", None)
                self._stream = sd.InputStream(**stream_kwargs)
            self._stream.start()
        return self._slug

    def _record_sounddevice(self, indata, frames, time_info, status):
        if not self._running:
            return
        data = indata.copy().tobytes()
        self._frames.append(data)
        if self._on_frame is not None:
            self._on_frame(data)
        if self._on_chunk is not None:
            self._sd_chunk_frames.append(data)
            if len(self._sd_chunk_frames) >= _READS_PER_CHUNK:
                self._emit_chunk(self._sd_chunk_frames)
                self._sd_chunk_frames = []

    def _record(self):
        chunk_frames = []
        while self._running:
            data = self._stream.read(CHUNK, exception_on_overflow=False)
            self._frames.append(data)
            if self._on_frame is not None:
                self._on_frame(data)
            if self._on_chunk is not None:
                chunk_frames.append(data)
                if len(chunk_frames) >= _READS_PER_CHUNK:
                    self._emit_chunk(chunk_frames)
                    chunk_frames = []
        # Emit any remaining frames as a final chunk
        if chunk_frames and self._on_chunk is not None:
            self._emit_chunk(chunk_frames)

    def _emit_chunk(self, frames):
        self._chunk_idx += 1
        path = os.path.join(REC_DIR, f"{self._slug}_chunk{self._chunk_idx}.wav")
        wf = wave.open(path, "wb")
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(self._sample_width)
        wf.setframerate(RATE)
        wf.writeframes(b"".join(frames))
        wf.close()
        self._on_chunk(path)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        if self._stream:
            if self._backend == "pyaudio":
                self._stream.stop_stream()
                self._stream.close()
            else:
                self._stream.stop()
                self._stream.close()
        if self._backend == "sounddevice" and self._sd_chunk_frames and self._on_chunk is not None:
            self._emit_chunk(self._sd_chunk_frames)
            self._sd_chunk_frames = []

        duration = time.time() - self._started
        wav_path = os.path.join(REC_DIR, self._slug + ".wav")
        wf = wave.open(wav_path, "wb")
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(self._sample_width)
        wf.setframerate(RATE)
        wf.writeframes(b"".join(self._frames))
        wf.close()

        started_at = datetime.datetime.now().isoformat()
        meta = {
            "slug":       self._slug,
            "title":      default_session_title(started_at),
            "started_at": started_at,
            "duration":   duration,
            "status":     "processing",
            "summary":    "",
            "transcript": "",
            "speakers":   0,
        }
        _save_meta(meta)
        return meta

    def elapsed(self):
        return time.time() - self._started if self._started else 0


# ── Player ────────────────────────────────────────────────────────────────────
class Player:
    """Thin wrapper around pygame.mixer.music."""
    def __init__(self):
        pygame.mixer.pre_init(44100, -16, 2, 1024)
        pygame.mixer.init()
        self._path       = None
        self._state      = "stopped"   # stopped | playing | paused
        self._play_epoch = 0.0         # wall time when last play() called
        self._pause_pos  = 0.0         # position (s) at last pause

    def load(self, path):
        if self._path == path:
            return
        self.stop()
        self._path = path
        pygame.mixer.music.load(path)

    def play(self):
        if not self._path: return
        if self._state == "paused":
            pygame.mixer.music.unpause()
            self._play_epoch = time.time() - self._pause_pos
        else:
            pygame.mixer.music.play()
            self._play_epoch = time.time()
            self._pause_pos  = 0.0
        self._state = "playing"

    def pause(self):
        if self._state == "playing":
            pygame.mixer.music.pause()
            self._pause_pos = time.time() - self._play_epoch
            self._state = "paused"

    def stop(self):
        pygame.mixer.music.stop()
        self._state     = "stopped"
        self._pause_pos = 0.0

    def clear(self):
        self.stop()
        try:
            if hasattr(pygame.mixer.music, "unload"):
                pygame.mixer.music.unload()
        except Exception:
            pass
        self._path = None

    def toggle(self):
        if self._state == "playing": self.pause()
        else:                        self.play()

    @property
    def position(self):
        if self._state == "playing":
            return time.time() - self._play_epoch
        return self._pause_pos

    @property
    def state(self):
        # Detect natural end-of-track
        if self._state == "playing" and not pygame.mixer.music.get_busy():
            self._state     = "stopped"
            self._pause_pos = 0.0
        return self._state


# ── GUI constants ─────────────────────────────────────────────────────────────
DARK_BG  = "#f3f4f6"
PANEL_BG = "#fbfbfd"
ITEM_BG  = "#ffffff"
ITEM_SEL = "#dceafe"
FG       = "#111827"
FG_DIM   = "#6b7280"
FG_LINK  = "#2563eb"
RED      = "#d14343"
GREEN    = "#2563eb"
YELLOW   = "#b7791f"
LINE     = "#e5e7eb"
ITEM_ALT = "#eef2f7"
FONT_SM  = ("Segoe UI", 9)
FONT_LG  = ("Segoe UI Semibold", 16)
FONT_MON = ("Consolas", 10)

# ── Microphone icon (pixel art) ──────────────────────────────────────────────
# 48×48 bitmap: '.' = transparent, 'X' = foreground
_MIC_BITMAP = [
    "................................................",  # 0
    "................................................",  # 1
    "................................................",  # 2
    "................XXXXXXXXXXXXXXXX................",  # 3
    "..............XXXXXXXXXXXXXXXXXXXX..............",  # 4
    ".............XXXXXXXXXXXXXXXXXXXXXX.............",  # 5
    "............XXXXXXXXXXXXXXXXXXXXXXXX............",  # 6
    "............XXXXXXXXXXXXXXXXXXXXXXXX............",  # 7
    "............XXXXXXXXXXXXXXXXXXXXXXXX............",  # 8
    "............XXXXXXXXXXXXXXXXXXXXXXXX............",  # 9
    "............XXXXXXXXXXXXXXXXXXXXXXXX............",  # 10
    "............XXXXXXXXXXXXXXXXXXXXXXXX............",  # 11
    "............XXXXXXXXXXXXXXXXXXXXXXXX............",  # 12
    "............XXXXXXXXXXXXXXXXXXXXXXXX............",  # 13
    "............XXXXXXXXXXXXXXXXXXXXXXXX............",  # 14
    "............XXXXXXXXXXXXXXXXXXXXXXXX............",  # 15
    "............XXXXXXXXXXXXXXXXXXXXXXXX............",  # 16
    "............XXXXXXXXXXXXXXXXXXXXXXXX............",  # 17
    "............XXXXXXXXXXXXXXXXXXXXXXXX............",  # 18
    "............XXXXXXXXXXXXXXXXXXXXXXXX............",  # 19
    "............XXXXXXXXXXXXXXXXXXXXXXXX............",  # 20
    ".............XXXXXXXXXXXXXXXXXXXXXX.............",  # 21
    "..............XXXXXXXXXXXXXXXXXXXX..............",  # 22
    "................XXXXXXXXXXXXXXXX................",  # 23
    "........XX..........................XX..........",  # 24
    ".......XXX..........................XXX.........",  # 25
    "......XXX............................XXX........",  # 26
    "......XXX............................XXX........",  # 27
    ".......XXX..........................XXX.........",  # 28
    "........XXX........................XXX..........",  # 29
    ".........XXX......................XXX...........",  # 30
    "..........XXX....................XXX............",  # 31
    "...........XXXX................XXXX.............",  # 32
    ".............XXXX............XXXX...............",  # 33
    "...............XXXXXXXXXXXXXX...................",  # 34
    "................XXXXXXXXXXXX....................",  # 35
    "...................XXXXXX.......................",  # 36
    ".....................XXXX.......................",  # 37
    ".....................XXXX.......................",  # 38
    ".....................XXXX.......................",  # 39
    ".....................XXXX.......................",  # 40
    ".....................XXXX.......................",  # 41
    ".....................XXXX.......................",  # 42
    "...............XXXXXXXXXXXXXXXX.................",  # 43
    "...............XXXXXXXXXXXXXXXX.................",  # 44
    "................................................",  # 45
    "................................................",  # 46
    "................................................",  # 47
]

def _make_mic_icon(master, fg_color):
    """Render the microphone bitmap as a 48×48 PhotoImage."""
    size = 48
    img = tk.PhotoImage(master=master, width=size, height=size)
    # Build row data for put() — one call per row is fast enough
    bg = DARK_BG
    for y, row in enumerate(_MIC_BITMAP[:size]):
        colors = []
        for x, ch in enumerate(row[:size]):
            colors.append(fg_color if ch == "X" else bg)
        img.put("{" + " ".join(colors) + "}", to=(0, y))
    return img


# ── Main App ──────────────────────────────────────────────────────────────────
def _load_app_icon(master):
    """Prefer the project icon asset, with a pixel-art fallback."""
    if os.path.exists(ICON_PNG):
        try:
            return tk.PhotoImage(master=master, file=ICON_PNG)
        except Exception:
            pass
    return _make_mic_icon(master, GREEN)


class ToolTip:
    def __init__(self, widget, text_fn):
        self.widget = widget
        self.text_fn = text_fn if callable(text_fn) else (lambda: str(text_fn))
        self.tip = None
        self._after_id = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._hide()
        self._after_id = self.widget.after(350, self._show)

    def _show(self):
        text = self.text_fn().strip()
        if not text:
            return
        if self.tip:
            return
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.attributes("-topmost", True)
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 10
        self.tip.geometry(f"+{x}+{y}")
        tk.Label(
            self.tip,
            text=text,
            justify="left",
            bg="#111827",
            fg="white",
            font=("Segoe UI", 9),
            padx=10,
            pady=6,
        ).pack()

    def _hide(self, _event=None):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None
        if self.tip:
            self.tip.destroy()
            self.tip = None


class RoundedButton(tk.Canvas):
    def __init__(
        self,
        master,
        text,
        command,
        *,
        font=("Segoe UI Semibold", 10),
        bg="#ffffff",
        fg="#111827",
        active_bg=None,
        disabled_bg="#d1d5db",
        disabled_fg="#94a3b8",
        shadow="#d8dee8",
        radius=14,
        pad_x=16,
        pad_y=9,
        min_width=0,
        tooltip=None,
        icon_image=None,
        icon_gap=8,
    ):
        super().__init__(master, highlightthickness=0, bd=0, bg=master.cget("bg"), relief="flat", cursor="hand2")
        self.command = command
        self.font = tkfont.Font(font=font)
        self.text = text
        self.bg_color = bg
        self.fg_color = fg
        self.active_bg = active_bg or bg
        self.disabled_bg = disabled_bg
        self.disabled_fg = disabled_fg
        self.shadow = shadow
        self.radius = radius
        self.pad_x = pad_x
        self.pad_y = pad_y
        self.min_width = min_width
        self.shadow_offset = 3
        self.enabled = True
        self.hovered = False
        self.pressed = False
        self.icon_image = icon_image
        self.icon_gap = icon_gap
        self._shape_ids = []
        self._text_id = None
        self._draw()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        if tooltip:
            ToolTip(self, tooltip)

    def _rounded_points(self, x1, y1, x2, y2, r):
        return [
            x1 + r, y1,
            x2 - r, y1,
            x2, y1,
            x2, y1 + r,
            x2, y2 - r,
            x2, y2,
            x2 - r, y2,
            x1 + r, y2,
            x1, y2,
            x1, y2 - r,
            x1, y1 + r,
            x1, y1,
        ]

    def _draw_rounded_rect(self, x1, y1, x2, y2, radius, fill):
        return self.create_polygon(
            self._rounded_points(x1, y1, x2, y2, radius),
            smooth=True,
            splinesteps=36,
            fill=fill,
            outline="",
        )

    def _current_bg(self):
        if not self.enabled:
            return self.disabled_bg
        if self.pressed or self.hovered:
            return self.active_bg
        return self.bg_color

    def _current_fg(self):
        return self.fg_color if self.enabled else self.disabled_fg

    def _draw(self):
        self.delete("all")
        text_w = self.font.measure(self.text)
        text_h = self.font.metrics("linespace")
        icon_w = self.icon_image.width() if self.icon_image else 0
        icon_h = self.icon_image.height() if self.icon_image else 0
        content_w = text_w
        if icon_w:
            content_w += icon_w
            if self.text:
                content_w += self.icon_gap
        content_h = max(text_h, icon_h)
        width = max(self.min_width, content_w + (self.pad_x * 2))
        height = content_h + (self.pad_y * 2)
        total_w = width + self.shadow_offset
        total_h = height + self.shadow_offset
        self.config(width=total_w, height=total_h)
        self._draw_rounded_rect(
            self.shadow_offset,
            self.shadow_offset,
            total_w,
            total_h,
            self.radius,
            self.shadow if self.enabled else self.disabled_bg,
        )
        self._draw_rounded_rect(0, 0, width, height, self.radius, self._current_bg())
        cursor_x = width / 2 - (content_w / 2)
        if icon_w:
            self.create_image(cursor_x + (icon_w / 2), height / 2, image=self.icon_image)
            cursor_x += icon_w + (self.icon_gap if self.text else 0)
        if self.text:
            self.create_text(
                cursor_x + (text_w / 2),
                height / 2,
                text=self.text,
                font=self.font,
                fill=self._current_fg(),
            )

    def configure_button(self, *, text=None, bg=None, fg=None, active_bg=None, shadow=None, icon_image=None):
        if text is not None:
            self.text = text
        if bg is not None:
            self.bg_color = bg
        if fg is not None:
            self.fg_color = fg
        if active_bg is not None:
            self.active_bg = active_bg
        if shadow is not None:
            self.shadow = shadow
        if icon_image is not None:
            self.icon_image = icon_image
        self._draw()

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.config(cursor="hand2" if enabled else "")
        self._draw()

    def _on_enter(self, _event):
        if not self.enabled:
            return
        self.hovered = True
        self._draw()

    def _on_leave(self, _event):
        self.hovered = False
        self.pressed = False
        self._draw()

    def _on_press(self, _event):
        if not self.enabled:
            return
        self.pressed = True
        self._draw()

    def _on_release(self, event):
        if not self.enabled:
            return
        was_pressed = self.pressed
        self.pressed = False
        self._draw()
        if was_pressed:
            x1, y1, x2, y2 = 0, 0, self.winfo_width(), self.winfo_height()
            if x1 <= event.x <= x2 and y1 <= event.y <= y2 and self.command:
                self.command()


class SummaryModesDialog(tk.Toplevel):
    def __init__(self, parent, modes):
        super().__init__(parent)
        self.title("Summary Modes")
        self.configure(bg=PANEL_BG)
        self.geometry("860x520")
        self.minsize(760, 460)
        self.transient(parent)
        self.grab_set()
        self.result = None
        self._modes = [dict(mode) for mode in modes]
        self._current_index = None

        shell = tk.Frame(self, bg=PANEL_BG, padx=18, pady=18)
        shell.pack(fill="both", expand=True)

        left = tk.Frame(shell, bg=PANEL_BG)
        left.pack(side="left", fill="y")
        tk.Label(left, text="Buttons", font=("Segoe UI Semibold", 10), bg=PANEL_BG, fg=FG).pack(anchor="w")
        self._listbox = tk.Listbox(
            left,
            bg=ITEM_BG,
            fg=FG,
            selectbackground=ITEM_SEL,
            selectforeground=FG,
            font=("Segoe UI", 10),
            relief="flat",
            bd=0,
            activestyle="none",
            highlightthickness=1,
            highlightbackground=LINE,
            width=24,
            height=18,
        )
        self._listbox.pack(fill="y", pady=(8, 10))
        self._listbox.bind("<<ListboxSelect>>", self._on_select)

        left_buttons = tk.Frame(left, bg=PANEL_BG)
        left_buttons.pack(fill="x")
        RoundedButton(
            left_buttons,
            text="Add",
            command=self._add_mode,
            font=FONT_SM,
            bg=ITEM_BG,
            fg=FG,
            active_bg=ITEM_ALT,
            shadow="#d7dde7",
            pad_x=12,
            pad_y=6,
        ).pack(side="left")
        self._delete_btn = RoundedButton(
            left_buttons,
            text="Remove",
            command=self._delete_mode,
            font=FONT_SM,
            bg="#fee2e2",
            fg="#991b1b",
            active_bg="#fecaca",
            shadow="#f5b8b8",
            pad_x=12,
            pad_y=6,
        )
        self._delete_btn.pack(side="left", padx=(8, 0))

        right = tk.Frame(shell, bg=PANEL_BG)
        right.pack(side="left", fill="both", expand=True, padx=(18, 0))
        tk.Label(right, text="Button Title", font=("Segoe UI Semibold", 10), bg=PANEL_BG, fg=FG).pack(anchor="w")
        self._title_var = tk.StringVar()
        self._title_entry = tk.Entry(right, textvariable=self._title_var, font=("Segoe UI", 10), relief="solid", bd=1)
        self._title_entry.pack(fill="x", pady=(8, 14))

        tk.Label(right, text="Prompt", font=("Segoe UI Semibold", 10), bg=PANEL_BG, fg=FG).pack(anchor="w")
        self._prompt_txt = tk.Text(
            right,
            wrap="word",
            bg=ITEM_BG,
            fg=FG,
            relief="solid",
            bd=1,
            font=("Consolas", 10),
            padx=10,
            pady=10,
        )
        self._prompt_txt.pack(fill="both", expand=True, pady=(8, 0))

        footer = tk.Frame(self, bg=PANEL_BG, padx=18, pady=14)
        footer.pack(fill="x")
        self._status_var = tk.StringVar(value="General always exists and can have its prompt edited.")
        tk.Label(footer, textvariable=self._status_var, bg=PANEL_BG, fg=FG_DIM, anchor="w", font=FONT_SM).pack(side="left")
        RoundedButton(
            footer,
            text="Cancel",
            command=self.destroy,
            font=FONT_SM,
            bg=ITEM_BG,
            fg=FG,
            active_bg=ITEM_ALT,
            shadow="#d7dde7",
            pad_x=12,
            pad_y=6,
        ).pack(side="right")
        RoundedButton(
            footer,
            text="Save",
            command=self._save_and_close,
            font=FONT_SM,
            bg=GREEN,
            fg="white",
            active_bg="#1d4ed8",
            shadow="#b9d0ff",
            pad_x=12,
            pad_y=6,
        ).pack(side="right", padx=(0, 8))

        self._refresh_list()
        self._select_index(0)
        self.wait_window(self)

    def _refresh_list(self):
        current = self._current_index
        self._listbox.delete(0, "end")
        for mode in self._modes:
            self._listbox.insert("end", mode["title"])
        if current is not None and 0 <= current < len(self._modes):
            self._select_index(current)

    def _select_index(self, index):
        if not self._modes:
            self._current_index = None
            self._title_var.set("")
            self._prompt_txt.delete("1.0", "end")
            return
        self._current_index = max(0, min(index, len(self._modes) - 1))
        self._listbox.selection_clear(0, "end")
        self._listbox.selection_set(self._current_index)
        self._load_current()

    def _load_current(self):
        if self._current_index is None:
            return
        mode = self._modes[self._current_index]
        self._title_var.set(mode["title"])
        self._prompt_txt.delete("1.0", "end")
        self._prompt_txt.insert("1.0", mode["prompt"])
        is_general = mode["id"] == SUMMARY_MODE_GENERAL_ID
        self._title_entry.configure(state="disabled" if is_general else "normal")
        self._delete_btn.set_enabled(not is_general)
        self._status_var.set(
            "General stays in the list; edit its prompt here."
            if is_general else
            "Custom button titles and prompts are editable."
        )

    def _save_current_fields(self):
        if self._current_index is None:
            return True
        mode = self._modes[self._current_index]
        title = "General" if mode["id"] == SUMMARY_MODE_GENERAL_ID else self._title_var.get().strip()
        prompt = self._prompt_txt.get("1.0", "end").strip()
        if not title:
            messagebox.showerror("Missing Title", "Each summary button needs a title.", parent=self)
            return False
        if not prompt:
            messagebox.showerror("Missing Prompt", "Each summary button needs a prompt.", parent=self)
            return False
        mode["title"] = title
        mode["prompt"] = prompt
        self._listbox.delete(self._current_index)
        self._listbox.insert(self._current_index, title)
        self._listbox.selection_set(self._current_index)
        return True

    def _on_select(self, _event=None):
        sel = self._listbox.curselection()
        if not sel:
            return
        if self._current_index is not None and not self._save_current_fields():
            self._select_index(self._current_index)
            return
        self._current_index = sel[0]
        self._load_current()

    def _next_custom_mode_id(self):
        taken = {mode["id"] for mode in self._modes}
        idx = 1
        while True:
            candidate = f"custom_{idx}"
            if candidate not in taken:
                return candidate
            idx += 1

    def _add_mode(self):
        if self._current_index is not None and not self._save_current_fields():
            return
        general_prompt = self._modes[0]["prompt"] if self._modes else _DEFAULT_PROMPT
        self._modes.append({
            "id": self._next_custom_mode_id(),
            "title": "New Mode",
            "prompt": general_prompt,
        })
        self._refresh_list()
        self._select_index(len(self._modes) - 1)
        self._title_entry.focus_set()
        self._title_entry.selection_range(0, "end")

    def _delete_mode(self):
        if self._current_index is None:
            return
        mode = self._modes[self._current_index]
        if mode["id"] == SUMMARY_MODE_GENERAL_ID:
            return
        del self._modes[self._current_index]
        self._refresh_list()
        self._select_index(max(0, self._current_index - 1))

    def _save_and_close(self):
        if not self._save_current_fields():
            return
        titles = [mode["title"].strip().lower() for mode in self._modes]
        if len(titles) != len(set(titles)):
            messagebox.showerror("Duplicate Titles", "Summary button titles must be unique.", parent=self)
            return
        self.result = [dict(mode) for mode in self._modes]
        self.destroy()


class MuesliApp(tk.Tk):
    def __init__(self, launch_token="", launch_probe=None):
        super().__init__(className="muesli")
        self._launch_token = str(launch_token or "").strip()
        self._launch_probe = launch_probe or _LaunchProbe(self._launch_token)
        self._launch_probe.begin(
            "Building Muesli window...",
            progress=35,
            detail="Creating the main window and Tk controls.",
        )
        self.title("Muesli")
        self.geometry("960x660")
        self.minsize(720, 480)
        self.configure(bg=DARK_BG)
        if IS_WIN and os.path.exists(ICON_ICO):
            try:
                self.iconbitmap(default=ICON_ICO)
            except Exception:
                pass

        self._recording_available = (pyaudio is not None) or (sd is not None)
        self._recorder   = Recorder() if self._recording_available else None
        self._player     = Player()
        self._recording  = False
        self._cur_meta   = None
        self._timer_id   = None
        self._recordings = []
        self._live_transcript = ""   # accumulated transcript during recording
        self._live_chunks_transcribed = 0
        self._live_recording_slug = None
        self._live_generation = 0
        self._chunk_pipeline = None
        self._realtime_transcriber = None
        self._realtime_turn_order = []
        self._realtime_turns = {}
        self._live_preview = None
        self._live_manual_title = ""
        self._live_manual_title_active = False
        self._live_manual_summary = ""
        self._live_manual_summary_active = False
        self._provisional_summary_started = False
        self._provisional_generation = 0
        self._provisional_transcript_seed = ""
        self._resume_progress = {}   # slug -> "transcribing 45%" for list labels
        cfg = load_config()
        self._summary_modes = [dict(mode) for mode in cfg.get("summary_modes", _default_summary_modes())]
        self._active_summary_mode = cfg.get("active_summary_mode", SUMMARY_MODE_GENERAL_ID)
        self._resummary_active = False
        self._reprocess_active = False
        self._launch_probe.end(
            "window_created",
            stage="Building Muesli window...",
            detail="Tk root, geometry, and early state are ready.",
            threshold_ms=1500,
            progress=35,
        )

        # Taskbar icons — idle and recording states
        self._launch_probe.begin(
            "Loading icons and shortcuts...",
            progress=45,
            detail="Reading icon assets and checking Windows launch integration.",
        )
        self._icon_idle = _load_app_icon(self)
        self._icon_rec  = self._icon_idle
        self._notepad_icon = self._load_detail_icon(NOTEPAD_ICON_PNG)
        self._copy_icon = self._load_detail_icon(COPY_ICON_PNG)
        self.wm_iconphoto(True, self._icon_idle)
        ensure_windows_shortcuts_if_missing()
        self._launch_probe.end(
            "icons_and_shortcuts_ready",
            stage="Loading icons and shortcuts...",
            detail="Icons loaded and launch integration checked.",
            threshold_ms=1200,
            progress=45,
        )
        if IS_WIN:
            self.after(750, ensure_windows_hotkey_agent_current)

        self._launch_probe.begin(
            "Building controls...",
            progress=58,
            detail="Creating the session list, detail pane, and toolbar controls.",
        )
        self._build_ui()
        self._launch_probe.end(
            "controls_built",
            stage="Building controls...",
            detail="The main UI controls are attached.",
            threshold_ms=2200,
            progress=58,
        )
        self._bind_shortcuts()
        self._cleanup_stale()
        self._launch_probe.begin(
            "Loading sessions...",
            progress=72,
            detail="Scanning saved recordings and refreshing the session list.",
        )
        self._refresh_list()
        self._launch_probe.end(
            "sessions_loaded",
            stage="Loading sessions...",
            detail=f"Loaded {len(self._recordings)} sessions into the list.",
            threshold_ms=2500,
            progress=72,
        )
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # Listen for commands from second instances (single-instance pattern)
        self._launch_probe.begin(
            "Starting app services...",
            progress=84,
            detail="Starting the IPC listener and runtime status polling.",
        )
        self._start_ipc_listener()
        self.after(250, self._poll_runtime_state)
        self.after(120, self._mark_launch_ready)
        self._launch_probe.end(
            "services_started",
            stage="Starting app services...",
            detail="IPC listener and runtime polling are active.",
            threshold_ms=1200,
            progress=84,
        )
        # Resume interrupted recordings in background
        self._status_var.set("Starting...")
        self._launch_probe.begin(
            "Finalizing startup...",
            progress=92,
            detail="Handing off any interrupted recordings to the background worker.",
        )
        threading.Thread(target=self._startup_background, daemon=True).start()
        self._launch_probe.end(
            "startup_background_started",
            stage="Finalizing startup...",
            detail="Background startup worker launched.",
            threshold_ms=800,
            progress=92,
        )
        # Auto-start recording if launched with --record
        if "--record" in sys.argv:
            self.after(500, self._auto_start_recording)

    # ── Single-instance IPC ─────────────────────────────────────────────────────
    def _start_ipc_listener(self):
        """Listen on a Unix socket for commands from second instances."""
        if IS_WIN:
            self._ipc_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._ipc_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self._ipc_sock.bind((_WIN_IPC_HOST, _WIN_IPC_PORT))
            except OSError:
                self._ipc_sock.close()
                self._ipc_sock = None
                return
            self._ipc_sock.listen(1)
            threading.Thread(target=self._ipc_loop, daemon=True).start()
            return
        if not hasattr(socket, "AF_UNIX"):
            self._ipc_sock = None
            return
        # Remove stale socket
        try:
            os.unlink(_SOCK_PATH)
        except FileNotFoundError:
            pass
        self._ipc_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._ipc_sock.bind(_SOCK_PATH)
        self._ipc_sock.listen(1)
        threading.Thread(target=self._ipc_loop, daemon=True).start()

    def _ipc_loop(self):
        """Accept connections and dispatch commands."""
        while True:
            try:
                conn, _ = self._ipc_sock.accept()
                data = conn.recv(256).decode().strip()
                conn.close()
                if data == "record":
                    self.after(0, self._auto_start_recording)
                elif data == "show":
                    self.after(0, self._show_window)
                elif data == "quit":
                    self.after(0, self._on_close)
                elif data == "toggle":
                    self.after(0, self._toggle_recording)
            except Exception:
                break

    def _show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _mark_launch_ready(self):
        self._launch_probe.end(
            "window_ready",
            stage="Ready",
            detail="Main window reached the ready callback.",
            threshold_ms=2000,
            progress=100,
        )
        _finish_launch_status(self._launch_token)

    def _auto_start_recording(self):
        """Start a new recording, raising the window first."""
        self._show_window()
        if not self._recording:
            self._start_recording()

    def _on_close(self):
        """Graceful shutdown: stop recording, stop playback, clean up socket."""
        if self._recording:
            # Stop the recorder immediately so the WAV is flushed
            self._recording = False
            if self._timer_id:
                self.after_cancel(self._timer_id)
            try:
                meta = self._recorder.stop()
                # Mark as error so it's obvious it was interrupted
                meta["status"] = "error"
                meta["error"]  = "Interrupted — app closed during processing"
                _save_meta(meta)
            except Exception:
                pass
        self._player.stop()
        # Clean up IPC socket
        try:
            if self._ipc_sock:
                self._ipc_sock.close()
            if not IS_WIN:
                os.unlink(_SOCK_PATH)
        except Exception:
            pass
        # Clean up leftover chunk WAV files
        for f in os.listdir(REC_DIR):
            if "_chunk" in f and f.endswith(".wav"):
                try:
                    os.remove(os.path.join(REC_DIR, f))
                except OSError:
                    pass
        update_runtime_state(
            app_pid=None,
            recording=False,
            processing=False,
            status="Idle",
        )
        self.destroy()

    def _cleanup_stale(self):
        """On launch, clean up chunk WAVs and mark interrupted sessions for resume."""
        # Clean up chunk WAVs
        for f in os.listdir(REC_DIR):
            if "_chunk" in f and f.endswith(".wav"):
                try:
                    os.remove(os.path.join(REC_DIR, f))
                except OSError:
                    pass
        # Find interrupted sessions and flip them to "processing" so the list
        # shows ⟳ instead of ✗ while they're being resumed
        self._interrupted = []
        for fname in os.listdir(REC_DIR):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(REC_DIR, fname)
            meta = load_recording(path)
            if not meta:
                continue
            status = meta.get("status", "")
            error = meta.get("error", "")
            if status == "processing" or (status == "error" and "Interrupted" in error):
                meta["status"] = "processing"
                meta.pop("error", None)
                _save_meta(meta)
                self._interrupted.append(meta)

    def _startup_background(self):
        """Single background thread: resume interrupted recordings.
        LLM is no longer preloaded — Claude API is preferred (fast), local LLM is fallback only."""
        # Resume interrupted recordings
        if not self._interrupted:
            self.after(0, lambda: self._status_var.set("Ready"))
            return

        n = len(self._interrupted)
        self._resume_active = True
        self.after(0, lambda: self._status_var.set(
            f"Resuming {n} interrupted recording{'s' if n != 1 else ''}…"))

        from muesli import Muesli
        m = Muesli()
        done = 0
        for i, meta in enumerate(self._interrupted, 1):
            slug = meta.get("slug", "")
            audio = m._find_audio_for_slug(slug)
            if not audio:
                meta["status"] = "error"
                meta["error"] = "No audio file found"
                _save_meta(meta)
                self._resume_progress.pop(slug, None)
                self.after(0, self._refresh_list)
                continue
            self._resume_progress[slug] = "starting"
            self.after(0, self._redraw_labels)
            try:
                m._resume_session(meta, audio,
                    on_progress=lambda s, stage: self._on_resume_progress(s, stage))
                done += 1
            except Exception as e:
                meta["status"] = "error"
                meta["error"] = f"Resume failed: {str(e)[:200]}"
                _save_meta(meta)
            self._resume_progress.pop(slug, None)
            self.after(0, self._refresh_list)

        self._resume_active = False
        msg = f"Resumed {done} recording{'s' if done != 1 else ''}" if done else "Ready"
        self.after(0, lambda: self._status_var.set(msg))
        self.after(0, self._refresh_list)
        self.after(5000, lambda: self._status_var.set("Ready"))
        self._interrupted = []

    def _on_resume_progress(self, slug, stage):
        """Called from resume thread with per-session progress updates."""
        self._resume_progress[slug] = stage
        self.after(0, self._redraw_labels)

    def _summary_mode_by_id(self, mode_id=None):
        target_id = str(mode_id or self._active_summary_mode or SUMMARY_MODE_GENERAL_ID).strip().lower()
        for mode in self._summary_modes:
            if mode.get("id") == target_id:
                return dict(mode)
        return dict(self._summary_modes[0])

    def _save_summary_mode_config(self):
        cfg = load_config()
        cfg["summary_modes"] = [dict(mode) for mode in self._summary_modes]
        cfg["active_summary_mode"] = self._active_summary_mode
        save_config(cfg)

    def _render_summary_mode_buttons(self):
        if not hasattr(self, "_summary_modes_frame"):
            return
        for child in self._summary_modes_frame.winfo_children():
            child.destroy()
        for mode in self._summary_modes:
            is_active = mode["id"] == self._active_summary_mode
            RoundedButton(
                self._summary_modes_frame,
                text=mode["title"],
                command=lambda mid=mode["id"]: self._on_summary_mode_button(mid),
                font=FONT_SM,
                bg=GREEN if is_active else ITEM_BG,
                fg="white" if is_active else FG,
                active_bg="#1d4ed8" if is_active else ITEM_ALT,
                shadow="#b9d0ff" if is_active else "#d7dde7",
                pad_x=12,
                pad_y=6,
                tooltip=(
                    f"Re-summarize the selected session as {mode['title']}"
                    if self._cur_meta else
                    f"Use {mode['title']} for the next recording summary"
                ),
            ).pack(side="left", padx=(0, 8))

    def _open_summary_modes_dialog(self):
        dialog = SummaryModesDialog(self, self._summary_modes)
        if not dialog.result:
            return
        self._summary_modes = dialog.result
        mode_ids = {mode["id"] for mode in self._summary_modes}
        if self._active_summary_mode not in mode_ids:
            self._active_summary_mode = SUMMARY_MODE_GENERAL_ID
        self._save_summary_mode_config()
        self._render_summary_mode_buttons()
        active_title = self._summary_mode_by_id()["title"]
        self._status_var.set(f"Summary modes updated. Active mode: {active_title}")

    def _set_active_summary_mode(self, mode_id):
        mode = self._summary_mode_by_id(mode_id)
        self._active_summary_mode = mode["id"]
        self._save_summary_mode_config()
        self._render_summary_mode_buttons()
        return mode

    def _on_summary_mode_button(self, mode_id):
        mode = self._set_active_summary_mode(mode_id)
        if self._cur_meta and not self._recording and self._cur_meta.get("status") != "processing":
            self._resummarize_session(self._cur_meta, mode)
            return
        if self._recording:
            self._status_var.set(f"Summary mode set to {mode['title']} for this recording")
        else:
            self._status_var.set(f"Summary mode set to {mode['title']} for the next recording")

    def _resummarize_session(self, meta, mode):
        if self._resummary_active:
            self._status_var.set("A session is already being re-summarized")
            return
        target = dict(meta or {})
        slug = target.get("slug", "")
        if not slug:
            return
        self._resummary_active = True
        target["status"] = "processing"
        target["summary"] = f"Re-summarizing with {mode['title']}..."
        target["summary_mode"] = mode["id"]
        target["summary_mode_title"] = mode["title"]
        _save_meta(target)
        self._cur_meta = target
        self._detail.show(target)
        self._refresh_list()
        self._status_var.set(f"Re-summarizing {meta_title(target)} as {mode['title']}...")

        def worker():
            try:
                from muesli import Muesli
                result = Muesli().resummarize_session(
                    target,
                    prompt_text=mode["prompt"],
                    mode_id=mode["id"],
                    mode_title=mode["title"],
                )
                self.after(0, lambda: self._finish_resummary(result, mode["title"]))
            except Exception as exc:
                failed = dict(target)
                failed["status"] = "error"
                failed["error"] = f"Re-summarize failed: {str(exc)[:200]}"
                _save_meta(failed)
                self.after(0, lambda: self._finish_resummary(failed, mode["title"], success=False))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_resummary(self, meta, mode_title, success=True):
        self._resummary_active = False
        self._cur_meta = meta
        self._refresh_list()
        self._detail.show(meta)
        self._status_var.set(
            f"Re-summarized with {mode_title}"
            if success and meta.get("status") == "done" else
            (meta.get("error") or "Re-summarize failed")
        )

    def _resubmit_current(self):
        self._reprocess_session(self._cur_meta)

    def _reprocess_session(self, meta):
        if self._recording:
            self._status_var.set("Stop the current recording before reprocessing a session")
            return
        if self._reprocess_active:
            self._status_var.set("A session is already being reprocessed")
            return
        target = dict(meta or {})
        slug = target.get("slug", "")
        if not slug:
            return
        from muesli import Muesli
        audio = Muesli()._find_audio_for_slug(slug)
        if not audio:
            self._status_var.set("No saved audio found for this session")
            return

        self._reprocess_active = True
        target["status"] = "processing"
        target["summary"] = "Reprocessing from saved audio..."
        target["transcript"] = ""
        target["speakers"] = 0
        target.pop("corrections", None)
        target.pop("bugs", None)
        target.pop("error", None)
        _save_meta(target)
        self._resume_progress[slug] = "queued"
        self._cur_meta = target
        self._detail.show(target)
        self._refresh_list()
        self._status_var.set(f"Reprocessing {meta_title(target)}...")

        def worker():
            try:
                from muesli import Muesli
                mgr = Muesli()
                audio_path = mgr._find_audio_for_slug(slug)
                if not audio_path:
                    raise FileNotFoundError("No audio file found")
                result = mgr._resume_session(
                    target,
                    audio_path,
                    on_progress=lambda s, stage: self.after(0, lambda slug=s, text=stage: self._on_reprocess_progress(slug, text)),
                )
                self.after(0, lambda: self._finish_reprocess(result, slug))
            except Exception as exc:
                failed = dict(target)
                failed["status"] = "error"
                failed["error"] = f"Resubmit failed: {str(exc)[:200]}"
                _save_meta(failed)
                self.after(0, lambda: self._finish_reprocess(failed, slug, success=False))

        threading.Thread(target=worker, daemon=True).start()

    def _on_reprocess_progress(self, slug, stage):
        self._resume_progress[slug] = stage
        self._redraw_labels()
        self._status_var.set(f"Reprocessing {slug}: {stage}")

    def _finish_reprocess(self, meta, requested_slug, success=True):
        self._reprocess_active = False
        self._resume_progress.pop(requested_slug, None)
        actual_slug = meta.get("slug", "")
        if actual_slug and actual_slug != requested_slug:
            self._resume_progress.pop(actual_slug, None)
        self._cur_meta = meta
        self._refresh_list()
        self._detail.show(meta)
        self._status_var.set(
            f"Reprocessed {meta_title(meta)}"
            if success and meta.get("status") == "done" else
            (meta.get("error") or "Reprocess failed")
        )

    def _set_shortcut(self):
        hotkey = capture_hotkey(self, get_launch_hotkey())
        if not hotkey:
            return
        cfg = load_config()
        cfg["launch_hotkey"] = hotkey
        save_config(cfg)
        ensure_windows_shortcuts(hotkey=hotkey)
        self._status_var.set(f"Launch shortcut set to {hotkey}")

    def _open_settings(self):
        if not open_settings_dialog(self):
            return
        _reset_whisper_cache()
        _reset_llm_cache()
        self._refresh_llm_warning()
        self._publish_runtime_state()
        self._status_var.set("Settings saved")

    def _toggle_processing_pause(self):
        paused = not is_processing_paused()
        set_processing_paused(paused)
        self._refresh_processing_pause_button()
        if self._recording:
            self._status_var.set("Recording… (processing paused)" if paused else "Recording...")
        elif any(m.get("status") == "processing" for m in self._recordings):
            self._status_var.set("Processing paused" if paused else "Processing resumed")
        else:
            self._status_var.set("Processing paused" if paused else "Processing resumed")
        self._publish_runtime_state()

    def _refresh_processing_pause_button(self):
        paused = is_processing_paused()
        self._pause_btn.configure_button(text="Resume Processing" if paused else "Pause Processing")

    def _effective_summary_runtime(self):
        cfg = load_config()
        backend = _llm_backend()
        if backend == "anthropic":
            return "anthropic", "claude-sonnet-4-6"
        if backend == "ollama":
            return "ollama", _selected_ollama_model() or ""
        if backend == "local":
            model_path = _selected_gguf_model_path()
            return "local", os.path.basename(model_path) if model_path else ""
        api_key = cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key and importlib.util.find_spec("anthropic") is not None:
            return "anthropic", "claude-sonnet-4-6"
        ollama_model = _selected_ollama_model()
        if ollama_model:
            return "ollama", ollama_model
        model_path = _selected_gguf_model_path()
        if model_path and importlib.util.find_spec("llama_cpp") is not None:
            return "local", os.path.basename(model_path)
        return "", ""

    def _publish_runtime_state(self, status_override=None):
        backend, model = self._effective_summary_runtime()
        if status_override is not None:
            status = status_override
        elif self._recording:
            status = "Recording (processing paused)" if is_processing_paused() else "Recording"
        elif any(m.get("status") == "processing" for m in self._recordings):
            status = "Processing paused" if is_processing_paused() else "Processing"
        else:
            status = "Idle"
        update_runtime_state(
            app_pid=os.getpid(),
            recording=self._recording,
            processing=any(m.get("status") == "processing" for m in self._recordings),
            processing_paused=is_processing_paused(),
            status=status,
            summary_backend=backend,
            summary_model=model,
        )

    def _poll_runtime_state(self):
        self._refresh_processing_pause_button()
        self._publish_runtime_state()
        self.after(1000, self._poll_runtime_state)

    def _refresh_llm_warning(self):
        message = _llm_status_message()
        if message:
            self._llm_warning_var.set(message)
            if not self._llm_warning_frame.winfo_ismapped():
                self._llm_warning_frame.pack(fill="x", before=self._paned)
        else:
            if self._llm_warning_frame.winfo_ismapped():
                self._llm_warning_frame.pack_forget()

    def _bind_shortcuts(self):
        self.bind_all("<Control-r>", lambda e: (self._toggle_recording(), "break")[1])
        self.bind_all("<Control-p>", lambda e: (self._open_summary_modes_dialog(), "break")[1])
        self.bind_all("<Control-k>", lambda e: (self._set_shortcut(), "break")[1])
        self.bind_all("<Control-comma>", lambda e: (self._open_settings(), "break")[1])
        self.bind_all("<Control-o>", lambda e: (self._detail._open_transcript(), "break")[1])
        self.bind_all("<Control-Shift-C>", lambda e: (self._detail._copy_transcript(), "break")[1])
        self.bind_all("<space>", self._on_spacebar)

    def _on_spacebar(self, event):
        if isinstance(event.widget, tk.Text):
            return None
        self._toggle_recording()
        return "break"

    # ── Layout ────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Top bar
        top = tk.Frame(self, bg=DARK_BG, pady=14, padx=18)
        top.pack(fill="x")
        tk.Label(
            top,
            text="Muesli",
            font=FONT_LG,
            bg=DARK_BG,
            fg=FG,
        ).pack(side="left")
        self._rec_btn = RoundedButton(
            top,
            text="Start Recording",
            command=self._toggle_recording,
            font=("Segoe UI Semibold", 15),
            bg=RED,
            fg="white",
            active_bg="#b42318",
            shadow="#e8b3b3",
            pad_x=24,
            pad_y=13,
            min_width=240,
            tooltip=lambda: f"Start or stop recording\nCtrl+R\nLaunch and record: {get_launch_hotkey()}",
        )
        self._rec_btn.pack(side="right")
        if not self._recording_available:
            self._rec_btn.set_enabled(False)
        RoundedButton(
            top,
            text="Pause Processing",
            command=self._toggle_processing_pause,
            font=FONT_SM,
            bg=ITEM_BG,
            fg=FG,
            active_bg=ITEM_ALT,
            shadow="#d7dde7",
            tooltip="Keep recording audio but defer Whisper and LLM work",
        ).pack(side="right", padx=(0, 10))
        self._pause_btn = top.winfo_children()[0]
        RoundedButton(
            top,
            text="Settings",
            command=self._open_settings,
            font=FONT_SM,
            bg=ITEM_BG,
            fg=FG,
            active_bg=ITEM_ALT,
            shadow="#d7dde7",
            tooltip="Model and runtime settings\nCtrl+,",
        ).pack(side="right", padx=(0, 10))
        RoundedButton(
            top,
            text="Manage Modes",
            command=self._open_summary_modes_dialog,
            font=FONT_SM,
            bg=ITEM_BG,
            fg=FG,
            active_bg=ITEM_ALT,
            shadow="#d7dde7",
            tooltip="Add, remove, or edit summary modes\nCtrl+P",
        ).pack(side="right", padx=(0, 10))
        RoundedButton(
            top,
            text="Set Shortcut",
            command=self._set_shortcut,
            font=FONT_SM,
            bg=ITEM_BG,
            fg=FG,
            active_bg=ITEM_ALT,
            shadow="#d7dde7",
            tooltip="Choose the global launch shortcut\nCtrl+K",
        ).pack(side="right", padx=(0, 10))
        self._timer_lbl = tk.Label(top, text="",
                                   font=("Consolas", 12, "bold"),
                                   bg=DARK_BG, fg=RED)
        self._timer_lbl.pack(side="right", padx=12)

        mode_bar = tk.Frame(self, bg=DARK_BG, padx=18)
        mode_bar.pack(fill="x", pady=(0, 10))
        tk.Label(
            mode_bar,
            text="Summary Mode",
            font=("Segoe UI Semibold", 9),
            bg=DARK_BG,
            fg=FG_DIM,
        ).pack(side="left", padx=(0, 12))
        self._summary_modes_frame = tk.Frame(mode_bar, bg=DARK_BG)
        self._summary_modes_frame.pack(side="left", fill="x", expand=True)

        # Status bar
        initial_status = "Ready" if self._recording_available else "Ready (recording backend unavailable)"
        self._status_var = tk.StringVar(value=initial_status)
        tk.Label(self, textvariable=self._status_var, font=FONT_SM,
                 bg=DARK_BG, fg=FG_DIM, anchor="w", padx=18).pack(fill="x", pady=(0, 8))
        tk.Frame(self, bg=LINE, height=1).pack(fill="x")

        self._llm_warning_var = tk.StringVar(value="")
        self._llm_warning_frame = tk.Frame(self, bg="#facc15")
        tk.Label(
            self._llm_warning_frame,
            textvariable=self._llm_warning_var,
            bg="#facc15",
            fg="#4a3410",
            anchor="w",
            justify="left",
            font=("Segoe UI Semibold", 9),
            padx=18,
            pady=10,
        ).pack(fill="x")

        # Main split — draggable PanedWindow
        paned = tk.PanedWindow(self, orient="horizontal",
                               bg=LINE, sashwidth=4, sashpad=0,
                               bd=0, relief="flat")
        paned.pack(fill="both", expand=True)
        self._paned = paned
        self._refresh_llm_warning()

        # Left list
        left = tk.Frame(paned, bg=PANEL_BG, width=290)
        left.pack_propagate(False)
        tk.Label(left, text="Sessions", font=("Segoe UI Semibold", 9),
                 bg=PANEL_BG, fg=FG_DIM, anchor="w",
                 padx=16, pady=14).pack(fill="x")

        lf = tk.Frame(left, bg=PANEL_BG)
        lf.pack(fill="both", expand=True)
        sb = tk.Scrollbar(lf, bg=PANEL_BG, troughcolor=ITEM_ALT, relief="flat")
        sb.pack(side="right", fill="y")
        self._listbox = tk.Listbox(
            lf, bg=PANEL_BG, fg=FG,
            selectbackground=ITEM_SEL, selectforeground="white",
            font=("Segoe UI", 9), relief="flat", bd=0, activestyle="none",
            highlightthickness=0,
            yscrollcommand=sb.set, cursor="hand2"
        )
        self._listbox.pack(fill="both", expand=True)
        sb.config(command=self._listbox.yview)
        self._listbox.bind("<<ListboxSelect>>", self._on_select)
        self._listbox.bind("<Double-Button-1>", self._rename_selected_from_list)

        paned.add(left, minsize=150, width=270)

        # Right detail
        self._detail = DetailPanel(paned, self._player,
                                   on_delete=self._delete_current,
                                    on_rename=self._rename_current,
                                   on_edit_summary=self._edit_current_summary,
                                   on_export_obsidian=self._export_current_to_obsidian,
                                   on_resubmit=self._resubmit_current,
                                   on_stop_recording=self._stop_recording,
                                   notepad_icon=self._notepad_icon,
                                   copy_icon=self._copy_icon,
                                   bg=DARK_BG)
        paned.add(self._detail, minsize=400)

        # Redraw list labels when sash is dragged (adapt to new width)
        self._listbox.bind("<Configure>", self._on_list_resize)
        self._list_last_width = 0
        self._render_summary_mode_buttons()

    # ── List ──────────────────────────────────────────────────────────────────
    def _max_label_chars(self):
        """Estimate how many characters fit in the listbox at current width."""
        w = self._listbox.winfo_width()
        if w < 10:
            return 40  # fallback before first layout
        # Approximate: 7 pixels per char at font size 9
        return max(15, w // 7)

    def _on_list_resize(self, _e):
        w = self._listbox.winfo_width()
        if w != self._list_last_width:
            self._list_last_width = w
            self._redraw_labels()

    def _redraw_labels(self):
        """Re-render listbox labels to fit current width."""
        sel = self._listbox.curselection()
        sel_idx = sel[0] if sel else None
        self._listbox.delete(0, "end")
        maxc = self._max_label_chars()
        for meta in self._recordings:
            self._listbox.insert("end", self._format_label(meta, maxc))
        if sel_idx is not None and sel_idx < self._listbox.size():
            self._listbox.selection_set(sel_idx)

    def _format_label(self, meta, maxc=40):
        status = meta.get("status", "")
        slug   = meta.get("slug", "")
        icon   = {"done": "✓", "processing": "⟳", "error": "✗"}.get(status, " ")
        title  = meta_title(meta)
        # Show resume progress for processing sessions
        progress = self._resume_progress.get(slug, "")
        if status == "processing" and progress:
            label = f"{icon}  {title}  ·  {progress}"
        else:
            try:
                dt  = datetime.datetime.fromisoformat(meta["started_at"])
                age = format_age(dt)
            except Exception:
                age = ""
            label = f"{icon}  {title}"
            if age:
                label += f"  ·  {age}"
        if len(label) > maxc:
            label = label[:maxc - 1] + "…"
        return label

    def _refresh_list(self):
        selected_slug = self._cur_meta.get("slug") if self._cur_meta else None
        self._recordings = list_recordings()
        self._redraw_labels()
        if not self._recordings:
            self._cur_meta = None
            self._detail.clear()
            self._render_summary_mode_buttons()
            return
        selected_idx = 0
        if selected_slug:
            for idx, meta in enumerate(self._recordings):
                if meta.get("slug") == selected_slug:
                    selected_idx = idx
                    break
        self._listbox.selection_clear(0, "end")
        self._listbox.selection_set(selected_idx)
        self._cur_meta = self._recordings[selected_idx]
        self._detail.show(self._cur_meta)
        self._render_summary_mode_buttons()

    def _on_select(self, _e):
        sel = self._listbox.curselection()
        if not sel: return
        meta = self._recordings[sel[0]]
        self._cur_meta = meta
        self._detail.show(meta)
        self._render_summary_mode_buttons()

    def _load_detail_icon(self, path):
        if os.path.exists(path):
            try:
                return tk.PhotoImage(master=self, file=path)
            except Exception:
                return None
        return None

    def _rename_meta(self, meta):
        if not meta:
            return
        current = meta_title(meta)
        title = simpledialog.askstring("Rename Session", "Session name", initialvalue=current, parent=self)
        if title is None:
            return
        title = title.strip()
        if not title:
            return
        meta["title"] = title
        meta["title_manual"] = True
        _save_meta(meta)
        _sync_session_text_file(meta)
        self._cur_meta = meta
        self._refresh_list()
        self._status_var.set("Session renamed")

    def _rename_current(self):
        if self._recording and self._detail._live_mode:
            current = self._live_manual_title if self._live_manual_title_active else self._detail.title_text()
            title = simpledialog.askstring("Rename Session", "Session name", initialvalue=current, parent=self)
            if title is None:
                return
            title = title.strip()
            if not title:
                return
            self._live_manual_title = title
            self._live_manual_title_active = True
            self._detail.apply_manual_title(title)
            self._status_var.set("Session renamed")
            return
        self._rename_meta(self._cur_meta)

    def _edit_current_summary(self):
        if self._recording and self._detail._live_mode:
            initial = self._live_manual_summary if self._live_manual_summary_active else self._detail.summary_text()
            summary = edit_multiline_text(self, "Edit Summary", "Summary", initial_text=initial, allow_empty=True)
            if summary is None:
                return
            self._live_manual_summary = summary
            self._live_manual_summary_active = True
            self._detail.apply_manual_summary(summary)
            self._status_var.set("Summary updated")
            return
        if not self._cur_meta:
            return
        summary = edit_multiline_text(
            self,
            "Edit Summary",
            "Summary",
            initial_text=str(self._cur_meta.get("summary", "") or ""),
            allow_empty=True,
        )
        if summary is None:
            return
        self._cur_meta["summary"] = summary
        self._cur_meta["summary_manual"] = True
        _save_meta(self._cur_meta)
        _sync_session_text_file(self._cur_meta)
        self._refresh_list()
        self._status_var.set("Summary updated")

    def _rename_selected_from_list(self, _event=None):
        sel = self._listbox.curselection()
        if not sel:
            return
        self._rename_meta(self._recordings[sel[0]])

    # ── Recording ─────────────────────────────────────────────────────────────
    def _toggle_recording(self):
        if not self._recording: self._start_recording()
        else:                   self._stop_recording()

    def _start_recording(self):
        if not self._recording_available:
            messagebox.showerror("Recording unavailable", "PyAudio is not installed, so microphone recording is disabled on this PC.")
            return
        self._player.stop()
        self._recording = True
        self._live_transcript = ""
        self._live_chunks_transcribed = 0
        self._live_preview = None
        self._live_manual_title = ""
        self._live_manual_title_active = False
        self._live_manual_summary = ""
        self._live_manual_summary_active = False
        self._live_generation += 1
        live_generation = self._live_generation
        self._provisional_summary_started = False
        self._provisional_generation += 1
        self._provisional_transcript_seed = ""
        self._chunk_pipeline = None
        self._realtime_transcriber = None
        self._realtime_turn_order = []
        self._realtime_turns = {}
        recorder_on_chunk = None
        recorder_on_frame = None
        if self._use_openai_realtime():
            try:
                self._start_realtime_transcriber(live_generation)
            except Exception as exc:
                self._status_var.set(f"OpenAI Realtime unavailable; falling back to local chunks. {exc}")
                self._chunk_pipeline = ChunkPipeline(
                    on_transcribed=lambda n, text: self.after(0,
                        lambda t=text, nn=n, gen=live_generation: self._on_chunk_transcribed(gen, nn, t)),
                    on_summarised=lambda n: self.after(0,
                        lambda nn=n, gen=live_generation: self._on_chunk_summarised(gen, nn))
                )
                recorder_on_chunk = self._chunk_pipeline.submit
            else:
                recorder_on_frame = self._realtime_transcriber.append_audio
        else:
            self._chunk_pipeline = ChunkPipeline(
                on_transcribed=lambda n, text: self.after(0,
                    lambda t=text, nn=n, gen=live_generation: self._on_chunk_transcribed(gen, nn, t)),
                on_summarised=lambda n: self.after(0,
                    lambda nn=n, gen=live_generation: self._on_chunk_summarised(gen, nn))
            )
            recorder_on_chunk = self._chunk_pipeline.submit
        self._live_recording_slug = self._recorder.start(on_chunk=recorder_on_chunk, on_frame=recorder_on_frame)
        self._rec_btn.configure_button(
            text="Stop Recording",
            bg="#111827",
            fg="white",
            active_bg="#1f2937",
            shadow="#c7d0db",
        )
        self.wm_iconphoto(True, self._icon_rec)
        self._status_var.set("Recording...")
        # Show the detail panel immediately for live transcript display
        self._detail.show_live()
        self._detail.set_live_transcript("", processing=True, status_note=self._live_transcript_status())
        self._detail.set_live_overview(
            title="Recording in progress",
            summary=f"Provisional title and summary will appear after the first {LIVE_CHUNK_SECONDS}-second chunk.",
            status_text="Recording...",
        )
        self._render_summary_mode_buttons()
        self._tick_timer()

    def _on_chunk_summarised(self, generation, n):
        if generation != self._live_generation:
            return
        self._status_var.set(f"Recording… ({n} chunk{'s' if n != 1 else ''} processed)")

    def _on_chunk_transcribed(self, generation, n, text):
        """Called on main thread when a chunk has been transcribed — update live display."""
        if generation != self._live_generation:
            return
        self._live_chunks_transcribed = n
        if text:
            if self._live_transcript:
                self._live_transcript += " " + text
            else:
                self._live_transcript = text
            if self._provisional_transcript_seed:
                self._provisional_transcript_seed += " " + text
            else:
                self._provisional_transcript_seed = text
        self._detail.set_live_transcript(
            self._live_transcript, processing=True, status_note=self._live_transcript_status())
        if text and not self._provisional_summary_started:
            self._start_provisional_summary(self._provisional_transcript_seed)

    def _use_openai_realtime(self):
        return _transcription_backend() == "openai_realtime"

    def _compose_realtime_transcript(self):
        parts = []
        for item_id in self._realtime_turn_order:
            text = str(self._realtime_turns.get(item_id, "")).strip()
            if text:
                parts.append(text)
        return " ".join(parts).strip()

    def _start_realtime_transcriber(self, generation):
        api_key = _openai_transcription_api_key()
        if not api_key:
            raise RuntimeError("OpenAI Realtime is selected but no OPENAI_API_KEY or saved OpenAI key is configured")
        model_name = _selected_openai_transcription_model()
        self._realtime_turn_order = []
        self._realtime_turns = {}
        self._realtime_transcriber = OpenAIRealtimeTranscriber(
            model_name,
            api_key,
            on_delta=lambda item_id, delta: self.after(
                0, lambda iid=item_id, text=delta, gen=generation: self._on_realtime_delta(gen, iid, text)
            ),
            on_completed=lambda item_id, transcript: self.after(
                0, lambda iid=item_id, text=transcript, gen=generation: self._on_realtime_completed(gen, iid, text)
            ),
            on_error=lambda text: self.after(
                0, lambda message=text, gen=generation: self._on_realtime_error(gen, message)
            ),
            on_status=lambda text: self.after(
                0, lambda message=text, gen=generation: self._on_realtime_status(gen, message)
            ),
        )
        self._realtime_transcriber.start()

    def _on_realtime_status(self, generation, message):
        if generation != self._live_generation:
            return
        if message:
            self._status_var.set(message)

    def _on_realtime_delta(self, generation, item_id, text):
        if generation != self._live_generation:
            return
        if item_id not in self._realtime_turn_order:
            self._realtime_turn_order.append(item_id)
        self._realtime_turns[item_id] = str(self._realtime_turns.get(item_id, "")) + str(text or "")
        self._live_transcript = self._compose_realtime_transcript()
        if self._live_transcript:
            self._provisional_transcript_seed = self._live_transcript
        self._detail.set_live_transcript(
            self._live_transcript,
            processing=True,
            status_note=self._live_transcript_status(),
        )
        if text and not self._provisional_summary_started and self._provisional_transcript_seed:
            self._start_provisional_summary(self._provisional_transcript_seed)

    def _on_realtime_completed(self, generation, item_id, transcript):
        if generation != self._live_generation:
            return
        if item_id not in self._realtime_turn_order:
            self._realtime_turn_order.append(item_id)
        self._realtime_turns[item_id] = str(transcript or "").strip()
        self._live_transcript = self._compose_realtime_transcript()
        if self._live_transcript:
            self._provisional_transcript_seed = self._live_transcript
        self._detail.set_live_transcript(
            self._live_transcript,
            processing=True,
            status_note=self._live_transcript_status(),
        )

    def _on_realtime_error(self, generation, message):
        if generation != self._live_generation:
            return
        self._status_var.set(f"OpenAI Realtime error: {message}")

    def _live_transcript_status(self, finalising=False):
        if self._use_openai_realtime():
            if is_processing_paused():
                return "Processing paused. Transcript updates will resume after Resume Processing."
            if finalising:
                return "Finishing OpenAI Realtime transcript and preparing the final summary..."
            return "Streaming live transcript with OpenAI Realtime..."
        pipeline = getattr(self, "_chunk_pipeline", None)
        snapshot = pipeline.progress_snapshot() if pipeline else {}
        processed = snapshot.get("processed", self._live_chunks_transcribed)
        remaining_work = snapshot.get("remaining", 0)
        avg_seconds = snapshot.get("avg_seconds", 0.0)
        if is_processing_paused():
            return "Processing paused. Transcript updates will resume after Resume Processing."
        if finalising:
            if remaining_work > 0:
                eta = remaining_work * (avg_seconds or LIVE_CHUNK_SECONDS)
                return (
                    f"Finishing {remaining_work} queued chunk"
                    f"{'s' if remaining_work != 1 else ''} before the final summary. "
                    f"Rough ETA {format_eta(eta)}."
                )
            return "Queued chunks finished. Running the final summary and rename..."
        if not self._recording or not self._recorder:
            return "Preparing live transcript..."
        elapsed = self._recorder.elapsed()
        if processed <= 0:
            remaining = max(0, LIVE_CHUNK_SECONDS - elapsed)
            if remaining > 0:
                return f"First live chunk in about {int(math.ceil(remaining))}s."
            return "Transcribing the first live chunk..."
        remaining = LIVE_CHUNK_SECONDS - (elapsed % LIVE_CHUNK_SECONDS)
        if remaining >= LIVE_CHUNK_SECONDS - 0.2:
            remaining = 0
        status = (
            f"{processed} chunk"
            f"{'s' if processed != 1 else ''} ready. "
            f"Next chunk in about {int(math.ceil(max(0, remaining)))}s."
        )
        if remaining_work > 0:
            eta = remaining_work * (avg_seconds or LIVE_CHUNK_SECONDS)
            status += (
                f" {remaining_work} queued for transcription; rough catch-up ETA "
                f"{format_eta(eta)}."
            )
        return status

    def _fallback_live_summary(self, transcript_snapshot):
        text = _clean_sentence(transcript_snapshot)
        if not text:
            return ""
        if len(text) <= 220:
            return text
        clipped = text[:220].rsplit(" ", 1)[0].strip()
        return clipped + "..."

    def _start_provisional_summary(self, transcript_snapshot):
        self._provisional_summary_started = True
        generation = self._provisional_generation
        self._detail.set_live_overview(
            summary=(
                f"Generating provisional title and summary from the first spoken "
                f"{LIVE_CHUNK_SECONDS}-second chunk..."
            ),
            status_text="Recording...",
        )

        def _worker():
            ai = _generate_ai_fields(transcript_snapshot, ollama_timeout=60)
            self.after(0, lambda: self._apply_provisional_summary(generation, ai, transcript_snapshot))

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_provisional_summary(self, generation, ai, transcript_snapshot):
        if generation != self._provisional_generation:
            return
        if not ai:
            return
        title = str(ai.get("title", "")).strip()
        summary = str(ai.get("summary", "")).strip() or self._fallback_live_summary(transcript_snapshot)
        speakers = int(ai.get("speakers", 0) or 0)
        if not title and not summary:
            return
        self._live_preview = {
            "title": title or "Recording in progress",
            "summary": summary,
            "speakers": speakers,
        }
        if self._recording and self._detail._live_mode:
            self._detail.set_live_overview(
                title=self._live_preview["title"],
                summary=self._live_preview["summary"],
                status_text="Recording...",
                speakers=self._live_preview["speakers"],
            )

    def _tick_timer(self):
        if not self._recording: return
        self._timer_lbl.config(text=format_duration(self._recorder.elapsed()))
        self._detail.set_live_transcript(
            self._live_transcript,
            processing=True,
            status_note=self._live_transcript_status(),
        )
        self._timer_id = self.after(500, self._tick_timer)

    def _stop_recording(self):
        self._recording = False
        if self._timer_id: self.after_cancel(self._timer_id)
        self._timer_id = None
        self._timer_lbl.config(text="")
        self._rec_btn.configure_button(
            text="Start Recording",
            bg=RED,
            fg="white",
            active_bg="#b42318",
            shadow="#e8b3b3",
        )
        self.wm_iconphoto(True, self._icon_idle)
        self._status_var.set("Finishing processing...")
        self.update_idletasks()

        meta = self._recorder.stop()
        if self._live_manual_title_active and self._live_manual_title.strip():
            meta["title"] = self._live_manual_title.strip()
            meta["title_manual"] = True
        if self._live_manual_summary_active:
            meta["summary"] = self._live_manual_summary
            meta["summary_manual"] = True
        _save_meta(meta)
        self._recordings.insert(0, meta)
        self._listbox.insert(0, self._format_label(meta, self._max_label_chars()))
        self._listbox.selection_clear(0, "end")
        self._listbox.selection_set(0)
        self._cur_meta = meta
        self._detail.show(meta)
        self._render_summary_mode_buttons()
        # Restore the live transcript that show(meta) just cleared
        if self._live_transcript:
            self._detail.set_live_transcript(
                self._live_transcript, processing=True, status_note=self._live_transcript_status(finalising=True))
        self._detail.set_live_overview(
            title=meta_title(meta),
            summary="Final summary and rename will appear when processing completes.",
            status_text="Processing...",
            speakers=meta.get("speakers", 0),
        )

        pipeline = self._chunk_pipeline
        realtime_transcriber = self._realtime_transcriber

        def _finalize():
            transcript = ""
            summaries = []
            if realtime_transcriber is not None:
                realtime_transcriber.finish()
                transcript = self._compose_realtime_transcript().strip()
            elif pipeline is not None:
                # Wait for all chunk transcription + summarisation to finish
                pipeline.finish()
                transcript = pipeline.get_transcript().strip()
                summaries = pipeline.get_summaries()
            # Show final transcript (no "[still processing...]")
            self.after(0, lambda: self._detail.set_live_transcript(
                transcript, processing=False, status_note="Final summary and rename are still running..."))
            # Run conversion + final LLM merge (skip whisper — already done)
            process_recording(meta, self._on_proc_update,
                              transcript=(transcript or None), summaries=summaries)

        threading.Thread(target=_finalize, daemon=True).start()

    def _on_proc_update(self, meta, stage):
        self.after(0, lambda: self._apply_update(meta, stage))

    def _apply_update(self, meta, stage):
        self._status_var.set("" if stage == "done" else stage)
        self._refresh_list()
        if (self._cur_meta and self._cur_meta.get("slug") in (meta.get("slug"), meta.get("_old_slug"))
                and stage == "summarising…"):
            self._detail.set_live_transcript(
                meta.get("transcript", "") or self._live_transcript,
                processing=False,
                status_note="Summarising and renaming with the selected LLM. This can take a while.",
            )
        if stage == "done" or (self._cur_meta and
                self._cur_meta.get("slug") in (meta.get("slug"), meta.get("_old_slug"))):
            self._cur_meta = meta
            self._detail.show(meta)
            if stage == "done":
                self._refresh_llm_warning()
                for i, m in enumerate(self._recordings):
                    if m.get("slug") == meta.get("slug"):
                        self._listbox.selection_clear(0, "end")
                        self._listbox.selection_set(i)
                        break

    def _delete_current(self):
        """Delete the currently selected recording and refresh the UI."""
        if not self._cur_meta:
            return
        title = meta_title(self._cur_meta)
        if not messagebox.askyesno("Delete Session", f"Delete “{title}”?", parent=self):
            return
        current_slug = self._cur_meta.get("slug")
        current_index = 0
        for idx, meta in enumerate(self._recordings):
            if meta.get("slug") == current_slug:
                current_index = idx
                break
        self._player.clear()
        delete_recording(self._cur_meta)
        self._cur_meta = None
        self._recordings = list_recordings()
        if not self._recordings:
            self._detail.clear()
            self._listbox.delete(0, "end")
            self._status_var.set("Session deleted")
            return
        next_index = min(current_index, len(self._recordings) - 1)
        self._redraw_labels()
        self._listbox.selection_clear(0, "end")
        self._listbox.selection_set(next_index)
        self._cur_meta = self._recordings[next_index]
        self._detail.show(self._cur_meta)
        self._status_var.set("Session deleted")

    def _export_current_to_obsidian(self):
        if not self._cur_meta:
            return
        cfg = load_config()
        if not cfg.get("obsidian_vault_dir", "").strip():
            chosen = filedialog.askdirectory(
                parent=self,
                title="Choose Obsidian vault folder",
                initialdir=os.path.expanduser("~"),
            )
            if not chosen:
                return
            cfg["obsidian_vault_dir"] = chosen
            cfg["obsidian_export_folder"] = cfg.get("obsidian_export_folder", "Muesli") or "Muesli"
            save_config(cfg)
        try:
            note_path = _export_session_to_obsidian(self._cur_meta, cfg)
        except Exception as exc:
            messagebox.showerror("Obsidian Export", str(exc), parent=self)
            return
        self._status_var.set(f"Exported to Obsidian: {os.path.basename(note_path)}")


# ── Detail Panel ──────────────────────────────────────────────────────────────
class DetailPanel(tk.Frame):
    def __init__(self, parent, player, on_delete=None, on_rename=None, on_edit_summary=None, on_stop_recording=None, on_export_obsidian=None, on_resubmit=None, notepad_icon=None, copy_icon=None, **kw):
        super().__init__(parent, **kw)
        self._current   = None
        self._player    = player
        self._on_delete = on_delete
        self._on_rename = on_rename
        self._on_edit_summary = on_edit_summary
        self._on_stop_recording = on_stop_recording
        self._on_export_obsidian = on_export_obsidian
        self._on_resubmit = on_resubmit
        self._notepad_icon = notepad_icon
        self._copy_icon = copy_icon
        self._tick_id   = None
        self._duration  = 0.0
        self._live_mode = False
        self._title_manual = False
        self._summary_manual = False
        self._process_nodes = {
            "audio": "pending",
            "chunks": "pending",
            "transcript": "pending",
            "summary": "pending",
            "ready": "pending",
        }
        self._build()

    def _build(self):
        self._placeholder = tk.Label(
            self,
            text="Select a session to review the transcript and summary",
            font=("Segoe UI", 10),
            bg=PANEL_BG,
            fg=FG_DIM,
        )
        self._placeholder.pack(expand=True)

        c = tk.Frame(self, bg=PANEL_BG, padx=24, pady=20)
        self._content = c

        # ── Title ─────────────────────────────────────────────────────────
        tk.Label(c, text="Session", font=("Segoe UI Semibold", 9),
                 bg=PANEL_BG, fg=FG_DIM).pack(anchor="w")
        self._title_var = tk.StringVar()
        self._title_lbl = tk.Label(
            c,
            textvariable=self._title_var,
            font=("Segoe UI Semibold", 18),
            bg=PANEL_BG,
            fg=FG,
            wraplength=560,
            justify="left",
            cursor="hand2",
        )
        self._title_lbl.pack(anchor="w", pady=(2, 10))
        self._title_lbl.bind("<Button-1>", self._on_rename_click)
        self._edit_hint_var = tk.StringVar(value="Click the title to rename. Use Edit to change the summary. Manual edits are preserved.")
        self._edit_hint_lbl = tk.Label(
            c,
            textvariable=self._edit_hint_var,
            font=FONT_SM,
            bg=PANEL_BG,
            fg=FG_DIM,
            wraplength=560,
            justify="left",
        )
        self._edit_hint_lbl.pack(anchor="w", pady=(0, 10))

        # ── Meta row ──────────────────────────────────────────────────────
        mr = tk.Frame(c, bg=PANEL_BG)
        mr.pack(anchor="w", pady=(0, 14))
        self._date_lbl     = tk.Label(mr, font=FONT_SM, bg=PANEL_BG, fg=FG_DIM)
        self._age_lbl      = tk.Label(mr, font=FONT_SM, bg=PANEL_BG, fg=FG_DIM)
        self._dur_lbl      = tk.Label(mr, font=FONT_SM, bg=PANEL_BG, fg=FG_DIM)
        self._speakers_lbl = tk.Label(mr, font=FONT_SM, bg=PANEL_BG, fg=FG_DIM)
        self._status_lbl   = tk.Label(mr, font=FONT_SM, bg=PANEL_BG, fg=YELLOW)
        dot = lambda: tk.Label(mr, text=" · ", bg=PANEL_BG, fg=FG_DIM, font=FONT_SM)
        self._date_lbl.pack(side="left")
        dot().pack(side="left"); self._age_lbl.pack(side="left")
        dot().pack(side="left"); self._dur_lbl.pack(side="left")
        dot().pack(side="left"); self._speakers_lbl.pack(side="left")
        dot().pack(side="left"); self._status_lbl.pack(side="left")

        # ── Transport controls ────────────────────────────────────────────
        transport = tk.Frame(c, bg=PANEL_BG)
        transport.pack(anchor="w", pady=(0, 14))

        self._play_btn = RoundedButton(
            transport,
            text="Play",
            command=self._on_play,
            font=FONT_SM,
            bg=ITEM_BG,
            fg=FG,
            active_bg=ITEM_ALT,
            shadow="#d7dde7",
            pad_x=12,
            pad_y=6,
        )
        self._stop_btn = RoundedButton(
            transport,
            text="Stop",
            command=self._on_stop,
            font=FONT_SM,
            bg=ITEM_BG,
            fg=FG,
            active_bg=ITEM_ALT,
            shadow="#d7dde7",
            pad_x=12,
            pad_y=6,
        )
        self._play_btn.pack(side="left", padx=(0, 4))
        self._stop_btn.pack(side="left", padx=(0, 12))

        self._pos_var = tk.StringVar(value="0:00")
        tk.Label(transport, textvariable=self._pos_var,
                 font=("Consolas", 10), bg=PANEL_BG, fg=FG_DIM).pack(side="left")
        tk.Label(transport, text=" / ", font=FONT_SM,
                 bg=PANEL_BG, fg=FG_DIM).pack(side="left")
        self._dur_play_var = tk.StringVar(value="0:00")
        tk.Label(transport, textvariable=self._dur_play_var,
                 font=("Consolas", 10), bg=PANEL_BG, fg=FG_DIM).pack(side="left")

        # Progress bar (Canvas)
        self._bar_canvas = tk.Canvas(transport, height=4, width=200,
                                     bg=LINE, bd=0, highlightthickness=0)
        self._bar_canvas.pack(side="left", padx=(14, 0), pady=6)
        self._bar_fill = self._bar_canvas.create_rectangle(
            0, 0, 0, 4, fill=GREEN, outline="")

        # ── File links ────────────────────────────────────────────────────
        fr = tk.Frame(c, bg=PANEL_BG)
        fr.pack(anchor="w", pady=(0, 14))
        self._audio_btn = RoundedButton(
            fr,
            text="Open Audio",
            command=self._open_audio,
            font=FONT_SM,
            bg=ITEM_BG,
            fg=FG,
            active_bg=ITEM_ALT,
            shadow="#d7dde7",
            pad_x=12,
            pad_y=6,
        )
        self._audio_btn.pack(side="left")
        self._resubmit_btn = RoundedButton(
            fr,
            text="Reprocess",
            command=self._on_resubmit_click,
            font=FONT_SM,
            bg=ITEM_BG,
            fg=FG,
            active_bg=ITEM_ALT,
            shadow="#d7dde7",
            pad_x=12,
            pad_y=6,
            tooltip="Re-run transcription and summary from saved audio",
        )
        self._resubmit_btn.pack(side="left", padx=(10, 0))
        self._obsidian_btn = RoundedButton(
            fr,
            text="Export Obsidian",
            command=self._on_export_obsidian_click,
            font=FONT_SM,
            bg=ITEM_BG,
            fg=FG,
            active_bg=ITEM_ALT,
            shadow="#d7dde7",
            pad_x=12,
            pad_y=6,
            tooltip="Write this session into an Obsidian vault note",
        )
        self._obsidian_btn.pack(side="left", padx=(10, 0))
        self._del_btn = RoundedButton(
            fr,
            text="Delete",
            command=self._on_delete_click,
            font=FONT_SM,
            bg="#fee2e2",
            fg="#991b1b",
            active_bg="#fecaca",
            shadow="#f5b8b8",
            pad_x=12,
            pad_y=6,
        )
        self._del_btn.pack(side="left", padx=(10, 0))

        # ── Summary ───────────────────────────────────────────────────────
        tk.Label(c, text="Process", font=("Segoe UI Semibold", 9),
                 bg=PANEL_BG, fg=FG_DIM).pack(anchor="w", pady=(2, 4))
        self._process_canvas = tk.Canvas(c, height=76, bg=PANEL_BG, highlightthickness=0, bd=0, relief="flat")
        self._process_canvas.pack(fill="x", pady=(0, 12))
        self._process_canvas.bind("<Configure>", lambda _e: self._draw_process_diagram())

        summary_hdr = tk.Frame(c, bg=PANEL_BG)
        summary_hdr.pack(fill="x", pady=(4, 2))
        tk.Label(summary_hdr, text="Summary", font=("Segoe UI Semibold", 9),
                 bg=PANEL_BG, fg=FG_DIM).pack(side="left")
        self._edit_summary_btn = RoundedButton(
            summary_hdr,
            text="Edit",
            command=self._on_edit_summary_click,
            font=FONT_SM,
            bg=ITEM_BG,
            fg=FG,
            active_bg=ITEM_ALT,
            shadow="#d7dde7",
            pad_x=10,
            pad_y=5,
            tooltip="Edit summary",
        )
        self._edit_summary_btn.pack(side="left", padx=(8, 0))
        self._edit_summary_btn.set_enabled(False)
        self._summary_txt = tk.Text(c, height=4, wrap="word",
                                    bg=ITEM_BG, fg=FG, relief="flat", bd=0,
                                    font=("Segoe UI", 10), padx=10, pady=8, state="disabled")
        self._summary_txt.pack(fill="x", pady=(0, 12))

        # ── Transcript ────────────────────────────────────────────────────
        tr_hdr = tk.Frame(c, bg=PANEL_BG)
        tr_hdr.pack(fill="x", pady=(0, 2))
        tk.Label(tr_hdr, text="Transcript", font=("Segoe UI Semibold", 9),
                 bg=PANEL_BG, fg=FG_DIM).pack(side="left")
        self._open_transcript_btn = RoundedButton(
            tr_hdr,
            text="",
            command=self._open_transcript,
            icon_image=self._notepad_icon,
            bg=ITEM_BG,
            fg=FG,
            active_bg=ITEM_ALT,
            shadow="#d7dde7",
            pad_x=9,
            pad_y=5,
            tooltip="Open transcript file\nCtrl+O",
        )
        self._open_transcript_btn.pack(side="left", padx=(8, 0))
        self._copy_btn = RoundedButton(
            tr_hdr,
            text="Copy",
            command=self._copy_transcript,
            font=FONT_SM,
            icon_image=self._copy_icon,
            bg=ITEM_BG,
            fg=FG,
            active_bg=ITEM_ALT,
            shadow="#d7dde7",
            pad_x=12,
            pad_y=6,
            tooltip="Copy transcript\nCtrl+Shift+C",
        )
        self._copy_btn.pack(side="left", padx=(8, 0))
        self._open_transcript_btn.set_enabled(False)
        self._copy_btn.set_enabled(False)
        self._transcript_txt = tk.Text(c, height=10, wrap="word",
                                       bg=ITEM_BG, fg=FG, relief="flat", bd=0,
                                       font=FONT_MON, padx=10, pady=8, state="disabled")
        self._transcript_txt.pack(fill="both", expand=True)

    # ── Show ──────────────────────────────────────────────────────────────────
    def show_live(self):
        """Prepare the detail panel for live recording display."""
        self._live_mode = True
        self._current = None
        self._title_manual = False
        self._summary_manual = False
        self._edit_hint_var.set("Click the title to rename. Use Edit to change the summary while recording. Manual edits are preserved.")
        self._placeholder.pack_forget()
        self._content.pack(fill="both", expand=True)
        self._title_var.set("Recording in progress")
        self._date_lbl.config(text="")
        self._age_lbl.config(text="")
        self._dur_lbl.config(text="")
        self._speakers_lbl.config(text="")
        self._status_lbl.config(text="Recording...", fg=YELLOW)
        self._play_btn.set_enabled(False)
        self._stop_btn.set_enabled(True)
        self._audio_btn.set_enabled(False)
        self._resubmit_btn.set_enabled(False)
        self._del_btn.set_enabled(False)
        self._edit_summary_btn.set_enabled(True)
        self._open_transcript_btn.set_enabled(False)
        self._copy_btn.set_enabled(False)
        self._obsidian_btn.set_enabled(False)
        self._set_text(self._summary_txt, "")
        self._set_process_nodes(audio="active", chunks="pending", transcript="pending", summary="pending", ready="pending")
        self.set_live_transcript("", processing=True)

    def set_live_overview(self, title=None, summary=None, status_text=None, speakers=None):
        if title is not None and not self._title_manual:
            self._title_var.set(title)
        if summary is not None and not self._summary_manual:
            self._set_text(self._summary_txt, summary)
            if summary.strip():
                self._set_process_nodes(summary="active")
        if status_text is not None:
            self._status_lbl.config(text=status_text, fg=YELLOW)
        if speakers is not None:
            self._speakers_lbl.config(
                text=f"{speakers} speaker{'s' if speakers != 1 else ''}" if speakers else ""
            )

    def show(self, meta):
        self._live_mode = False
        self._title_manual = bool(meta.get("title_manual"))
        self._summary_manual = bool(meta.get("summary_manual"))
        self._edit_hint_var.set("Click the title to rename. Use Edit to change the summary. Manual edits are preserved.")
        # Stop playback if switching to a different recording
        if self._current and self._current.get("slug") != meta.get("slug"):
            self._player.stop()
            self._stop_tick()

        self._current = meta
        self._placeholder.pack_forget()
        self._content.pack(fill="both", expand=True)

        title = meta_title(meta)
        self._title_var.set(title)

        try:
            dt       = datetime.datetime.fromisoformat(meta["started_at"])
            date_str = dt.strftime("%b %d, %Y  %I:%M %p")
            age_str  = format_age(dt)
        except Exception:
            date_str = age_str = ""

        self._date_lbl.config(text=date_str)
        self._age_lbl.config(text=age_str)

        dur = meta.get("duration", 0)
        self._duration = dur or 0
        self._dur_lbl.config(text=format_duration(dur))
        self._dur_play_var.set(format_duration(dur) or "?")

        spk = meta.get("speakers", 0)
        self._speakers_lbl.config(
            text=f"{spk} speaker{'s' if spk != 1 else ''}" if spk else "")

        status = meta.get("status", "")
        st_map = {"done": "Ready", "processing": "Processing...", "error": "Error"}
        sc_map = {"done": GREEN, "error": RED}
        self._status_lbl.config(text=st_map.get(status, status),
                                fg=sc_map.get(status, YELLOW))

        slug      = meta.get("slug", "")
        has_audio = _audio_exists(slug)
        has_txt   = os.path.exists(_txt_path(slug))
        self._audio_btn.set_enabled(has_audio)
        self._resubmit_btn.set_enabled(_can_reprocess_meta(meta))
        self._del_btn.set_enabled(True)
        self._edit_summary_btn.set_enabled(True)
        self._open_transcript_btn.set_enabled(has_txt)
        self._copy_btn.set_enabled(bool(meta.get("transcript", "")))
        self._obsidian_btn.set_enabled(bool(meta.get("slug")))
        self._play_btn.set_enabled(has_audio)
        self._stop_btn.set_enabled(has_audio)

        if has_audio:
            self._player.load(_audio_path(slug))
            self._update_transport()

        body = meta.get("summary", "")
        if meta.get("error") and not (bool(meta.get("summary_manual")) and str(body).strip()):
            body = "Error: " + meta["error"]
        self._set_text(self._summary_txt, body)
        self._set_text(self._transcript_txt, meta.get("transcript", ""))
        self._sync_process_diagram(meta)

    # ── Transport ─────────────────────────────────────────────────────────────
    def _on_play(self):
        self._player.toggle()
        self._update_transport()
        if self._player.state == "playing":
            self._start_tick()
        else:
            self._stop_tick()

    def _on_stop(self):
        if self._live_mode and self._on_stop_recording:
            self._on_stop_recording()
            return
        self._player.stop()
        self._stop_tick()
        self._update_transport()

    def _start_tick(self):
        self._stop_tick()
        self._do_tick()

    def _do_tick(self):
        self._update_transport()
        if self._player.state == "playing":
            self._tick_id = self.after(333, self._do_tick)
        else:
            self._tick_id = None

    def _stop_tick(self):
        if self._tick_id:
            self.after_cancel(self._tick_id)
            self._tick_id = None

    def _update_transport(self):
        state = self._player.state
        self._play_btn.configure_button(text="Pause" if state == "playing" else "Play")

        pos = self._player.position
        self._pos_var.set(format_duration(pos) or "0:00")

        if self._duration > 0:
            frac = min(pos / self._duration, 1.0)
            w    = self._bar_canvas.winfo_width() or 200
            self._bar_canvas.coords(self._bar_fill, 0, 0, int(w * frac), 4)

    # ── File actions ──────────────────────────────────────────────────────────
    def _open_audio(self):
        if not self._current: return
        path = _audio_path(self._current["slug"])
        if os.path.exists(path):
            open_file(path)

    def _open_transcript(self):
        if not self._current: return
        path = _txt_path(self._current["slug"])
        if os.path.exists(path):
            open_file(path)

    def _on_delete_click(self):
        if self._current and self._on_delete:
            self._on_delete()

    def _on_export_obsidian_click(self):
        if self._current and self._on_export_obsidian:
            self._on_export_obsidian()

    def _on_resubmit_click(self):
        if self._current and self._on_resubmit:
            self._on_resubmit()

    def _on_rename_click(self, _event=None):
        if self._on_rename and (self._current or self._live_mode):
            self._on_rename()

    def _on_edit_summary_click(self):
        if self._on_edit_summary and (self._current or self._live_mode):
            self._on_edit_summary()

    def _copy_transcript(self):
        if not self._current:
            return
        text = self._current.get("transcript", "")
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            # Brief visual feedback
            self._copy_btn.configure_button(bg="#e0f2fe", fg=FG)
            self.after(
                800,
                lambda: self._copy_btn.configure_button(bg=ITEM_BG, fg=FG),
            )

    def clear(self):
        """Reset the detail panel to the placeholder state."""
        self._live_mode = False
        self._current = None
        self._title_manual = False
        self._summary_manual = False
        self._stop_tick()
        self._edit_summary_btn.set_enabled(False)
        self._content.pack_forget()
        self._placeholder.pack(expand=True)
        self._audio_btn.set_enabled(False)
        self._resubmit_btn.set_enabled(False)
        self._del_btn.set_enabled(False)
        self._play_btn.set_enabled(False)
        self._stop_btn.set_enabled(False)
        self._open_transcript_btn.set_enabled(False)
        self._copy_btn.set_enabled(False)
        self._obsidian_btn.set_enabled(False)
        self._set_process_nodes(audio="pending", chunks="pending", transcript="pending", summary="pending", ready="pending")

    def title_text(self):
        return self._title_var.get().strip()

    def summary_text(self):
        return self._summary_txt.get("1.0", "end-1c")

    def apply_manual_title(self, title):
        self._title_manual = True
        self._title_var.set(title)

    def apply_manual_summary(self, summary):
        self._summary_manual = True
        self._set_text(self._summary_txt, summary)
        if self._live_mode:
            self._set_process_nodes(summary="active" if summary.strip() else "pending")

    def set_live_transcript(self, text, processing=True, status_note=""):
        """Update transcript widget with live text during recording."""
        w = self._transcript_txt
        w.config(state="normal")
        w.delete("1.0", "end")
        if text:
            w.insert("1.0", text)
        if status_note:
            prefix = "\n\n" if text else ""
            w.insert("end", f"{prefix}[{status_note}]", "dim")
        elif processing:
            w.insert("end", "\n\n[still processing…]", "dim")
        w.tag_config("dim", foreground=FG_DIM)
        w.see("end")
        w.config(state="disabled")
        self._copy_btn.set_enabled(bool(text))
        if text.strip():
            self._set_process_nodes(audio="active", chunks="done", transcript="active")
        elif processing:
            self._set_process_nodes(audio="active", chunks="active", transcript="pending")

    def _set_text(self, w, text):
        w.config(state="normal")
        w.delete("1.0", "end")
        w.insert("1.0", text or "")
        w.config(state="disabled")

    def _set_process_nodes(self, **updates):
        changed = False
        for key, value in updates.items():
            if key in self._process_nodes and self._process_nodes[key] != value:
                self._process_nodes[key] = value
                changed = True
        if changed:
            self._draw_process_diagram()

    def _sync_process_diagram(self, meta):
        status = str(meta.get("status", "")).lower()
        has_audio = bool(meta.get("slug") and _audio_exists(meta.get("slug")))
        has_transcript = bool((meta.get("transcript") or "").strip())
        has_summary = bool((meta.get("summary") or "").strip()) and status != "error"
        self._set_process_nodes(
            audio="done" if has_audio else "pending",
            chunks="done" if has_transcript else ("active" if status == "processing" else "pending"),
            transcript="done" if has_transcript else ("active" if status == "processing" else "pending"),
            summary="done" if has_summary and status == "done" else ("error" if status == "error" else ("active" if status == "processing" else "pending")),
            ready="done" if status == "done" else ("error" if status == "error" else "pending"),
        )

    def _draw_process_diagram(self):
        canvas = self._process_canvas
        width = max(canvas.winfo_width(), 420)
        canvas.delete("all")
        steps = [
            ("audio", "Audio"),
            ("chunks", "Chunks"),
            ("transcript", "Transcript"),
            ("summary", "Summary"),
            ("ready", "Ready"),
        ]
        colors = {
            "pending": ("#d1d5db", "#9ca3af"),
            "active": ("#f59e0b", "#b45309"),
            "done": ("#10b981", "#047857"),
            "error": ("#ef4444", "#b91c1c"),
        }
        margin_x = 34
        radius = 11
        track_y = 24
        spacing = (width - (margin_x * 2)) / max(1, len(steps) - 1)
        centers = [margin_x + (spacing * idx) for idx in range(len(steps))]
        for idx in range(len(centers) - 1):
            left_state = self._process_nodes.get(steps[idx][0], "pending")
            line_color = colors[left_state][0] if left_state in ("active", "done") else "#d1d5db"
            canvas.create_line(centers[idx] + radius, track_y, centers[idx + 1] - radius, track_y, fill=line_color, width=4)
        for center, (key, label) in zip(centers, steps):
            fill, outline = colors.get(self._process_nodes.get(key, "pending"), colors["pending"])
            canvas.create_oval(center - radius, track_y - radius, center + radius, track_y + radius, fill=fill, outline=outline, width=2)
            canvas.create_text(center, track_y + 28, text=label, fill=FG_DIM, font=("Segoe UI", 8))


# ── Entry point ───────────────────────────────────────────────────────────────
def _send_to_existing(cmd="record"):
    """Try to send a command to an already-running instance. Returns True if sent."""
    if IS_WIN:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.3)
            s.connect((_WIN_IPC_HOST, _WIN_IPC_PORT))
            s.sendall(cmd.encode())
            s.close()
            return True
        except OSError:
            return False
    if not hasattr(socket, "AF_UNIX") or not os.path.exists(_SOCK_PATH):
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(_SOCK_PATH)
        s.sendall(cmd.encode())
        s.close()
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return False


def _handoff_to_existing(cmd, attempts=8, delay=0.2):
    for _ in range(max(1, attempts)):
        if _send_to_existing(cmd):
            return True
        time.sleep(delay)
    return False


if __name__ == "__main__":
    launch_token = _launch_token_from_argv()
    _update_launch_status(
        launch_token,
        "Starting Muesli...",
        progress=10,
        detail="Python is running; finishing startup imports and bootstrapping the app.",
    )
    bootstrap_started = os.environ.get("MUESLI_BOOTSTRAP_STARTED_AT", "").strip()
    if bootstrap_started:
        try:
            bootstrap_elapsed_ms = int((time.perf_counter() - float(bootstrap_started)) * 1000)
        except ValueError:
            bootstrap_elapsed_ms = None
        else:
            _append_launch_trace(
                launch_token,
                "python_import_bootstrap_complete",
                stage="Starting Muesli...",
                detail="Bootstrap plus muesli_gui import completed.",
                elapsed_ms=bootstrap_elapsed_ms,
                threshold_ms=2500,
                progress=10,
                flagged=bootstrap_elapsed_ms > 2500,
            )
    requested_cmd = "record" if "--record" in sys.argv else "show"
    if not _acquire_single_instance():
        _update_launch_status(
            launch_token,
            "Opening existing Muesli window...",
            progress=40,
            detail="An existing Muesli instance is already running; routing focus to it.",
        )
        if _handoff_to_existing(requested_cmd):
            _finish_launch_status(launch_token)
            sys.exit(0)
        _finish_launch_status(launch_token)
        sys.exit(0)

    # First instance — launch the app (always auto-record from hotkey)
    try:
        launch_probe = _LaunchProbe(launch_token)
        launch_probe.begin(
            "Loading application...",
            progress=20,
            detail="Preparing the desktop app after import/bootstrap completed.",
        )
        _apply_windows_app_identity()
        launch_probe.end(
            "app_identity_applied",
            stage="Loading application...",
            detail="Windows AppUserModelID was applied before creating the Tk window.",
            threshold_ms=600,
            progress=20,
        )
        app = MuesliApp(launch_token=launch_token, launch_probe=launch_probe)
        if IS_WIN:
            app.after(200, ensure_shared_dir_configured)
        app.mainloop()
    finally:
        _finish_launch_status(launch_token)
        _release_single_instance()
