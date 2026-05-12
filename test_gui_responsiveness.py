#!/usr/bin/env python3
"""Live UI-responsiveness regression test for Muesli.

Reproduces the 2026-05-12 wedge where _poll_runtime_state hit ollama
every second and starved the Tk event loop (Responding=False, cursor
spinning, delete/select stalled). Catches it again if anything in the
poll path or any other after()-scheduled callback ever blocks the main
thread for more than 100ms after a 5s post-launch grace period.

Method:
- Spawn the GUI via the same VBS launcher the user does at boot.
- Poll EnumWindows for a top-level visible window with title
  containing "Muesli" (max 120s — cold pygame import alone is ~12s
  on Py 3.12).
- Sleep GRACE_SECONDS so legitimate startup work (session list scan,
  whisper init, ollama probe to seed the cache) has time to settle.
- For SAMPLE_SECONDS, send WM_NULL via SendMessageTimeoutW with
  SMTO_ABORTIFHUNG and a RESPONSE_TIMEOUT_MS deadline. Any sample
  that fails to round-trip in time is recorded.
- Pass iff zero failed samples.
- Cleanup PostMessage WM_CLOSE then taskkill /F /T as a backstop.

Why a separate file (not in test_muesli.py [17]):
- Slow: ~30-90s for the cold launch alone.
- Live: actually launches the GUI; needs all heavy deps installed.
- Side-effecting: spawns a real window, writes runtime_state.json.
  We point MUESLI_HOME / MUESLI_RUNTIME_DIR at a temp dir so it
  doesn't pollute the user's session list.

Run on demand:

    %LOCALAPPDATA%\\muesli\\.venv\\Scripts\\python.exe test_gui_responsiveness.py

Exit 0 on pass, 1 on fail or launch timeout.
"""

from __future__ import annotations

import atexit
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

REPO_DIR = os.path.dirname(os.path.abspath(__file__))

# Isolate from the user's real Muesli state. Must be set before any code path
# that reads MUESLI_HOME (i.e. before any subprocess spawn that re-imports
# muesli — wscript inherits this env).
TEST_HOME = tempfile.mkdtemp(prefix="muesli-resp-test-")
TEST_RUNTIME = os.path.join(TEST_HOME, "runtime")
TEST_SHARED = os.path.join(TEST_HOME, "shared")
os.makedirs(TEST_RUNTIME, exist_ok=True)
os.makedirs(TEST_SHARED, exist_ok=True)
with open(os.path.join(TEST_HOME, "config.json"), "w", encoding="utf-8") as _cfg:
    json.dump({"shared_dir": TEST_SHARED}, _cfg)
os.environ["MUESLI_HOME"] = TEST_HOME
os.environ["MUESLI_RUNTIME_DIR"] = TEST_RUNTIME
atexit.register(lambda: shutil.rmtree(TEST_HOME, ignore_errors=True))

VBS = os.path.join(REPO_DIR, "muesli_gui_launcher.vbs")
WSCRIPT = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                      "System32", "wscript.exe")

# ── Tunables ─────────────────────────────────────────────────────────────────
MAX_LAUNCH_SECONDS = 120.0   # cold pygame + faster_whisper + ctranslate2
GRACE_SECONDS = 5.0          # window appeared; legitimate startup work
SAMPLE_SECONDS = 10.0        # how long to keep probing
SAMPLE_INTERVAL_S = 0.2      # 5 samples / second
RESPONSE_TIMEOUT_MS = 100    # the actual responsiveness budget

# ── Win32 plumbing ───────────────────────────────────────────────────────────
WM_NULL = 0x0000
WM_CLOSE = 0x0010
SMTO_ABORTIFHUNG = 0x0002

user32 = ctypes.windll.user32
EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumWindows.argtypes = [EnumWindowsProc, wt.LPARAM]
user32.EnumWindows.restype = wt.BOOL
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.SendMessageTimeoutW.argtypes = [
    wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM,
    wt.UINT, wt.UINT, ctypes.POINTER(ctypes.c_void_p),
]
user32.SendMessageTimeoutW.restype = wt.LPARAM
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL


def _process_image_name(pid):
    """Best-effort process image name (e.g. 'pythonw.exe'). Empty on error."""
    try:
        out = subprocess.check_output(
            ["wmic", "process", "where", f"ProcessId={pid}", "get", "Name", "/value"],
            stderr=subprocess.DEVNULL, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        if line.startswith("Name="):
            return line.split("=", 1)[1].strip().lower()
    return ""


def find_muesli_window():
    """Find the GUI's actual main window — title is exactly 'Muesli', owned
    by pythonw.exe (so the powershell splash 'Launching Muesli' and the
    tray sidecar's hidden 'Muesli Tray' are both excluded)."""
    found = []

    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        # Exact match: GUI sets self.title("Muesli"). Splash uses
        # "Launching Muesli", tray uses "Muesli Tray". Neither passes.
        if title != "Muesli":
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        # Splash is rendered by powershell.exe; GUI runs under pythonw.exe.
        # Belt-and-braces in case future Muesli versions title their splash
        # exactly "Muesli".
        if _process_image_name(int(pid.value)) not in ("pythonw.exe", "python.exe"):
            return True
        found.append((hwnd, int(pid.value), title))
        return False

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return found[0] if found else None


def send_wm_null(hwnd, timeout_ms):
    """SendMessageTimeoutW WM_NULL with SMTO_ABORTIFHUNG. Returns True iff the
    window's message pump processed the message before the timeout."""
    result = ctypes.c_void_p()
    ret = user32.SendMessageTimeoutW(
        hwnd, WM_NULL, 0, 0,
        SMTO_ABORTIFHUNG, timeout_ms,
        ctypes.byref(result),
    )
    return ret != 0


def main():
    print("=== muesli GUI responsiveness regression ===")
    print(f"VBS:           {VBS}")
    print(f"TEST_HOME:     {TEST_HOME}")
    print(f"grace:         {GRACE_SECONDS}s")
    print(f"sample window: {SAMPLE_SECONDS}s @ {int(SAMPLE_INTERVAL_S*1000)}ms")
    print(f"response cap:  {RESPONSE_TIMEOUT_MS}ms per WM_NULL")
    print()

    if not os.path.exists(VBS):
        print(f"FAIL: VBS launcher missing at {VBS}")
        return 1

    print("Launching via wscript...")
    subprocess.Popen([WSCRIPT, VBS], close_fds=True)

    print(f"Polling EnumWindows for the GUI window (max {MAX_LAUNCH_SECONDS}s)...")
    launch_start = time.monotonic()
    deadline = launch_start + MAX_LAUNCH_SECONDS
    win = None
    while time.monotonic() < deadline:
        win = find_muesli_window()
        if win:
            break
        time.sleep(0.5)
    if not win:
        print(f"FAIL: window did not appear within {MAX_LAUNCH_SECONDS}s")
        return 1
    hwnd, pid, title = win
    print(f"Window appeared after {time.monotonic() - launch_start:.1f}s: "
          f"hwnd={hwnd} pid={pid} title={title!r}")

    try:
        print(f"Grace period: {GRACE_SECONDS}s")
        time.sleep(GRACE_SECONDS)

        print(f"Sampling for {SAMPLE_SECONDS}s:")
        sample_deadline = time.monotonic() + SAMPLE_SECONDS
        ok = 0
        bad = 0
        slowest_ms = 0.0
        slowest_responsive_ms = 0.0
        while time.monotonic() < sample_deadline:
            t0 = time.monotonic()
            responded = send_wm_null(hwnd, RESPONSE_TIMEOUT_MS)
            dt_ms = (time.monotonic() - t0) * 1000
            slowest_ms = max(slowest_ms, dt_ms)
            if responded:
                ok += 1
                slowest_responsive_ms = max(slowest_responsive_ms, dt_ms)
            else:
                bad += 1
                print(f"  [unresponsive] WM_NULL exceeded {RESPONSE_TIMEOUT_MS}ms "
                      f"(elapsed {dt_ms:.0f}ms)")
            time.sleep(SAMPLE_INTERVAL_S)

        total = ok + bad
        print()
        print(f"Results: {ok}/{total} samples responded within {RESPONSE_TIMEOUT_MS}ms")
        print(f"  slowest sample (any):        {slowest_ms:.0f}ms")
        print(f"  slowest sample that passed:  {slowest_responsive_ms:.0f}ms")
        if bad == 0:
            print("PASS")
            return 0
        print(f"FAIL: {bad} samples exceeded the {RESPONSE_TIMEOUT_MS}ms responsiveness cap")
        return 1
    finally:
        print("Cleanup: PostMessage WM_CLOSE")
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        # Give _on_close a moment to run normally; backstop with taskkill /T.
        time.sleep(2.0)
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, check=False,
            )
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
