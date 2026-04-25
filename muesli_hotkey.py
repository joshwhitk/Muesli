#!/usr/bin/env python3
"""Windows tray sidecar and global hotkey listener for Muesli."""

import atexit
import ctypes
from ctypes import wintypes
import json
import os
import socket
import subprocess
import sys
import time

from muesli_runtime import load_runtime_state, update_runtime_state

APP_DIR = os.environ.get("MUESLI_HOME") or os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
PID_FILE = os.path.join(APP_DIR, "muesli_hotkey.pid")
ICON_FILE = os.path.join(APP_DIR, "assets", "muesli-icon.ico")
DEFAULT_HOTKEY = "Ctrl+Shift+`"
HOTKEY_ID = 1
TRAY_UID = 1
WSCRIPT_EXE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "wscript.exe")
GUI_LAUNCHER = os.path.join(APP_DIR, "muesli_gui_launcher.vbs")

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
VK_F1 = 0x70
VK_OEM_3 = 0xC0
WM_HOTKEY = 0x0312
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_MEASUREITEM = 0x002C
WM_DRAWITEM = 0x002B
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1
DEBOUNCE_SECONDS = 1.0
_WIN_IPC_HOST = "127.0.0.1"
_WIN_IPC_PORT = 45873
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
NIF_MESSAGE = 0x0001
NIF_ICON = 0x0002
NIF_TIP = 0x0004
NIF_SHOWTIP = 0x0080
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004
NOTIFYICON_VERSION_4 = 4
MF_STRING = 0x0000
MF_OWNERDRAW = 0x0100
MF_SEPARATOR = 0x0800
MF_DISABLED = 0x0002
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
IDI_APPLICATION = 32512
ODT_MENU = 1
ODS_SELECTED = 0x0001
ODS_DISABLED = 0x0004
COLOR_MENU = 4
COLOR_MENUTEXT = 7
COLOR_HIGHLIGHT = 13
COLOR_HIGHLIGHTTEXT = 14
COLOR_GRAYTEXT = 17
DEFAULT_GUI_FONT = 17
TRANSPARENT = 1
DT_SINGLELINE = 0x0020
DT_VCENTER = 0x0004
DT_LEFT = 0x0000
MENU_SHOW = 1001
MENU_RECORD = 1002
MENU_PAUSE = 1003
MENU_EXIT = 1004

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32
LRESULT = getattr(wintypes, "LRESULT", wintypes.LPARAM)
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HCURSOR),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class MEASUREITEMSTRUCT(ctypes.Structure):
    _fields_ = [
        ("CtlType", wintypes.UINT),
        ("CtlID", wintypes.UINT),
        ("itemID", wintypes.UINT),
        ("itemWidth", wintypes.UINT),
        ("itemHeight", wintypes.UINT),
        ("itemData", ctypes.c_void_p),
    ]


class DRAWITEMSTRUCT(ctypes.Structure):
    _fields_ = [
        ("CtlType", wintypes.UINT),
        ("CtlID", wintypes.UINT),
        ("itemID", wintypes.UINT),
        ("itemAction", wintypes.UINT),
        ("itemState", wintypes.UINT),
        ("hwndItem", wintypes.HWND),
        ("hDC", wintypes.HDC),
        ("rcItem", wintypes.RECT),
        ("itemData", ctypes.c_void_p),
    ]


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


def app_command(record=False):
    launcher_exe = os.path.join(APP_DIR, "dist", "Muesli.exe")
    if os.path.exists(launcher_exe):
        return [launcher_exe, "--record"] if record else [launcher_exe]
    cmd = [WSCRIPT_EXE, GUI_LAUNCHER]
    if record:
        cmd.append("--record")
    return cmd


def write_pid():
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def send_to_existing(cmd="record"):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        s.connect((_WIN_IPC_HOST, _WIN_IPC_PORT))
        s.sendall(cmd.encode())
        s.close()
        return True
    except OSError:
        return False


def launch_app(record=False):
    cmd = "record" if record else "show"
    if send_to_existing(cmd):
        return True
    subprocess.Popen(
        app_command(record=record),
        cwd=APP_DIR,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True,
    )
    return True


def toggle_processing_pause():
    state = load_runtime_state()
    paused = not bool(state.get("processing_paused", False))
    update_runtime_state(processing_paused=paused)
    return paused


def _pid_alive(pid):
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def _terminate_pid(pid):
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return
    if pid <= 0:
        return
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def cleanup():
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, "r", encoding="utf-8") as f:
                current = f.read().strip()
            if current == str(os.getpid()):
                os.remove(PID_FILE)
    except Exception:
        pass
    try:
        update_runtime_state(sidecar_pid=None)
    except Exception:
        pass


def _tray_event_code(lparam):
    try:
        return int(lparam) & 0xFFFF
    except Exception:
        return 0


class TraySidecar:
    def __init__(self, mods, vk):
        self._mods = mods
        self._vk = vk
        self._last_launch = 0.0
        self._last_tip = ""
        self._class_name = f"MuesliTrayWindow.{os.getpid()}"
        self._wndproc = WNDPROC(self._window_proc)
        self._hwnd = None
        self._icon = None
        self._nid = None

    def _menu_label(self, cmd):
        if cmd == MENU_SHOW:
            return "Show Muesli"
        if cmd == MENU_RECORD:
            return "Start Recording"
        if cmd == MENU_PAUSE:
            paused = bool(load_runtime_state().get("processing_paused", False))
            return "Resume Processing" if paused else "Pause Processing"
        if cmd == MENU_EXIT:
            return "Exit"
        return ""

    def _measure_menu_item(self, lparam):
        mis = ctypes.cast(lparam, ctypes.POINTER(MEASUREITEMSTRUCT)).contents
        if mis.CtlType != ODT_MENU:
            return False
        label = self._menu_label(mis.itemID)
        if not label:
            return False
        hdc = user32.GetDC(self._hwnd)
        font = gdi32.GetStockObject(DEFAULT_GUI_FONT)
        old_font = gdi32.SelectObject(hdc, font)
        size = SIZE()
        gdi32.GetTextExtentPoint32W(hdc, label, len(label), ctypes.byref(size))
        gdi32.SelectObject(hdc, old_font)
        user32.ReleaseDC(self._hwnd, hdc)
        mis.itemWidth = max(180, size.cx + 28)
        mis.itemHeight = max(24, size.cy + 10)
        return True

    def _draw_menu_item(self, lparam):
        dis = ctypes.cast(lparam, ctypes.POINTER(DRAWITEMSTRUCT)).contents
        if dis.CtlType != ODT_MENU:
            return False
        label = self._menu_label(dis.itemID)
        if not label:
            return False
        selected = bool(dis.itemState & ODS_SELECTED)
        disabled = bool(dis.itemState & ODS_DISABLED)
        bg_brush = user32.GetSysColorBrush(COLOR_HIGHLIGHT if selected else COLOR_MENU)
        user32.FillRect(dis.hDC, ctypes.byref(dis.rcItem), bg_brush)
        text_color = (
            user32.GetSysColor(COLOR_GRAYTEXT) if disabled else
            user32.GetSysColor(COLOR_HIGHLIGHTTEXT if selected else COLOR_MENUTEXT)
        )
        gdi32.SetBkMode(dis.hDC, TRANSPARENT)
        gdi32.SetTextColor(dis.hDC, text_color)
        font = gdi32.GetStockObject(DEFAULT_GUI_FONT)
        old_font = gdi32.SelectObject(dis.hDC, font)
        rect = wintypes.RECT(dis.rcItem.left + 12, dis.rcItem.top, dis.rcItem.right - 8, dis.rcItem.bottom)
        user32.DrawTextW(dis.hDC, label, -1, ctypes.byref(rect), DT_SINGLELINE | DT_VCENTER | DT_LEFT)
        gdi32.SelectObject(dis.hDC, old_font)
        return True

    def _load_icon(self):
        flags = LR_LOADFROMFILE | LR_DEFAULTSIZE
        if os.path.exists(ICON_FILE):
            handle = user32.LoadImageW(None, ICON_FILE, IMAGE_ICON, 0, 0, flags)
            if handle:
                return handle
        return user32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))

    def _register_window(self):
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.lpszClassName = self._class_name
        wc.hInstance = kernel32.GetModuleHandleW(None)
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom and kernel32.GetLastError():
            raise ctypes.WinError()
        hwnd = user32.CreateWindowExW(
            0,
            self._class_name,
            "Muesli Tray",
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            wc.hInstance,
            None,
        )
        if not hwnd:
            raise ctypes.WinError()
        self._hwnd = hwnd

    def _create_tray_icon(self):
        self._icon = self._load_icon()
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = TRAY_UID
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = self._icon
        nid.szTip = self._tooltip_text()
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            raise ctypes.WinError()
        nid.uVersion = NOTIFYICON_VERSION_4
        shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))
        self._nid = nid
        self._last_tip = nid.szTip

    def _tooltip_text(self):
        state = load_runtime_state()
        status = str(state.get("status") or "Idle").strip() or "Idle"
        return f"Muesli: {status}"[:127]

    def _refresh_tray(self, force=False):
        state = load_runtime_state()
        if state.get("app_pid") and not _pid_alive(state.get("app_pid")):
            update_runtime_state(app_pid=None, recording=False, processing=False, status="Idle")
            state = load_runtime_state()
        if not self._nid:
            return
        tip = self._tooltip_text()
        if not force and tip == self._last_tip:
            return
        self._nid.uFlags = NIF_TIP | NIF_ICON | NIF_SHOWTIP
        self._nid.hIcon = self._icon
        self._nid.szTip = tip
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))
        self._last_tip = tip

    def _show_menu(self):
        state = load_runtime_state()
        paused = bool(state.get("processing_paused", False))
        can_pause = bool(
            state.get("app_pid") or state.get("recording") or state.get("processing") or paused
        )
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_OWNERDRAW, MENU_SHOW, MENU_SHOW)
        user32.AppendMenuW(menu, MF_OWNERDRAW, MENU_RECORD, MENU_RECORD)
        user32.AppendMenuW(
            menu,
            MF_OWNERDRAW | (0 if can_pause else MF_DISABLED),
            MENU_PAUSE,
            MENU_PAUSE,
        )
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_OWNERDRAW, MENU_EXIT, MENU_EXIT)
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self._hwnd)
        cmd = user32.TrackPopupMenu(
            menu,
            TPM_RIGHTBUTTON | TPM_RETURNCMD,
            pt.x,
            pt.y,
            0,
            self._hwnd,
            None,
        )
        user32.DestroyMenu(menu)
        if cmd:
            self._dispatch_menu(cmd)

    def _dispatch_menu(self, cmd):
        if cmd == MENU_SHOW:
            launch_app(record=False)
        elif cmd == MENU_RECORD:
            self._handle_hotkey()
        elif cmd == MENU_PAUSE:
            toggle_processing_pause()
            self._refresh_tray(force=True)
        elif cmd == MENU_EXIT:
            self._exit_everything()

    def _handle_hotkey(self):
        now = time.monotonic()
        if now - self._last_launch < DEBOUNCE_SECONDS:
            return
        self._last_launch = now
        try:
            launch_app(record=True)
        except Exception:
            pass

    def _exit_everything(self):
        state = load_runtime_state()
        pid = state.get("app_pid")
        send_to_existing("quit")
        deadline = time.monotonic() + 5.0
        while _pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        if _pid_alive(pid):
            _terminate_pid(pid)
        user32.DestroyWindow(self._hwnd)

    def _window_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_HOTKEY and wparam == HOTKEY_ID:
            self._handle_hotkey()
            return 0
        if msg == WM_MEASUREITEM and self._measure_menu_item(lparam):
            return 1
        if msg == WM_DRAWITEM and self._draw_menu_item(lparam):
            return 1
        if msg == WM_COMMAND:
            self._dispatch_menu(wparam & 0xFFFF)
            return 0
        if msg == WM_TIMER:
            self._refresh_tray()
            return 0
        if msg == WM_TRAYICON:
            event_code = _tray_event_code(lparam)
            if event_code in (WM_RBUTTONUP, WM_LBUTTONUP):
                self._show_menu()
                return 0
            if event_code == WM_LBUTTONDBLCLK:
                launch_app(record=False)
                return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self):
        self._register_window()
        if not user32.RegisterHotKey(self._hwnd, HOTKEY_ID, self._mods, self._vk):
            return 1
        write_pid()
        update_runtime_state(sidecar_pid=os.getpid())
        atexit.register(cleanup)
        self._create_tray_icon()
        user32.SetTimer(self._hwnd, 1, 1000, None)
        self._refresh_tray(force=True)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnregisterHotKey(self._hwnd, HOTKEY_ID)
        user32.KillTimer(self._hwnd, 1)
        if self._nid:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
        return 0


def main():
    hotkey = load_hotkey()
    mods, vk = parse_hotkey(hotkey)
    return TraySidecar(mods, vk).run()


if __name__ == "__main__":
    sys.exit(main())
