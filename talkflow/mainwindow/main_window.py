from __future__ import annotations

import logging
from pathlib import Path

import webview

from talkflow.mainwindow.api import Api

logger = logging.getLogger(__name__)

_HTML_PATH = Path(__file__).parent / "web" / "index.html"
_ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "icon.ico"
MIN_WIDTH = 720
MIN_HEIGHT = 480


def _apply_native_window_icon(window_title: str, icon_path: Path) -> bool:
    """pywebview's own `webview.start(icon=...)` sets the WinForms Form.Icon
    at window-construction time, which should be enough - but in practice the
    taskbar button for a frameless EdgeChromium window keeps showing python.exe's
    generic icon instead (observed live; not just a theoretical gap). Setting
    the HICON directly via WM_SETICON on the real HWND, after the window
    exists, is the fix that actually sticks. Returns True once it succeeds so
    the caller can stop retrying."""
    if not icon_path.exists():
        return False
    try:
        import ctypes
        import win32gui

        hwnd = win32gui.FindWindow(None, window_title)
        if not hwnd:
            return False

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        WM_SETICON = 0x0080
        ICON_SMALL, ICON_BIG = 0, 1
        user32 = ctypes.windll.user32

        for icon_type, size in ((ICON_SMALL, 16), (ICON_BIG, 32)):
            hicon = user32.LoadImageW(None, str(icon_path), IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if hicon:
                user32.SendMessageW(hwnd, WM_SETICON, icon_type, hicon)
        return True
    except Exception:
        logger.exception("Could not set the native taskbar icon for %r", window_title)
        return False


def _work_area() -> tuple[int, int, int, int]:
    """Primary monitor's work area (left, top, right, bottom), i.e. the
    screen minus the taskbar. Same helper/fallback as overlay_window.py's -
    pywebview's own Window.maximize() ignores the taskbar on a frameless
    window (a known behavior of frameless windows on Windows: they don't get
    the OS's normal maximize-to-work-area treatment), covering it instead of
    snapping to the space above it, so maximize/restore here is done by hand
    against this rect rather than delegating to pywebview."""
    try:
        import win32api
        info = win32api.GetMonitorInfo(win32api.MonitorFromPoint((0, 0)))
        return tuple(info["Work"])  # type: ignore[return-value]
    except Exception:
        return (0, 0, 1920, 1040)


class MainWindow:
    def __init__(self):
        self._window: webview.Window | None = None
        self.api = Api()
        self.api.main_window = self
        self._is_maximized = False
        self._pre_maximize_geometry: tuple[int, int, int, int] | None = None  # x, y, w, h
        self._native_icon_applied = False

    def create(self) -> webview.Window:
        self._window = webview.create_window(
            title="TalkFlow",
            url=_HTML_PATH.as_uri(),
            width=1040,
            height=680,
            min_size=(MIN_WIDTH, MIN_HEIGHT),
            js_api=self.api,
            hidden=True,
            # Frameless so the HTML title bar (collapse/minimize/maximize/
            # close) replaces the native Windows chrome - see index.html's
            # #titlebar. Closing it hides to tray instead of destroying it
            # (wired via the close button calling hide_main_window(), not
            # window.destroy()) so re-opening from the tray still works.
            frameless=True,
            easy_drag=True,
        )
        return self._window

    def show(self) -> None:
        if self._window:
            self._window.show()
            if not self._native_icon_applied:
                self._native_icon_applied = _apply_native_window_icon("TalkFlow", _ICON_PATH)

    def bring_to_front(self) -> None:
        """show(), plus un-minimize and raise - for a second launch of the
        app, where the window may be hidden, minimized or behind others."""
        self.show()
        try:
            import win32con
            import win32gui
            hwnd = win32gui.FindWindow(None, "TalkFlow")
            if hwnd:
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd)
        except Exception:
            logger.debug("Could not raise the main window", exc_info=True)

    def hide(self) -> None:
        if self._window:
            self._window.hide()

    def minimize(self) -> None:
        if self._window:
            self._window.minimize()

    def maximize(self) -> None:
        if not self._window:
            return
        self._pre_maximize_geometry = (self._window.x, self._window.y, self._window.width, self._window.height)
        left, top, right, bottom = _work_area()
        self._window.resize(right - left, bottom - top)
        self._window.move(left, top)
        self._is_maximized = True

    def restore(self) -> None:
        if not self._window:
            return
        if self._pre_maximize_geometry:
            x, y, w, h = self._pre_maximize_geometry
            self._window.resize(w, h)
            self._window.move(x, y)
        else:
            self._window.resize(1040, 680)
        self._is_maximized = False

    def resize(self, width: int, height: int) -> None:
        if self._window:
            self._window.resize(max(MIN_WIDTH, int(width)), max(MIN_HEIGHT, int(height)))
