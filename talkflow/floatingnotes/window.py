"""Floating multi-tab Scratchpad popup (Wispr Flow's "Flow Bar" notes
window). A separate small pywebview window, docked near the dictation
overlay pill's bottom-right corner, hidden until toggled from the pill's
notes icon (see overlay_window.py) or the main window's Scratchpad tab.

Reuses the same `Api` instance as the main window - the scratchpad methods
on it are stateless (plain sqlite CRUD), so sharing one instance across both
windows is safe and avoids a second copy of the same wiring.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import webview

from talkflow.config.settings import Settings

logger = logging.getLogger(__name__)


def _patch_pywebview_move_bug() -> None:
    """pywebview 6.2.1's WinForms BrowserForm.move() (platforms/winforms.py)
    calls SetWindowPos with cx/cy=None when passing SWP_NOSIZE - ctypes
    rejects None for a plain int arg, even though SetWindowPos ignores cx/cy
    under that flag anyway (0 is always a safe substitute). This crashes on
    every frame of dragging a frameless window via easy_drag's JS (which
    calls window.move() continuously) - confirmed live: dragging the
    floating notes popup spammed this exact ctypes.ArgumentError and the
    window never actually moved. Patched once at import time; if a future
    pywebview version fixes this upstream, this just becomes a harmless
    duplicate of the correct behavior.
    """
    try:
        from ctypes import windll
        from webview.platforms.winforms import BrowserView

        def patched_move(self, x, y):
            SWP_NOSIZE, SWP_NOZORDER, SWP_SHOWWINDOW = 0x0001, 0x0004, 0x0040
            scale = self._scale
            x_phys = int(x * scale) if scale != 1 else int(x)
            y_phys = int(y * scale) if scale != 1 else int(y)
            windll.user32.SetWindowPos(
                self.Handle.ToInt32(), None, x_phys, y_phys, 0, 0,
                SWP_NOSIZE | SWP_NOZORDER | SWP_SHOWWINDOW,
            )

        BrowserView.BrowserForm.move = patched_move
    except Exception:
        logger.exception("Could not patch pywebview's move() bug - frameless window drag may crash")


_patch_pywebview_move_bug()

# pywebview's easy_drag, by default, drags the window from a mousedown
# ANYWHERE in the page (DRAG_REGION_DIRECT_TARGET_ONLY defaults to False) -
# that would hijack every click on the popup's own buttons. Scoping it to
# elements carrying the '.pywebview-drag-region' class (notes_popup.html's
# header label/spacer, not the icon buttons) is pywebview's documented way
# to limit dragging to an explicit handle.
webview.settings['DRAG_REGION_DIRECT_TARGET_ONLY'] = True

_HTML_PATH = Path(__file__).parent / "web" / "notes_popup.html"
WIDTH = 340
HEIGHT = 460
MIN_WIDTH = 260
MIN_HEIGHT = 300
MARGIN = 10


def _work_area() -> tuple[int, int, int, int]:
    try:
        import win32api
        info = win32api.GetMonitorInfo(win32api.MonitorFromPoint((0, 0)))
        return tuple(info["Work"])  # type: ignore[return-value]
    except Exception:
        return (0, 0, 1920, 1040)


class FloatingNotesWindow:
    def __init__(self, api):
        self._api = api
        self._window: webview.Window | None = None
        self._visible = False
        self._save_timer: threading.Timer | None = None

    def create(self) -> webview.Window:
        s = Settings.load()
        width = s.scratchpad_popup_w or WIDTH
        height = s.scratchpad_popup_h or HEIGHT
        if s.scratchpad_popup_x is not None and s.scratchpad_popup_y is not None:
            x, y = s.scratchpad_popup_x, s.scratchpad_popup_y
        else:
            left, _top, right, bottom = _work_area()
            x = right - width - MARGIN
            y = bottom - height - 70  # sit just above the overlay pill's corner

        self._window = webview.create_window(
            title="TalkFlow Notes",
            url=_HTML_PATH.as_uri(),
            js_api=self._api,
            width=width,
            height=height,
            x=x,
            y=y,
            min_size=(MIN_WIDTH, MIN_HEIGHT),
            frameless=True,
            easy_drag=True,
            on_top=True,
            resizable=True,
            hidden=True,
            shadow=True,
        )
        self._window.events.moved += self._on_moved
        self._window.events.resized += self._on_resized
        return self._window

    def _on_moved(self, x: int, y: int) -> None:
        self._save_geometry(x=x, y=y)

    def _on_resized(self, width: int, height: int) -> None:
        self._save_geometry(w=width, h=height)

    def _save_geometry(self, **kwargs) -> None:
        # Debounced on a timer: resized/moved fire repeatedly while the user
        # is actively dragging, and writing settings.json on every single
        # event would mean dozens of disk writes per drag.
        def _write():
            try:
                s = Settings.load()
                if "x" in kwargs:
                    s.scratchpad_popup_x, s.scratchpad_popup_y = kwargs["x"], kwargs["y"]
                if "w" in kwargs:
                    s.scratchpad_popup_w, s.scratchpad_popup_h = kwargs["w"], kwargs["h"]
                s.save()
            except Exception:
                logger.exception("Failed to persist floating notes geometry")

        if self._save_timer:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(0.6, _write)
        self._save_timer.daemon = True
        self._save_timer.start()

    def toggle(self) -> None:
        if self._visible:
            self.hide()
        else:
            self.show()

    def show(self) -> None:
        if self._window:
            self._window.show()
            self._visible = True

    def hide(self) -> None:
        if self._window:
            self._window.hide()
            self._visible = False

    def resize(self, width: int, height: int) -> None:
        # pywebview's frameless windows have no native OS resize grips (no
        # border to drag), so the popup's own corner handle (notes_popup.html)
        # drives this directly - fixed to the top-left corner so dragging the
        # bottom-right corner grows/shrinks the window the way you'd expect.
        if self._window:
            self._window.resize(max(MIN_WIDTH, int(width)), max(MIN_HEIGHT, int(height)))
