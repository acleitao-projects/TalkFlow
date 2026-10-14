"""One TalkFlow per user session. A second launch (Start Menu, pinned taskbar
button, double-clicked TalkFlow.exe) must not start another tray app with its
own hotkey hooks and ASR model - it asks the running instance to show its
main window, then exits.

A named mutex marks "already running"; a named auto-reset event is the
"show yourself" signal the running instance waits on.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

import win32api
import win32event
import winerror

logger = logging.getLogger(__name__)

_MUTEX_NAME = "Local\\TalkFlow.SingleInstance"
_SHOW_EVENT_NAME = "Local\\TalkFlow.ShowMainWindow"

_mutex_handle = None  # held for the whole process lifetime
_show_event = None


def acquire() -> bool:
    """True if this is the first instance. Otherwise signals the running one
    to show its main window and returns False (the caller should exit)."""
    global _mutex_handle, _show_event
    _mutex_handle = win32event.CreateMutex(None, False, _MUTEX_NAME)
    if win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS:
        # Created right away, not when the listener starts, so a second
        # launch during this instance's (slow) startup still finds it and
        # its request is kept until the listener picks it up.
        _show_event = win32event.CreateEvent(None, False, False, _SHOW_EVENT_NAME)
        return True

    # This process was just launched by the user, so it may hand foreground
    # rights over; otherwise Windows only flashes the existing window's
    # taskbar button instead of bringing it to the front.
    try:
        import ctypes
        ASFW_ANY = -1
        ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY)
    except Exception:
        pass
    try:
        event = win32event.OpenEvent(win32event.EVENT_MODIFY_STATE, False, _SHOW_EVENT_NAME)
        win32event.SetEvent(event)
    except Exception:
        logger.exception("TalkFlow is already running but could not be signalled")
    return False


def listen_for_show_requests(on_show: Callable[[], None]) -> None:
    """Call on_show() every time a second launch asks to be shown. Only
    valid after acquire() returned True."""
    event = _show_event

    def _loop() -> None:
        while True:
            win32event.WaitForSingleObject(event, win32event.INFINITE)
            try:
                on_show()
            except Exception:
                logger.exception("Showing the main window for a second launch failed")

    threading.Thread(target=_loop, name="single-instance", daemon=True).start()
