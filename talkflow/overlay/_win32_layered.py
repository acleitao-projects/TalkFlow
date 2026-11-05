"""Shared raw Win32 layered-window plumbing: the ctypes structures and
UpdateLayeredWindow present pipeline, and SetWindowDisplayAffinity capture-
exclusion - used by both the dictation pill (overlay_window.py) and the
Live Assist transcript panel (live_assist_overlay_window.py), factored out
so this ~80 lines of ctypes boilerplate isn't duplicated verbatim.
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from PIL import Image

logger = logging.getLogger(__name__)


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("hwndTrack", ctypes.c_void_p), ("dwHoverTime", wintypes.DWORD)]


user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateDIBSection.restype = ctypes.c_void_p
gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wintypes.DWORD]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
user32.GetDC.restype = ctypes.c_void_p
user32.GetDC.argtypes = [ctypes.c_void_p]
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.UpdateLayeredWindow.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
    ctypes.c_void_p, ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
    ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD,
]
user32.TrackMouseEvent.argtypes = [ctypes.POINTER(TRACKMOUSEEVENT)]
user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_int] * 4 + [wintypes.UINT]
user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL

WDA_MONITOR = 0x00000001
WDA_EXCLUDEFROMCAPTURE = 0x00000011  # Windows 10 2004+ (build 19041+)


def exclude_hwnd_from_capture(hwnd: int) -> str:
    """Tries WDA_EXCLUDEFROMCAPTURE (true exclusion, Win10 2004+); falls
    back to WDA_MONITOR (blacks the window out in capture instead of
    excluding it) on older builds. Returns "excluded" | "blacked_out" |
    "unsupported" so the actual result can be recorded per machine instead
    of assumed."""
    if not hwnd:
        return "unsupported"
    if user32.SetWindowDisplayAffinity(ctypes.c_void_p(hwnd), WDA_EXCLUDEFROMCAPTURE):
        return "excluded"
    logger.warning("WDA_EXCLUDEFROMCAPTURE unavailable; falling back to WDA_MONITOR")
    if user32.SetWindowDisplayAffinity(ctypes.c_void_p(hwnd), WDA_MONITOR):
        return "blacked_out"
    logger.warning("SetWindowDisplayAffinity unsupported; window will be visible in captures")
    return "unsupported"


def present_layered(hwnd: int, img: Image.Image, pw: int, ph: int, x: int, y: int) -> None:
    """Pushes `img` (already sized to pw x ph physical px) onto `hwnd` via
    UpdateLayeredWindow at screen position (x, y), and re-asserts topmost
    without moving/resizing/activating."""
    pm = img.convert("RGBa")  # premultiplied alpha, as UpdateLayeredWindow wants
    r, g, b, a = Image.frombytes("RGBA", pm.size, pm.tobytes()).split()
    data = Image.merge("RGBA", (b, g, r, a)).tobytes()  # BGRA

    bmi = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), pw, -ph, 1, 32, 0, 0, 0, 0, 0, 0)
    screen_dc = user32.GetDC(None)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(mem_dc, hbmp)
    ctypes.memmove(bits, data, len(data))

    dst = wintypes.POINT(x, y)
    size = wintypes.SIZE(pw, ph)
    src = wintypes.POINT(0, 0)
    blend = _BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, AC_SRC_ALPHA
    user32.UpdateLayeredWindow(hwnd, screen_dc, ctypes.byref(dst), ctypes.byref(size),
                                mem_dc, ctypes.byref(src), 0, ctypes.byref(blend), 2)  # ULW_ALPHA
    # Re-assert topmost without moving/resizing/activating.
    user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
    gdi32.SelectObject(mem_dc, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(None, screen_dc)
