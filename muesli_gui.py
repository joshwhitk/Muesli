#!/usr/bin/env python3
"""Muesli - AI call recorder and transcriber."""

import tkinter as tk
from tkinter import simpledialog, filedialog, messagebox
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
import pygame
from faster_whisper import WhisperModel

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
APP_USER_MODEL_ID = "JoshStudio.Muesli"
os.makedirs(REC_DIR, exist_ok=True)

# ── Audio constants ───────────────────────────────────────────────────────────
RATE     = 16000
CHANNELS = 1
CHUNK    = 1024
FORMAT   = pyaudio.paInt16 if pyaudio else None

# ── Config ────────────────────────────────────────────────────────────────────
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


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
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True)
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

# SHARED_DIR holds exported audio/transcript artifacts.
_LOCAL_SHARED = os.path.join(os.path.expanduser("~"), "Documents", "MuesliData", "analytics", "audio")
_DEFAULT_SHARED = (
    _LOCAL_SHARED
    if IS_WIN else
    "/srv/al/muesli"
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
            os.remove(path)
    # Delete transcript txt
    txt = _txt_path(slug)
    if os.path.exists(txt):
        os.remove(txt)
    # Delete metadata JSON
    meta_path = os.path.join(REC_DIR, slug + ".json")
    if os.path.exists(meta_path):
        os.remove(meta_path)
    # Delete local WAV if still present
    local_wav = os.path.join(REC_DIR, slug + ".wav")
    if os.path.exists(local_wav):
        os.remove(local_wav)

# ── AI Processing ─────────────────────────────────────────────────────────────
_whisper_model = None


def _default_whisper_candidates():
    cfg = load_config()
    model_name = cfg.get("whisper_model", "large-v3")
    force_device = cfg.get("whisper_device", "auto")
    candidates = []

    if force_device in ("auto", "cuda"):
        candidates.append((model_name, "cuda", "float16"))
    if force_device in ("auto", "cpu"):
        candidates.append((model_name, "cpu", "int8"))
    if model_name != "medium":
        candidates.append(("medium", "cpu", "int8"))
    return candidates

def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        last_error = None
        for model_name, device, compute_type in _default_whisper_candidates():
            try:
                _whisper_model = WhisperModel(
                    model_name,
                    device=device,
                    compute_type=compute_type,
                )
                break
            except Exception as exc:
                last_error = exc
        if _whisper_model is None:
            raise RuntimeError(f"Unable to load Whisper model: {last_error}")
    return _whisper_model

# ── Local LLM ────────────────────────────────────────────────────────────────
_llm_model = None
_LLM_MODEL_DIR = os.path.join(APP_DIR, "models")

def _find_gguf_model():
    """Return path to the first .gguf file in the models directory."""
    if not os.path.isdir(_LLM_MODEL_DIR):
        return None
    for f in sorted(os.listdir(_LLM_MODEL_DIR)):
        if f.endswith(".gguf"):
            return os.path.join(_LLM_MODEL_DIR, f)
    return None

def _get_llm_model():
    global _llm_model
    if _llm_model is None:
        from llama_cpp import Llama
        model_path = _find_gguf_model()
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
    """Run prompt through Claude API first (fast), falling back to local LLM."""
    # Try Claude API first — much faster than CPU inference
    cfg = load_config()
    api_key = cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
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

    # Fallback: local LLM (slow on CPU but works offline)
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

    raise RuntimeError("No LLM available (no Claude API key, local model failed)")


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
        whisper = _get_whisper_model()
        while True:
            path = self._queue.get()
            if path is None:
                self._done.set()
                return
            # 1. Transcribe
            try:
                segments, _ = whisper.transcribe(path, beam_size=5)
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
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
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
                    capture_output=True, check=True
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
            model = _get_whisper_model()
            segments, info = model.transcribe(audio_file, beam_size=5)
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
    except Exception:
        ai = {"title": "Recording", "summary": "", "speakers": 1}

    title       = ai.get("title",       "Recording").strip() or "Recording"
    summary     = ai.get("summary",     "")
    speakers    = int(ai.get("speakers", 1))
    corrections = ai.get("corrections", "")
    bugs        = ai.get("bugs",        "")

    # Build slug from title, avoid collisions
    new_slug  = slug_from_title(title)
    candidate = new_slug
    i = 2
    while (os.path.exists(os.path.join(REC_DIR, candidate + ".json")) and
           candidate != slug):
        candidate = f"{new_slug}-{i}"; i += 1
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
    txt_body = f"# {title}\n\n## Summary\n\n{summary}\n\n"
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

        meta = {
            "slug":       self._slug,
            "title":      self._slug,
            "started_at": datetime.datetime.now().isoformat(),
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
DARK_BG  = "#1e1e1e"
PANEL_BG = "#252526"
ITEM_BG  = "#2d2d30"
ITEM_SEL = "#094771"
FG       = "#d4d4d4"
FG_DIM   = "#858585"
FG_LINK  = "#4fc1ff"
RED      = "#f44747"
GREEN    = "#4ec9b0"
YELLOW   = "#dcdcaa"
FONT_SM  = ("Ubuntu", 9)
FONT_LG  = ("Ubuntu", 13, "bold")
FONT_MON = ("Ubuntu Mono", 10)

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

        # Taskbar icons — green (idle) and red (recording)
        self._icon_idle = _load_app_icon(self)
        self._icon_rec  = self._icon_idle
        self.wm_iconphoto(True, self._icon_idle)
        ensure_windows_shortcuts()

        self._build_ui()
        self._cleanup_stale()
        self._refresh_list()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # Listen for commands from second instances (single-instance pattern)
        self._start_ipc_listener()
        # Resume interrupted recordings in background
        self._status_var.set("Starting…")
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

    # ── Layout ────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Top bar
        top = tk.Frame(self, bg=DARK_BG, pady=8, padx=12)
        top.pack(fill="x")
        tk.Label(top, text="⬤  Muesli", font=FONT_LG,
                 bg=DARK_BG, fg=GREEN).pack(side="left")
        self._rec_btn = tk.Button(
            top, text="  ● Start Recording  ",
            font=("Ubuntu", 11, "bold"),
            bg=RED, fg="white", activebackground="#c53030",
            relief="flat", padx=16, pady=6, cursor="hand2",
            command=self._toggle_recording
        )
        self._rec_btn.pack(side="right")
        if not self._recording_available:
            self._rec_btn.config(state="disabled", bg="#555555", activebackground="#555555")
        tk.Button(
            top, text="✎ Prompt", font=FONT_SM,
            bg=DARK_BG, fg=FG_DIM,
            activeforeground="white", activebackground=DARK_BG,
            relief="flat", bd=0, padx=8, cursor="hand2",
            command=self._edit_prompt
        ).pack(side="right", padx=(0, 10))
        tk.Button(
            top, text="Set Shortcut", font=FONT_SM,
            bg=DARK_BG, fg=FG_DIM,
            activeforeground="white", activebackground=DARK_BG,
            relief="flat", bd=0, padx=8, cursor="hand2",
            command=self._set_shortcut
        ).pack(side="right", padx=(0, 10))
        self._timer_lbl = tk.Label(top, text="",
                                   font=("Ubuntu Mono", 12, "bold"),
                                   bg=DARK_BG, fg=RED)
        self._timer_lbl.pack(side="right", padx=12)

        # Status bar
        initial_status = "Ready" if self._recording_available else "Ready (recording backend unavailable)"
        self._status_var = tk.StringVar(value=initial_status)
        tk.Label(self, textvariable=self._status_var, font=FONT_SM,
                 bg=DARK_BG, fg=FG_DIM, anchor="w", padx=12).pack(fill="x")
        tk.Frame(self, bg="#3c3c3c", height=1).pack(fill="x")

        # Main split — draggable PanedWindow
        paned = tk.PanedWindow(self, orient="horizontal",
                               bg="#3c3c3c", sashwidth=4, sashpad=0,
                               bd=0, relief="flat")
        paned.pack(fill="both", expand=True)

        # Left list
        left = tk.Frame(paned, bg=PANEL_BG, width=270)
        left.pack_propagate(False)
        tk.Label(left, text="SESSIONS", font=("Ubuntu", 8, "bold"),
                 bg=PANEL_BG, fg=FG_DIM, anchor="w",
                 padx=10, pady=8).pack(fill="x")

        lf = tk.Frame(left, bg=PANEL_BG)
        lf.pack(fill="both", expand=True)
        sb = tk.Scrollbar(lf, bg=PANEL_BG, troughcolor=PANEL_BG, relief="flat")
        sb.pack(side="right", fill="y")
        self._listbox = tk.Listbox(
            lf, bg=PANEL_BG, fg=FG,
            selectbackground=ITEM_SEL, selectforeground="white",
            font=FONT_SM, relief="flat", bd=0, activestyle="none",
            yscrollcommand=sb.set, cursor="hand2"
        )
        self._listbox.pack(fill="both", expand=True)
        sb.config(command=self._listbox.yview)
        self._listbox.bind("<<ListboxSelect>>", self._on_select)

        paned.add(left, minsize=150, width=270)

        # Right detail
        self._detail = DetailPanel(paned, self._player,
                                   on_delete=self._delete_current, bg=DARK_BG)
        paned.add(self._detail, minsize=400)

        # Redraw list labels when sash is dragged (adapt to new width)
        self._paned = paned
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
        self._recordings = list_recordings()
        self._redraw_labels()

    def _on_select(self, _e):
        sel = self._listbox.curselection()
        if not sel: return
        meta = self._recordings[sel[0]]
        self._cur_meta = meta
        self._detail.show(meta)

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
        self._rec_btn.config(text="  ■ Stop Recording  ",
                             bg="#333333", fg=RED)
        self.wm_iconphoto(True, self._icon_rec)
        self._status_var.set("Recording…")
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
        self._rec_btn.config(text="  ● Start Recording  ", bg=RED, fg="white")
        self.wm_iconphoto(True, self._icon_idle)
        self._status_var.set("Finishing processing…")
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
                for i, m in enumerate(self._recordings):
                    if m.get("slug") == meta.get("slug"):
                        self._listbox.selection_clear(0, "end")
                        self._listbox.selection_set(i)
                        break

    def _delete_current(self):
        """Delete the currently selected recording and refresh the UI."""
        if not self._cur_meta:
            return
        self._player.stop()
        delete_recording(self._cur_meta)
        self._cur_meta = None
        self._detail.clear()
        self._refresh_list()
        self._status_var.set("Recording deleted")


# ── Detail Panel ──────────────────────────────────────────────────────────────
class DetailPanel(tk.Frame):
    def __init__(self, parent, player, on_delete=None, **kw):
        super().__init__(parent, **kw)
        self._current   = None
        self._player    = player
        self._on_delete = on_delete
        self._tick_id   = None
        self._duration  = 0.0
        self._build()

    def _build(self):
        self._placeholder = tk.Label(
            self, text="Select a session to view details",
            font=FONT_SM, bg=DARK_BG, fg=FG_DIM
        )
        self._placeholder.pack(expand=True)

        c = tk.Frame(self, bg=DARK_BG, padx=24, pady=18)
        self._content = c

        # ── Title ─────────────────────────────────────────────────────────
        tk.Label(c, text="SESSION", font=("Ubuntu", 8, "bold"),
                 bg=DARK_BG, fg=FG_DIM).pack(anchor="w")
        self._title_var = tk.StringVar()
        tk.Label(c, textvariable=self._title_var,
                 font=("Ubuntu", 15, "bold"), bg=DARK_BG, fg=FG,
                 wraplength=560, justify="left").pack(anchor="w", pady=(2, 10))

        # ── Meta row ──────────────────────────────────────────────────────
        mr = tk.Frame(c, bg=DARK_BG)
        mr.pack(anchor="w", pady=(0, 14))
        self._date_lbl     = tk.Label(mr, font=FONT_SM, bg=DARK_BG, fg=FG_DIM)
        self._age_lbl      = tk.Label(mr, font=FONT_SM, bg=DARK_BG, fg=FG_DIM)
        self._dur_lbl      = tk.Label(mr, font=FONT_SM, bg=DARK_BG, fg=FG_DIM)
        self._speakers_lbl = tk.Label(mr, font=FONT_SM, bg=DARK_BG, fg=FG_DIM)
        self._status_lbl   = tk.Label(mr, font=FONT_SM, bg=DARK_BG, fg=YELLOW)
        dot = lambda: tk.Label(mr, text=" · ", bg=DARK_BG, fg=FG_DIM, font=FONT_SM)
        self._date_lbl.pack(side="left")
        dot().pack(side="left"); self._age_lbl.pack(side="left")
        dot().pack(side="left"); self._dur_lbl.pack(side="left")
        dot().pack(side="left"); self._speakers_lbl.pack(side="left")
        dot().pack(side="left"); self._status_lbl.pack(side="left")

        # ── Transport controls ────────────────────────────────────────────
        transport = tk.Frame(c, bg=DARK_BG)
        transport.pack(anchor="w", pady=(0, 14))

        btn_cfg = dict(font=("Ubuntu", 14), bg=ITEM_BG, fg=FG,
                       activebackground="#3a3a3d", activeforeground="white",
                       relief="flat", bd=0, padx=10, pady=4, cursor="hand2")

        self._play_btn = tk.Button(transport, text="▶", command=self._on_play, **btn_cfg)
        self._stop_btn = tk.Button(transport, text="⏹", command=self._on_stop, **btn_cfg)
        self._play_btn.pack(side="left", padx=(0, 4))
        self._stop_btn.pack(side="left", padx=(0, 12))

        self._pos_var = tk.StringVar(value="0:00")
        tk.Label(transport, textvariable=self._pos_var,
                 font=("Ubuntu Mono", 10), bg=DARK_BG, fg=FG_DIM).pack(side="left")
        tk.Label(transport, text=" / ", font=FONT_SM,
                 bg=DARK_BG, fg=FG_DIM).pack(side="left")
        self._dur_play_var = tk.StringVar(value="0:00")
        tk.Label(transport, textvariable=self._dur_play_var,
                 font=("Ubuntu Mono", 10), bg=DARK_BG, fg=FG_DIM).pack(side="left")

        # Progress bar (Canvas)
        self._bar_canvas = tk.Canvas(transport, height=4, width=200,
                                     bg="#3c3c3c", bd=0, highlightthickness=0)
        self._bar_canvas.pack(side="left", padx=(14, 0), pady=6)
        self._bar_fill = self._bar_canvas.create_rectangle(
            0, 0, 0, 4, fill=GREEN, outline="")

        # ── File links ────────────────────────────────────────────────────
        fr = tk.Frame(c, bg=DARK_BG)
        fr.pack(anchor="w", pady=(0, 14))
        self._audio_btn = self._link_btn(fr, "🔊 open in player", self._open_audio)
        self._audio_btn.pack(side="left")
        tk.Label(fr, text="   ", bg=DARK_BG).pack(side="left")
        self._txt_btn = self._link_btn(fr, "📄 open transcript", self._open_transcript)
        self._txt_btn.pack(side="left")
        tk.Label(fr, text="   ", bg=DARK_BG).pack(side="left")
        self._del_btn = tk.Button(fr, text="🗑 delete", font=FONT_SM,
                                  bg=DARK_BG, fg=RED,
                                  activeforeground="white", activebackground=DARK_BG,
                                  relief="flat", bd=0, cursor="hand2",
                                  command=self._on_delete_click)
        self._del_btn.pack(side="left")

        # ── Summary ───────────────────────────────────────────────────────
        tk.Label(c, text="SUMMARY", font=("Ubuntu", 8, "bold"),
                 bg=DARK_BG, fg=FG_DIM).pack(anchor="w", pady=(4, 2))
        self._summary_txt = tk.Text(c, height=4, wrap="word",
                                    bg=ITEM_BG, fg=FG, relief="flat", bd=0,
                                    font=FONT_SM, padx=8, pady=6, state="disabled")
        self._summary_txt.pack(fill="x", pady=(0, 12))

        # ── Transcript ────────────────────────────────────────────────────
        tr_hdr = tk.Frame(c, bg=DARK_BG)
        tr_hdr.pack(fill="x", pady=(0, 2))
        tk.Label(tr_hdr, text="TRANSCRIPT", font=("Ubuntu", 8, "bold"),
                 bg=DARK_BG, fg=FG_DIM).pack(side="left")
        self._copy_btn = tk.Button(
            tr_hdr, text="📋", font=("Ubuntu", 9),
            bg=DARK_BG, fg=FG_DIM,
            activeforeground="white", activebackground=DARK_BG,
            relief="flat", bd=0, padx=4, cursor="hand2",
            command=self._copy_transcript)
        self._copy_btn.pack(side="left", padx=(6, 0))
        self._transcript_txt = tk.Text(c, height=10, wrap="word",
                                       bg=ITEM_BG, fg=FG, relief="flat", bd=0,
                                       font=FONT_MON, padx=8, pady=6, state="disabled")
        self._transcript_txt.pack(fill="both", expand=True)

    def _link_btn(self, parent, label, cmd):
        return tk.Button(parent, text=label, font=FONT_SM,
                         bg=DARK_BG, fg=FG_LINK,
                         activeforeground="white", activebackground=DARK_BG,
                         relief="flat", bd=0, cursor="hand2", command=cmd)

    # ── Show ──────────────────────────────────────────────────────────────────
    def show_live(self):
        """Prepare the detail panel for live recording display."""
        self._current = None
        self._placeholder.pack_forget()
        self._content.pack(fill="both", expand=True)
        self._title_var.set("Recording…")
        self._date_lbl.config(text="")
        self._age_lbl.config(text="")
        self._dur_lbl.config(text="")
        self._speakers_lbl.config(text="")
        self._status_lbl.config(text="⟳ recording…", fg=YELLOW)
        self._play_btn.config(state="disabled")
        self._stop_btn.config(state="disabled")
        self._audio_btn.config(state="disabled", fg=FG_DIM)
        self._txt_btn.config(state="disabled", fg=FG_DIM)
        self._set_text(self._summary_txt, "")
        self.set_live_transcript("", processing=True)

    def show(self, meta):
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
        st_map = {"done": "✓ done", "processing": "⟳ processing…", "error": "✗ error"}
        sc_map = {"done": GREEN, "error": RED}
        self._status_lbl.config(text=st_map.get(status, status),
                                fg=sc_map.get(status, YELLOW))

        slug      = meta.get("slug", "")
        has_audio = _audio_exists(slug)
        has_txt   = os.path.exists(_txt_path(slug))
        self._audio_btn.config(state="normal" if has_audio else "disabled",
                               fg=FG_LINK if has_audio else FG_DIM)
        self._txt_btn.config(state="normal"  if has_txt   else "disabled",
                             fg=FG_LINK if has_txt   else FG_DIM)
        self._play_btn.config(state="normal" if has_audio else "disabled")
        self._stop_btn.config(state="normal" if has_audio else "disabled")

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
        self._play_btn.config(text="⏸" if state == "playing" else "▶")

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

    def _copy_transcript(self):
        if not self._current:
            return
        text = self._current.get("transcript", "")
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            # Brief visual feedback
            orig = self._copy_btn.cget("fg")
            self._copy_btn.config(fg=GREEN)
            self.after(800, lambda: self._copy_btn.config(fg=orig))

    def clear(self):
        """Reset the detail panel to the placeholder state."""
        self._current = None
        self._stop_tick()
        self._content.pack_forget()
        self._placeholder.pack(expand=True)

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
