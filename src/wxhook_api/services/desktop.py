"""Windows helpers for showing the installed WeChat client without relaunching it."""

from __future__ import annotations

import csv
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess


def wechat_process_ids(executable: Path) -> set[int]:
    if os.name != "nt":
        return set()
    result = subprocess.run(
        ["tasklist.exe", "/FI", f"IMAGENAME eq {executable.name}", "/FO", "CSV", "/NH"],
        capture_output=True,
        timeout=5,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    processes = set()
    for row in csv.reader(result.stdout.decode(errors="replace").splitlines()):
        if len(row) > 1 and row[0].casefold() == executable.name.casefold():
            try:
                processes.add(int(row[1].replace(",", "")))
            except ValueError:
                continue
    return processes


def _user32():
    api = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    api.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    api.EnumWindows.restype = wintypes.BOOL
    api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    api.GetWindowThreadProcessId.restype = wintypes.DWORD
    api.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    api.GetWindowTextW.restype = ctypes.c_int
    api.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    api.GetWindowRect.restype = wintypes.BOOL
    for name in ("IsWindowVisible", "IsIconic", "SetForegroundWindow"):
        function = getattr(api, name)
        function.argtypes = [wintypes.HWND]
        function.restype = wintypes.BOOL
    api.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    api.ShowWindowAsync.restype = wintypes.BOOL
    api.GetForegroundWindow.argtypes = []
    api.GetForegroundWindow.restype = wintypes.HWND
    api.FlashWindowEx.argtypes = [ctypes.POINTER(_FlashInfo)]
    api.FlashWindowEx.restype = wintypes.BOOL
    return api


class _FlashInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("hwnd", wintypes.HWND),
        ("dwFlags", wintypes.DWORD),
        ("uCount", wintypes.UINT),
        ("dwTimeout", wintypes.DWORD),
    ]


def _find_window(api, process_ids: set[int]) -> int | None:
    candidates = []
    callback_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(handle, parameter):
        process_id = wintypes.DWORD()
        api.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
        if process_id.value not in process_ids:
            return True
        title = ctypes.create_unicode_buffer(512)
        api.GetWindowTextW(handle, title, len(title))
        rect = wintypes.RECT()
        if not title.value or not api.GetWindowRect(handle, ctypes.byref(rect)):
            return True
        width, height = rect.right - rect.left, rect.bottom - rect.top
        # Ignore helper/tray windows. Minimized windows can have tiny rectangles,
        # so retain them when Windows explicitly marks them as minimized.
        if (width < 160 or height < 120) and not api.IsIconic(handle):
            return True
        recognizable = any(name in title.value.casefold() for name in ("微信", "wechat", "weixin"))
        candidates.append(((recognizable, bool(api.IsWindowVisible(handle)), width * height), handle))
        return True

    api.EnumWindows(visit, 0)
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def show_wechat_window(executable: Path) -> str:
    """Return focused/shown/missing; never create or terminate a client process."""
    if os.name != "nt":
        return "unsupported"
    process_ids = wechat_process_ids(executable)
    if not process_ids:
        return "missing"
    api = _user32()
    handle = _find_window(api, process_ids)
    if handle is None:
        return "missing"
    # SW_RESTORE for minimized windows; SW_SHOW preserves a maximized window.
    api.ShowWindowAsync(handle, 9 if api.IsIconic(handle) else 5)
    if api.SetForegroundWindow(handle) or api.GetForegroundWindow() == handle:
        return "focused"
    # Windows may deny focus stealing. Show the window and flash its taskbar
    # button rather than changing system settings or forcing keyboard input.
    flash = _FlashInfo(ctypes.sizeof(_FlashInfo), handle, 3, 3, 0)
    api.FlashWindowEx(ctypes.byref(flash))
    return "shown"
