"""Reads the currently-focused window's process name/title at insertion time,
via pywin32 (user-mode, no admin needed).
"""
from __future__ import annotations

from dataclasses import dataclass

import win32gui
import win32process


@dataclass
class TargetContext:
    process_name: str | None
    window_title: str | None
    hwnd: int | None


def get_focused_target() -> TargetContext:
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return TargetContext(None, None, None)
    title = win32gui.GetWindowText(hwnd)
    process_name = None
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process_name = _process_name_for_pid(pid)
    except Exception:
        process_name = None
    return TargetContext(process_name=process_name, window_title=title or None, hwnd=hwnd)


def _process_name_for_pid(pid: int) -> str | None:
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:
        try:
            import win32api
            import win32con
            handle = win32api.OpenProcess(
                win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            try:
                import win32process as wp
                path = wp.GetModuleFileNameEx(handle, 0)
                return path.rsplit("\\", 1)[-1]
            finally:
                win32api.CloseHandle(handle)
        except Exception:
            return None
