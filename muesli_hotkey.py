#!/usr/bin/env python3
"""Global Windows hotkey listener for Muesli."""

import atexit
import ctypes
from ctypes import wintypes
import json
import os
import subprocess
import sys


APP_DIR = os.environ.get("MUESLI_HOME") or os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
PID_FILE = os.path.join(APP_DIR, "muesli_hotkey.pid")
DEFAULT_HOTKEY = "Ctrl+Shift+`"
HOTKEY_ID = 1

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
VK_F1 = 0x70
VK_OEM_3 = 0xC0
WM_HOTKEY = 0x0312

user32 = ctypes.windll.user32


def load_hotkey():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("launch_hotkey", DEFAULT_HOTKEY)
    except Exception:
        return DEFAULT_HOTKEY


def parse_hotkey(combo):
    parts = [part.strip() for part in combo.split("+") if part.strip()]
    if not parts:
        raise ValueError("Empty hotkey")
    mods = 0
    key = parts[-1].upper()
    for part in parts[:-1]:
        upper = part.upper()
        if upper == "CTRL":
            mods |= MOD_CONTROL
        elif upper == "ALT":
            mods |= MOD_ALT
        elif upper == "SHIFT":
            mods |= MOD_SHIFT
        elif upper == "WIN":
            mods |= MOD_WIN
    if key == "F1":
        return mods, VK_F1
    if key in ("`", "GRAVE", "QUOTELEFT", "OEM_3"):
        return mods, VK_OEM_3
    raise ValueError("Unsupported hotkey")


def app_command():
    launcher_exe = os.path.join(APP_DIR, "dist", "Muesli.exe")
    if os.path.exists(launcher_exe):
        return [launcher_exe, "--record"]
    pythonw = os.path.join(APP_DIR, ".venv", "Scripts", "pythonw.exe")
    script = os.path.join(APP_DIR, "muesli_gui.py")
    return [pythonw, script, "--record"]


def write_pid():
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def cleanup():
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, "r", encoding="utf-8") as f:
                current = f.read().strip()
            if current == str(os.getpid()):
                os.remove(PID_FILE)
    except Exception:
        pass


def main():
    hotkey = load_hotkey()
    mods, vk = parse_hotkey(hotkey)
    if not user32.RegisterHotKey(None, HOTKEY_ID, mods, vk):
        return 1
    write_pid()
    atexit.register(cleanup)
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
            try:
                subprocess.Popen(
                    app_command(),
                    cwd=APP_DIR,
                    creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    close_fds=True,
                )
            except Exception:
                pass
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    user32.UnregisterHotKey(None, HOTKEY_ID)
    return 0


if __name__ == "__main__":
    sys.exit(main())
