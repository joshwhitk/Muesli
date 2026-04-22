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
import platform
import socket
import ctypes
import importlib.util
import shutil
import urllib.request
import urllib.error
import pygame

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

# ── Single-instance socket ───────────────────────────────────────────────────
_SOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".muesli.sock")
_WIN_IPC_HOST = "127.0.0.1"
_WIN_IPC_PORT = 45873

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
HOTKEY_AGENT_SCRIPT = os.path.join(APP_DIR, "muesli_hotkey.py")
HOTKEY_AGENT_PID = os.path.join(APP_DIR, "muesli_hotkey.pid")
HOTKEY_AGENT_SHORTCUT = os.path.join(STARTUP_DIR, "Muesli Hotkey.lnk")
TASKBAR_PIN_DIR  = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Internet Explorer", "Quick Launch", "User Pinned", "TaskBar")
TASKBAR_SHORTCUT = os.path.join(TASKBAR_PIN_DIR, "Muesli.lnk")
LAUNCHER_EXE     = os.path.join(APP_DIR, "dist", "Muesli.exe")
DEFAULT_LAUNCH_HOTKEY = "Ctrl+Shift+`"
APP_USER_MODEL_ID = "Muesli.App"
os.makedirs(REC_DIR, exist_ok=True)

# ── Audio constants ───────────────────────────────────────────────────────────
RATE     = 16000
CHANNELS = 1
CHUNK    = 1024
FORMAT   = pyaudio.paInt16 if pyaudio else None
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

    backend = str(normalized.get("llm_backend", "auto")).lower()
    normalized["llm_backend"] = backend if backend in ("auto", "anthropic", "ollama", "local") else "auto"
    return normalized


def _whisper_quality_summary():
    free_gb = _free_disk_gb()
    drive, _ = os.path.splitdrive(os.path.expanduser("~"))
    drive_label = drive + "\\" if drive else os.path.expanduser("~")
    quality = _default_whisper_quality()
    label = WHISPER_QUALITY_LABELS[quality]
    return f"Default on this PC: {label} ({free_gb:.0f} GB free on {drive_label})"


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
    pythonw = os.path.join(APP_DIR, ".venv", "Scripts", "pythonw.exe")
    script = os.path.join(APP_DIR, "muesli_gui.py")
    args = f'"{script}"'
    if record:
        args += " --record"
    return pythonw, args


def _hotkey_agent_target():
    pythonw = os.path.join(APP_DIR, ".venv", "Scripts", "pythonw.exe")
    return pythonw, f'"{HOTKEY_AGENT_SCRIPT}"'


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
                "Where-Object { $_.Name -eq 'pythonw.exe' -and $_.CommandLine -match 'muesli_hotkey.py' } | "
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
    target = os.path.join(APP_DIR, ".venv", "Scripts", "pythonw.exe")
    if not os.path.exists(target):
        return
    subprocess.Popen(
        [target, HOTKEY_AGENT_SCRIPT],
        cwd=APP_DIR,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True,
    )


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


def open_settings_dialog(master):
    cfg = load_config()
    dialog = tk.Toplevel(master)
    dialog.title("Settings")
    dialog.configure(bg=DARK_BG)
    dialog.resizable(False, False)
    dialog.transient(master)
    dialog.grab_set()

    whisper_quality = tk.StringVar(value=cfg.get("whisper_quality", _default_whisper_quality()))
    llm_backend = tk.StringVar(value=cfg.get("llm_backend", "auto"))
    gguf_models = _list_gguf_models()
    ollama_models = _list_ollama_models()
    llm_local_model = tk.StringVar(value=cfg.get("llm_local_model", gguf_models[0] if gguf_models else ""))
    ollama_model = tk.StringVar(value=cfg.get("ollama_model", _recommend_ollama_model(ollama_models) or ""))
    saved = {"ok": False}

    def _section(parent, title, subtitle):
        frame = tk.Frame(parent, bg=PANEL_BG, bd=0, highlightthickness=1, highlightbackground=LINE)
        frame.pack(fill="x", padx=18, pady=(0, 14))
        tk.Label(frame, text=title, font=("Segoe UI Semibold", 10), bg=PANEL_BG, fg=FG).pack(anchor="w", padx=14, pady=(12, 2))
        tk.Label(frame, text=subtitle, font=FONT_SM, bg=PANEL_BG, fg=FG_DIM, wraplength=430, justify="left").pack(anchor="w", padx=14, pady=(0, 10))
        return frame

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
        "Whisper Transcription",
        "Choose between faster transcription and the higher-quality model. New installs default to High Quality when there is enough free disk space.",
    )
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

    buttons = tk.Frame(dialog, bg=DARK_BG)
    buttons.pack(fill="x", padx=18, pady=(4, 18))

    def _save():
        updated = load_config()
        updated["whisper_quality"] = whisper_quality.get()
        updated["whisper_model"] = WHISPER_QUALITY_MODELS[updated["whisper_quality"]]
        updated["llm_backend"] = backend_map.get(backend_display.get(), "auto")
        if ollama_models and ollama_model.get() in ollama_models:
            updated["ollama_model"] = ollama_model.get()
        if gguf_models and llm_local_model.get() in gguf_models:
            updated["llm_local_model"] = llm_local_model.get()
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

SHARED_DIR = get_shared_dir()

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

def get_summary_prompt():
    """Load the summary prompt from prompt.txt, creating it with the default if missing."""
    if not os.path.exists(PROMPT_FILE):
        with open(PROMPT_FILE, "w") as f:
            f.write(_DEFAULT_PROMPT)
    with open(PROMPT_FILE) as f:
        return f.read().strip()

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

def datetime_slug():
    return datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

def format_duration(seconds):
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def format_age(dt):
    delta = datetime.datetime.now().date() - dt.date()
    if delta.days == 0:   return "Today"
    if delta.days == 1:   return "Yesterday"
    if delta.days < 7:    return f"{delta.days} days ago"
    return dt.strftime("%b %d")

def load_recording(meta_path):
    try:
        with open(meta_path) as f:
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
    with open(path, "w") as f:
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
        "qwen3.5:4b",
        "qwen3.5:9b",
        "qwen3:8b",
        "gemma4:e4b",
        "gemma4:4b",
        "llama3.1:8b",
        "mistral:7b",
        "phi4",
        "deepseek-r1:8b",
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

def _llm_generate(prompt_text):
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
                timeout=120,
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
    """Process 60-second audio chunks: whisper transcribe → local LLM summarise.

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
        self._worker          = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def submit(self, wav_path):
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
            # 1. Transcribe
            try:
                segments, _ = _transcribe_segments(path, beam_size=5)
                text = " ".join(s.text.strip() for s in segments).strip()
            except Exception:
                text = ""
            self._transcripts.append(text)
            try:
                os.remove(path)
            except OSError:
                pass
            n = len(self._transcripts)
            if self._on_transcribed:
                self._on_transcribed(n, text)

            # 2. Summarise chunk with local LLM
            if text:
                try:
                    summary = _llm_generate(
                        "Summarise this conversation excerpt in 1-2 sentences. "
                        "Also note how many distinct speakers you detect.\n\n"
                        f"{text}")
                    self._summaries.append(summary)
                except Exception:
                    self._summaries.append("")
            else:
                self._summaries.append("")
            if self._on_summarised:
                self._on_summarised(n)

    def get_transcript(self):
        return " ".join(t for t in self._transcripts if t)

    def get_summaries(self):
        return list(self._summaries)


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

    # Transcribe with faster-whisper (skip if transcript already provided)
    if transcript is None:
        # Re-check size of the audio file to be transcribed
        audio_file = mp3_path if os.path.exists(mp3_path) else wav_dest
        if not large_file:
            large_file = _file_size_mb(audio_file) > 1.0

        if large_file:
            on_update(meta, "transcribing… 0%")
        else:
            on_update(meta, "transcribing…")
        try:
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
            meta["error"]  = f"Whisper: {str(e)[:120]}"
            _save_meta(meta); on_update(meta, meta["error"]); return

    # Summarise with local LLM
    on_update(meta, "summarising…")
    llm_used = False
    try:
        if summaries and any(summaries):
            # Merge chunk summaries (short input — fast)
            merged = "\n".join(f"- {s}" for s in summaries if s)
            prompt_text = get_summary_prompt().replace(
                "{transcript}",
                f"[Chunk summaries from a longer recording]\n{merged}")
        else:
            # Full transcript (fallback for non-chunked recordings)
            prompt_text = get_summary_prompt().replace("{transcript}", transcript)
        raw = _llm_generate(prompt_text)
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$",        "", raw)
        ai  = json.loads(raw)
        llm_used = True
    except Exception:
        ai = _fallback_ai_fields(transcript)

    default_title = default_session_title(meta.get("started_at", ""))
    title       = ai.get("title", "").strip() or default_title
    summary     = ai.get("summary", "").strip() if llm_used else ""
    speakers    = int(ai.get("speakers", 0) or 0) if llm_used else 0
    corrections = ai.get("corrections", "")
    bugs        = ai.get("bugs",        "")

    # Only rename files when an LLM supplied a real title.
    new_slug = slug
    if llm_used and title:
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

    # Write combined markdown to shared folder
    txt_body = f"# {title}\n\n"
    if summary:
        txt_body += f"## Summary\n\n{summary}\n\n"
    if corrections:
        txt_body += f"## Transcription Corrections\n\n{corrections}\n\n"
    if bugs:
        txt_body += f"## Bugs / Issues Mentioned\n\n{bugs}\n\n"
    txt_body += f"## Transcript\n\n{transcript}\n"
    with open(_txt_path(new_slug), "w") as f:
        f.write(txt_body)

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
    })
    meta.pop("error", None)
    # Remove old JSON if slug changed
    old_json = os.path.join(REC_DIR, slug + ".json")
    if new_slug != slug and os.path.exists(old_json):
        os.remove(old_json)
    _save_meta(meta)
    on_update(meta, "done")


# ── Recorder ──────────────────────────────────────────────────────────────────
# Reads per 30 seconds of audio (RATE / CHUNK = ~15.6 reads/sec × 30)
_READS_PER_CHUNK = int(30 * RATE / CHUNK)

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
        self._chunk_idx = 0
        self._sample_width = 2

    def start(self, on_chunk=None):
        """Begin recording. If `on_chunk` is provided, 60-second WAV chunks are
        saved and passed to the callback for live transcription."""
        self._slug      = datetime_slug()
        self._frames    = []
        self._on_chunk  = on_chunk
        self._chunk_idx = 0
        self._running   = True
        self._started   = time.time()
        if self._backend == "pyaudio":
            self._sample_width = self._pa.get_sample_size(FORMAT)
            self._stream = self._pa.open(
                format=FORMAT, channels=CHANNELS, rate=RATE,
                input=True, frames_per_buffer=CHUNK
            )
            self._thread = threading.Thread(target=self._record, daemon=True)
            self._thread.start()
        else:
            self._sample_width = 2
            self._stream = sd.InputStream(
                samplerate=RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK,
                callback=self._record_sounddevice,
            )
            self._stream.start()
        return self._slug

    def _record_sounddevice(self, indata, frames, time_info, status):
        if not self._running:
            return
        self._frames.append(indata.copy().tobytes())

    def _record(self):
        chunk_frames = []
        while self._running:
            data = self._stream.read(CHUNK, exception_on_overflow=False)
            self._frames.append(data)
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
        wf.setsampwidth(self._pa.get_sample_size(FORMAT))
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


class MuesliApp(tk.Tk):
    def __init__(self):
        super().__init__(className="muesli")
        if IS_WIN:
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
            except Exception:
                pass
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
        self._resume_progress = {}   # slug -> "transcribing 45%" for list labels

        # Taskbar icons — idle and recording states
        self._icon_idle = _load_app_icon(self)
        self._icon_rec  = self._icon_idle
        self._notepad_icon = self._load_detail_icon(NOTEPAD_ICON_PNG)
        self._copy_icon = self._load_detail_icon(COPY_ICON_PNG)
        self.wm_iconphoto(True, self._icon_idle)
        ensure_windows_shortcuts_if_missing()

        self._build_ui()
        self._bind_shortcuts()
        self._cleanup_stale()
        self._refresh_list()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # Listen for commands from second instances (single-instance pattern)
        self._start_ipc_listener()
        # Resume interrupted recordings in background
        self._status_var.set("Starting...")
        threading.Thread(target=self._startup_background, daemon=True).start()
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
                elif data == "toggle":
                    self.after(0, self._toggle_recording)
            except Exception:
                break

    def _auto_start_recording(self):
        """Start a new recording, raising the window first."""
        self.deiconify()
        self.lift()
        self.focus_force()
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

    def _edit_prompt(self):
        """Open prompt.txt in the system text editor."""
        get_summary_prompt()  # ensure file exists with default
        open_file(PROMPT_FILE)

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
        self._status_var.set("Settings saved")

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
        self.bind_all("<Control-p>", lambda e: (self._edit_prompt(), "break")[1])
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
            text="Edit Prompt",
            command=self._edit_prompt,
            font=FONT_SM,
            bg=ITEM_BG,
            fg=FG,
            active_bg=ITEM_ALT,
            shadow="#d7dde7",
            tooltip="Edit the summary prompt\nCtrl+P",
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
        self._listbox.bind("<ButtonRelease-1>", self._rename_selected_on_click, add="+")
        self._listbox.bind("<Double-Button-1>", self._rename_selected_from_list)

        paned.add(left, minsize=150, width=270)

        # Right detail
        self._detail = DetailPanel(paned, self._player,
                                   on_delete=self._delete_current,
                                   on_rename=self._rename_current,
                                   on_stop_recording=self._stop_recording,
                                   notepad_icon=self._notepad_icon,
                                   copy_icon=self._copy_icon,
                                   bg=DARK_BG)
        paned.add(self._detail, minsize=400)

        # Redraw list labels when sash is dragged (adapt to new width)
        self._listbox.bind("<Configure>", self._on_list_resize)
        self._list_last_width = 0

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

    def _on_select(self, _e):
        sel = self._listbox.curselection()
        if not sel: return
        meta = self._recordings[sel[0]]
        self._cur_meta = meta
        self._detail.show(meta)

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
        _save_meta(meta)
        self._refresh_list()
        for item in self._recordings:
            if item.get("slug") == meta.get("slug"):
                self._cur_meta = item
                self._detail.show(item)
                break
        self._status_var.set("Session renamed")

    def _rename_current(self):
        self._rename_meta(self._cur_meta)

    def _rename_selected_from_list(self, _event=None):
        sel = self._listbox.curselection()
        if not sel:
            return
        self._rename_meta(self._recordings[sel[0]])

    def _rename_selected_on_click(self, event):
        if not self._recordings:
            return
        idx = self._listbox.nearest(event.y)
        sel = self._listbox.curselection()
        if not sel or idx != sel[0]:
            return
        if not self._cur_meta or self._recordings[idx].get("slug") != self._cur_meta.get("slug"):
            return
        self.after(0, lambda: self._rename_meta(self._recordings[idx]))

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
        self._chunk_pipeline = ChunkPipeline(
            on_transcribed=lambda n, text: self.after(0,
                lambda t=text, nn=n: self._on_chunk_transcribed(nn, t)),
            on_summarised=lambda n: self.after(0,
                lambda nn=n: self._status_var.set(
                    f"Recording… ({nn} chunk{'s' if nn != 1 else ''} processed)"))
        )
        self._recorder.start(on_chunk=self._chunk_pipeline.submit)
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
        self._tick_timer()

    def _on_chunk_transcribed(self, n, text):
        """Called on main thread when a chunk has been transcribed — update live display."""
        if text:
            if self._live_transcript:
                self._live_transcript += " " + text
            else:
                self._live_transcript = text
        self._detail.set_live_transcript(
            self._live_transcript, processing=True)

    def _tick_timer(self):
        if not self._recording: return
        self._timer_lbl.config(text=format_duration(self._recorder.elapsed()))
        self._timer_id = self.after(500, self._tick_timer)

    def _stop_recording(self):
        self._recording = False
        if self._timer_id: self.after_cancel(self._timer_id)
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
        self._recordings.insert(0, meta)
        self._listbox.insert(0, self._format_label(meta, self._max_label_chars()))
        self._listbox.selection_clear(0, "end")
        self._listbox.selection_set(0)
        self._detail.show(meta)
        # Restore the live transcript that show(meta) just cleared
        if self._live_transcript:
            self._detail.set_live_transcript(
                self._live_transcript, processing=True)

        pipeline = self._chunk_pipeline

        def _finalize():
            # Wait for all chunk transcription + summarisation to finish
            pipeline.finish()
            transcript = pipeline.get_transcript().strip()
            summaries  = pipeline.get_summaries()
            # Show final transcript (no "[still processing...]")
            self.after(0, lambda: self._detail.set_live_transcript(
                transcript, processing=False))
            # Run conversion + final LLM merge (skip whisper — already done)
            process_recording(meta, self._on_proc_update,
                              transcript=(transcript or None), summaries=summaries)

        threading.Thread(target=_finalize, daemon=True).start()

    def _on_proc_update(self, meta, stage):
        self.after(0, lambda: self._apply_update(meta, stage))

    def _apply_update(self, meta, stage):
        self._status_var.set("" if stage == "done" else stage)
        self._refresh_list()
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


# ── Detail Panel ──────────────────────────────────────────────────────────────
class DetailPanel(tk.Frame):
    def __init__(self, parent, player, on_delete=None, on_rename=None, on_stop_recording=None, notepad_icon=None, copy_icon=None, **kw):
        super().__init__(parent, **kw)
        self._current   = None
        self._player    = player
        self._on_delete = on_delete
        self._on_rename = on_rename
        self._on_stop_recording = on_stop_recording
        self._notepad_icon = notepad_icon
        self._copy_icon = copy_icon
        self._tick_id   = None
        self._duration  = 0.0
        self._live_mode = False
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
        self._del_btn.pack(side="left")

        # ── Summary ───────────────────────────────────────────────────────
        tk.Label(c, text="Summary", font=("Segoe UI Semibold", 9),
                 bg=PANEL_BG, fg=FG_DIM).pack(anchor="w", pady=(4, 2))
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
        self._del_btn.set_enabled(False)
        self._open_transcript_btn.set_enabled(False)
        self._copy_btn.set_enabled(False)
        self._set_text(self._summary_txt, "")
        self.set_live_transcript("", processing=True)

    def show(self, meta):
        self._live_mode = False
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
        self._del_btn.set_enabled(True)
        self._open_transcript_btn.set_enabled(has_txt)
        self._copy_btn.set_enabled(bool(meta.get("transcript", "")))
        self._play_btn.set_enabled(has_audio)
        self._stop_btn.set_enabled(has_audio)

        if has_audio:
            self._player.load(_audio_path(slug))
            self._update_transport()

        body = "Error: " + meta["error"] if meta.get("error") else meta.get("summary", "")
        self._set_text(self._summary_txt, body)
        self._set_text(self._transcript_txt, meta.get("transcript", ""))

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

    def _on_rename_click(self, _event=None):
        if self._current and self._on_rename and not self._live_mode:
            self._on_rename()

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
        self._stop_tick()
        self._content.pack_forget()
        self._placeholder.pack(expand=True)
        self._audio_btn.set_enabled(False)
        self._del_btn.set_enabled(False)
        self._play_btn.set_enabled(False)
        self._stop_btn.set_enabled(False)
        self._open_transcript_btn.set_enabled(False)
        self._copy_btn.set_enabled(False)

    def set_live_transcript(self, text, processing=True):
        """Update transcript widget with live text during recording."""
        w = self._transcript_txt
        w.config(state="normal")
        w.delete("1.0", "end")
        if text:
            w.insert("1.0", text)
        if processing:
            w.insert("end", "\n\n[still processing…]", "dim")
            w.tag_config("dim", foreground=FG_DIM)
        w.see("end")
        w.config(state="disabled")
        self._copy_btn.set_enabled(bool(text))

    def _set_text(self, w, text):
        w.config(state="normal")
        w.delete("1.0", "end")
        w.insert("1.0", text or "")
        w.config(state="disabled")


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


if __name__ == "__main__":
    # If Muesli is already running, tell it to start recording and exit
    if _send_to_existing("record"):
        sys.exit(0)

    # First instance — launch the app (always auto-record from hotkey)
    app = MuesliApp()
    if IS_WIN:
        app.after(200, ensure_shared_dir_configured)
    app.mainloop()
