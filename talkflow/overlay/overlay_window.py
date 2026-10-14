"""Floating dictation pill, modelled on Wispr Flow's overlay.

Native Win32 layered window with PER-PIXEL ALPHA (`UpdateLayeredWindow`),
drawn with PIL at 4x supersampling. This replaces the earlier pywebview
version: WebView2 can't do real transparency here (`transparent=True` renders
a gray block, and region-clipping gives hard, un-antialiased edges plus DWM
shadow artifacts). With per-pixel alpha, fully transparent pixels are also
click-through, rounded corners are smooth, and the idle pill can be genuinely
translucent.

Modes: idle (tiny translucent pill) / hover (tooltip + mic pill) / recording
(X, waveform, check) / busy (pulsing dots) / message (Done/Error/Cancelled).

The window is WS_EX_NOACTIVATE|WS_EX_TOOLWINDOW|TOPMOST so clicking it never
steals focus from the app being dictated into. It runs its own message loop on
a private thread, so it is independent of pywebview's main-thread loop.
"""
from __future__ import annotations

import ctypes
import logging
import math
import threading
import time
from ctypes import wintypes
from typing import Callable

import win32api
import win32con
import win32gui
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

WINDOW_CLASS = "TalkFlowOverlayWnd"
BOTTOM_MARGIN = 10
SS = 4  # supersampling factor

_BUSY_STATES = {"arming", "stopping", "transcribing", "formatting", "inserting"}
_MESSAGE_STATES = {"complete": "Done", "error": "Error", "cancelled": "Cancelled",
                    "nospeech": "No speech heard"}

# Logical-px layouts: canvas size, pad_bottom (transparent strip under the
# pill that still counts toward the window, used as an invisible hover pad).
_LAYOUT = {
    "idle": {"canvas": (64, 26), "pad_bottom": 8},
    "hover": {"canvas": (184, 70), "pad_bottom": 0},
    "recording": {"canvas": (136, 34), "pad_bottom": 0},
    "busy": {"canvas": (76, 30), "pad_bottom": 0},
    "message": {"canvas": (132, 30), "pad_bottom": 0},
}

_FILL = (13, 13, 16)
WM_REFRESH = win32con.WM_USER + 1
WM_MOUSELEAVE = 0x02A3
TIMER_ID = 1


def _font(size: float, bold: bool = False):
    for name in (("segoeuib.ttf", "seguisb.ttf") if bold else ("segoeui.ttf",)):
        try:
            return ImageFont.truetype(name, max(1, round(size)))
        except OSError:
            continue
    return ImageFont.load_default()


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


class _TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("hwndTrack", ctypes.c_void_p), ("dwHoverTime", wintypes.DWORD)]


_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32
_gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
_gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
_gdi32.CreateDIBSection.restype = ctypes.c_void_p
_gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
                                    ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wintypes.DWORD]
_gdi32.SelectObject.restype = ctypes.c_void_p
_gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
_gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
_user32.GetDC.restype = ctypes.c_void_p
_user32.GetDC.argtypes = [ctypes.c_void_p]
_user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_user32.UpdateLayeredWindow.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
    ctypes.c_void_p, ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
    ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD,
]
_user32.TrackMouseEvent.argtypes = [ctypes.POINTER(_TRACKMOUSEEVENT)]
_user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_int] * 4 + [wintypes.UINT]
_user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]


def _work_area() -> tuple[int, int, int, int]:
    try:
        info = win32api.GetMonitorInfo(win32api.MonitorFromPoint((0, 0)))
        return tuple(info["Work"])  # type: ignore[return-value]
    except Exception:
        return (0, 0, 1920, 1040)


class OverlayWindow:
    def __init__(self):
        self._lock = threading.RLock()
        self._hover = False
        self._state = "idle"
        self._hint = ""
        self._hwnd = 0
        self._scale = 1.0
        self._mode = "idle"
        self._buttons: dict[str, tuple[float, float, float, float]] = {}  # logical px
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._tracking = False
        self.on_cancel: Callable[[], None] | None = None
        self.on_confirm: Callable[[], None] | None = None
        self.on_notes_click: Callable[[], None] | None = None
        self.on_mic_click: Callable[[], None] | None = None
        self.on_record_click: Callable[[], None] | None = None
        self._flow_bar_enabled = False
        self._meeting_active = False

    # ---- lifecycle -------------------------------------------------------

    def create(self) -> None:
        self._thread = threading.Thread(target=self._run, name="overlay", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def _run(self) -> None:
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = WINDOW_CLASS
        wc.lpfnWndProc = self._wndproc
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        try:
            win32gui.RegisterClass(wc)
        except win32gui.error:
            pass  # already registered

        # An unowned WS_EX_TOOLWINDOW popup still shows up on the Windows 11
        # taskbar in practice (a known shell quirk - the style alone isn't
        # honored without an owner). Giving it a hidden owner window makes
        # Windows actually exclude it, which also sidesteps needing to set an
        # icon on it at all (it falls back to pythonw.exe's own icon, a
        # generic "python file" glyph, when none is set - not worth fixing
        # since the window shouldn't be in the taskbar in the first place).
        owner_class = WINDOW_CLASS + "Owner"
        owner_wc = win32gui.WNDCLASS()
        owner_wc.hInstance = wc.hInstance
        owner_wc.lpszClassName = owner_class
        owner_wc.lpfnWndProc = win32gui.DefWindowProc
        try:
            win32gui.RegisterClass(owner_wc)
        except win32gui.error:
            pass
        owner_hwnd = win32gui.CreateWindowEx(
            0, owner_class, "TalkFlowOverlayOwner", win32con.WS_POPUP,
            0, 0, 0, 0, 0, 0, wc.hInstance, None,
        )

        ex = (win32con.WS_EX_LAYERED | win32con.WS_EX_TOPMOST |
              win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE)
        self._hwnd = win32gui.CreateWindowEx(
            ex, WINDOW_CLASS, "TalkFlowOverlay", win32con.WS_POPUP,
            0, 0, 10, 10, owner_hwnd, 0, wc.hInstance, None,
        )
        try:
            self._scale = _user32.GetDpiForWindow(self._hwnd) / 96.0
        except Exception:
            self._scale = 1.0
        _user32.SetTimer(ctypes.c_void_p(self._hwnd), TIMER_ID, 33, None)
        self._render()
        win32gui.ShowWindow(self._hwnd, win32con.SW_SHOWNOACTIVATE)
        self._ready.set()
        win32gui.PumpMessages()

    # ---- window procedure ------------------------------------------------

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == win32con.WM_MOUSEACTIVATE:
                return 3  # MA_NOACTIVATE
            if msg == win32con.WM_MOUSEMOVE:
                if not self._tracking:
                    tme = _TRACKMOUSEEVENT(ctypes.sizeof(_TRACKMOUSEEVENT), 2, hwnd, 0)  # TME_LEAVE
                    _user32.TrackMouseEvent(ctypes.byref(tme))
                    self._tracking = True
                if not self._hover:
                    self._hover = True
                    self._render()
                return 0
            if msg == WM_MOUSELEAVE:
                self._tracking = False
                if self._hover:
                    self._hover = False
                    self._render()
                return 0
            if msg == win32con.WM_LBUTTONUP:
                x = win32api.LOWORD(lparam) / self._scale
                y = win32api.HIWORD(lparam) / self._scale
                for name, (x0, y0, x1, y1) in self._buttons.items():
                    if x0 <= x <= x1 and y0 <= y <= y1:
                        cb = {"cancel": self.on_cancel, "confirm": self.on_confirm,
                              "notes": self.on_notes_click, "mic": self.on_mic_click,
                              "record": self.on_record_click}.get(name)
                        if cb:
                            threading.Thread(target=cb, daemon=True).start()
                        break
                return 0
            if msg == WM_REFRESH:
                self._render()
                return 0
            if msg == win32con.WM_TIMER:
                if self._mode in ("recording", "busy"):
                    self._render()
                return 0
            if msg == win32con.WM_DESTROY:
                win32gui.PostQuitMessage(0)
                return 0
        except Exception:
            logger.exception("overlay wndproc error")
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    # ---- rendering -------------------------------------------------------

    def _current_mode(self) -> str:
        if self._state == "recording":
            return "recording"
        if self._state in _BUSY_STATES:
            return "busy"
        if self._state in _MESSAGE_STATES:
            return "message"
        return "hover" if self._hover else "idle"

    def _render(self) -> None:
        with self._lock:
            if not self._hwnd:
                return
            mode = self._current_mode()
            self._mode = mode
            lay = _LAYOUT[mode]
            cw, ch = lay["canvas"]
            s = self._scale
            pw, ph = round(cw * s), round(ch * s)
            img = self._draw(mode, cw, ch, s)
            img = img.resize((pw, ph), Image.LANCZOS)
            self._present(img, pw, ph, lay["pad_bottom"], s)

    def _present(self, img: Image.Image, pw: int, ph: int, pad_bottom: int, s: float) -> None:
        pm = img.convert("RGBa")  # premultiplied alpha, as UpdateLayeredWindow wants
        r, g, b, a = Image.frombytes("RGBA", pm.size, pm.tobytes()).split()
        data = Image.merge("RGBA", (b, g, r, a)).tobytes()  # BGRA

        left, _top, right, bottom = _work_area()
        x = (left + right) // 2 - pw // 2
        y = bottom - round(BOTTOM_MARGIN * s) - (ph - round(pad_bottom * s))

        bmi = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), pw, -ph, 1, 32, 0, 0, 0, 0, 0, 0)
        screen_dc = _user32.GetDC(None)
        mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
        bits = ctypes.c_void_p()
        hbmp = _gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        old = _gdi32.SelectObject(mem_dc, hbmp)
        ctypes.memmove(bits, data, len(data))

        dst = wintypes.POINT(x, y)
        size = wintypes.SIZE(pw, ph)
        src = wintypes.POINT(0, 0)
        blend = _BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, AC_SRC_ALPHA
        _user32.UpdateLayeredWindow(self._hwnd, screen_dc, ctypes.byref(dst), ctypes.byref(size),
                                    mem_dc, ctypes.byref(src), 0, ctypes.byref(blend), 2)  # ULW_ALPHA
        # Re-assert topmost without moving/resizing/activating.
        _user32.SetWindowPos(self._hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        _gdi32.SelectObject(mem_dc, old)
        _gdi32.DeleteObject(hbmp)
        _gdi32.DeleteDC(mem_dc)
        _user32.ReleaseDC(None, screen_dc)

    def _draw(self, mode: str, cw: int, ch: int, s: float) -> Image.Image:
        u = s * SS  # logical px -> supersampled px
        img = Image.new("RGBA", (round(cw * u), round(ch * u)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        self._buttons = {}

        def pill(x0, y0, x1, y1, fill, outline):
            d.rounded_rectangle([x0 * u, y0 * u, x1 * u - 1, y1 * u - 1], radius=(y1 - y0) * u / 2,
                                fill=fill, outline=outline, width=max(1, round(u)))

        def circle(cx, cy, r, **kw):
            d.ellipse([(cx - r) * u, (cy - r) * u, (cx + r) * u, (cy + r) * u], **kw)

        def mic(cx, cy, color, k=1.0):
            w = 3.6 * k
            d.rounded_rectangle([(cx - w) * u, (cy - 7 * k) * u, (cx + w) * u, (cy + 2 * k) * u],
                                radius=w * u, fill=color)
            d.arc([(cx - 6.5 * k) * u, (cy - 5 * k) * u, (cx + 6.5 * k) * u, (cy + 6 * k) * u],
                  0, 180, fill=color, width=max(1, round(1.5 * k * u)))
            d.line([cx * u, (cy + 6 * k) * u, cx * u, (cy + 9 * k) * u], fill=color,
                   width=max(1, round(1.5 * k * u)))

        def notes_icon(cx, cy, color):
            w, h = 7, 8.5
            d.rounded_rectangle([(cx - w) * u, (cy - h) * u, (cx + w) * u, (cy + h) * u],
                                radius=1.6 * u, outline=color, width=max(1, round(1.2 * u)))
            for i, frac in enumerate((0.35, 0.0, 0.35)):
                ly = cy + (i - 1) * 3.4
                lw = w * (1 - abs(frac))
                d.line([(cx - lw * 0.55) * u, ly * u, (cx + lw * 0.55) * u, ly * u],
                       fill=color, width=max(1, round(1.0 * u)))

        solid = _FILL + (255,)
        edge = (255, 255, 255, 70)
        white = (255, 255, 255, 255)
        now = time.time()

        if mode == "idle":
            bottom = ch - _LAYOUT["idle"]["pad_bottom"]
            # Near-invisible hover pad (alpha 1) so the tiny pill is easy to hit.
            d.rectangle([0, 0, cw * u, ch * u], fill=(0, 0, 0, 1))
            x0 = (cw - 44) / 2
            pill(x0, bottom - 10, x0 + 44, bottom, _FILL + (120,), (255, 255, 255, 95))
        elif mode == "hover":
            # A layered window hit-tests by alpha: a fully transparent pixel
            # lets clicks/hover pass straight through to whatever's behind
            # it. The tooltip bar, mic pill and notes icon are three separate
            # opaque shapes with transparent gaps between them, so without
            # this the cursor crossing a gap looks like "left the window" to
            # TrackMouseEvent - hover drops, the pill shrinks back to idle,
            # and a click aimed at a gap (or near one) never reaches us.
            # Same trick "idle" mode already uses: a background at alpha=1 is
            # visually invisible but still catches mouse input everywhere.
            d.rectangle([0, 0, cw * u, ch * u], fill=(0, 0, 0, 1))
            tip_font, hk_font = _font(12 * u, bold=True), _font(12 * u)
            pill(0, 0, cw, 28, solid, edge)
            t1, t2 = "Dictate", ("  " + self._hint) if self._hint else ""
            w1, w2 = tip_font.getlength(t1), hk_font.getlength(t2) if t2 else 0
            tx, ty = (cw * u - (w1 + w2)) / 2, 14 * u
            d.text((tx, ty), t1, font=tip_font, fill=white, anchor="lm")
            if t2:
                d.text((tx + w1, ty), t2, font=hk_font, fill=(255, 255, 255, 150), anchor="lm")
            px0 = (cw - 76) / 2
            pill(px0, 38, px0 + 76, 70, solid, edge)
            mic(px0 + 24, 54, white, 0.9)
            circle(px0 + 52, 54, 10, outline=(255, 255, 255, 190), width=round(2 * u))
            # Record-meeting dot: red while a meeting recording is running.
            circle(px0 + 52, 54, 4, fill=(239, 68, 68, 255) if self._meeting_active else white)
            self._buttons["mic"] = (px0, 38, px0 + 38, 70)
            self._buttons["record"] = (px0 + 38, 38, px0 + 76, 70)
            # Notes icon sits in the existing right margin next to the mic
            # pill - no canvas resize needed (cw already has room), which
            # keeps the window's real hit-test bounds identical to what's
            # drawn (a previous version resized the canvas for this and the
            # icon ended up not reliably clickable).
            if self._flow_bar_enabled:
                nx0 = px0 + 76 + 6
                if nx0 + 30 <= cw - 4:
                    pill(nx0, 40, nx0 + 30, 68, solid, edge)
                    notes_icon(nx0 + 15, 54, white)
                    self._buttons["notes"] = (nx0, 40, nx0 + 30, 68)
        elif mode == "recording":
            pill(0, 0, cw, ch, solid, edge)
            cy = ch / 2
            circle(19, cy, 11, fill=(255, 255, 255, 36))
            for sgn in (-1, 1):  # X
                d.line([(19 - 3.2) * u, (cy - 3.2 * sgn) * u, (19 + 3.2) * u, (cy + 3.2 * sgn) * u],
                       fill=white, width=round(1.7 * u))
            cx = cw - 19
            circle(cx, cy, 11, fill=white)
            d.line([(cx - 4) * u, cy * u, (cx - 1) * u, (cy + 3) * u, (cx + 4.2) * u, (cy - 3.2) * u],
                   fill=solid, width=round(1.9 * u), joint="curve")
            n, x_start, gap = 13, 38, 4.6
            for i in range(n):
                amp = 0.5 + 0.5 * math.sin(now * 7 + i * 0.7)
                amp *= 0.55 + 0.45 * math.sin(now * 3.1 + i * 1.9) ** 2
                h = 2.5 + amp * 15
                bx = x_start + i * gap
                d.rounded_rectangle([bx * u, (cy - h / 2) * u, (bx + 2.4) * u, (cy + h / 2) * u],
                                    radius=1.2 * u, fill=(255, 255, 255, 235))
            self._buttons = {"cancel": (8, 0, 30, ch), "confirm": (cw - 30, 0, cw - 8, ch)}
        elif mode == "busy":
            pill(0, 0, cw, ch, solid, edge)
            for i in range(4):
                a = 0.25 + 0.75 * (0.5 + 0.5 * math.sin(now * 6 - i * 0.9))
                circle(cw / 2 + (i - 1.5) * 11, ch / 2, 2.6, fill=(255, 255, 255, round(255 * a)))
        else:  # message
            pill(0, 0, cw, ch, solid, edge)
            d.text((cw * u / 2, ch * u / 2), _MESSAGE_STATES.get(self._state, ""),
                   font=_font(12.5 * u, bold=True), fill=white, anchor="mm")
        return img

    # ---- public API used by app.py --------------------------------------

    def _poke(self) -> None:
        if self._hwnd:
            win32gui.PostMessage(self._hwnd, WM_REFRESH, 0, 0)

    def show(self) -> None:
        if self._hwnd:
            win32gui.ShowWindow(self._hwnd, win32con.SW_SHOWNOACTIVATE)

    def hide(self) -> None:
        if self._hwnd:
            win32gui.ShowWindow(self._hwnd, win32con.SW_HIDE)

    def set_state(self, state: str) -> None:
        self._state = state
        self._poke()

    def set_flow_bar_enabled(self, enabled: bool) -> None:
        self._flow_bar_enabled = enabled
        self._poke()

    def set_meeting_active(self, active: bool) -> None:
        self._meeting_active = active
        self._poke()

    def set_hotkey_hint(self, text: str) -> None:
        self._hint = text
        self._poke()
