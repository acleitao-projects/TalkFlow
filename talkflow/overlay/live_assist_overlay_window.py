"""LiveAssistOverlayWindow - LA-06: a second native overlay, separate from
the dictation pill, showing a Live Assist session's two transcript feeds
("You" / mic, "Them" / system-output) live as they're recognized, plus
session state. Built the same way as the pill (talkflow/overlay/
overlay_window.py): raw Win32 layered window, PIL-rendered, own thread +
message pump, excluded from screen capture - but kept as a separate class
rather than folded into the pill's modes, since this window needs to stay
open for a whole session instead of auto-reverting like the pill's transient
idle/hover/recording states. Capture-exclusion follows the same Settings
toggle as the main window (exclude_main_window_from_capture) rather than
being unconditionally hidden - previously that was hardcoded on with no
way to turn it off, which made this panel (and anything behind it in a
screenshot) invisible in any screen recording/share/capture tool, with no
way to actually demo or record a Live Assist session.

Resizable via a bottom-right corner grip, and movable by dragging its
header - freely, to any monitor (Windows' virtual-screen coordinate space
spans all monitors, including negative coordinates for ones placed left of
or above the primary). Once dragged, the position is "manual" and persists
across restarts instead of auto-anchoring. Both the remembered size and a
manual position are restored on startup.

Two visual themes, both following the app's own light/dark setting
(Settings.ui_theme) rather than carrying a separate theme of their own:
plain (the original flowing-text transcript) and a Perssua/iMessage-style
chat-bubble view (Settings.live_assist_bubble_theme) - see _draw_bubble().
Panel opacity is user-configurable (Settings.live_assist_panel_opacity).
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from itertools import islice
from typing import Callable

import win32api
import win32con
import win32gui
from PIL import Image, ImageDraw

from talkflow.config.settings import Settings
from talkflow.features.liveassist.session import LiveAssistState
from talkflow.overlay._win32_layered import exclude_hwnd_from_capture, present_layered
from talkflow.overlay._win32_layered import user32 as _user32
from talkflow.overlay.overlay_window import _font

logger = logging.getLogger(__name__)

WINDOW_CLASS = "TalkFlowLiveAssistWnd"
WM_REFRESH = win32con.WM_USER + 1
TIMER_ID = 1
SS = 4  # supersampling factor, same as the pill

MIN_W, MIN_H = 260, 160
_DEFAULT_W, _DEFAULT_H = 340, 240
_GRIP_SIZE = 12
_MARGIN = 10
_HEADER_H = 28
_BTN_SIZE = 16  # header transport-button click target, square, unscaled px
_BTN_GAP = 4
_LABEL_H = 18
_LINE_H = 18
_BUBBLE_PAD_X = 10
_BUBBLE_PAD_Y = 6
_BUBBLE_GAP = 6
_BUBBLE_RADIUS = 11
_BUBBLE_MAX_WIDTH_FRAC = 0.78
_MIN_VISIBLE_PX = 40  # how much of the panel must stay reachable on-screen
# How long a feed's interim (not-yet-final) text stays shown after its last
# update before being treated as stale/gone. Needed because the realtime
# ASR adapter emits one more interim event carrying the SAME text right
# after committing a final (confirmed live: "...produtos" finalizes, then
# an interim ' Tudo' for the next word arrives, and once that speaker goes
# quiet nothing ever overwrites or clears it again) - without this, that
# leftover kept getting redrawn forever: a lingering dim trailing line in
# the plain theme, and - more visibly - a whole new phantom one/two-word
# bubble in bubble theme, since each render re-appends whatever is
# currently in interim_text with no concept of "this went stale".
_INTERIM_STALE_S = 2.0
_REDRAW_INTERVAL = 0.1  # ~10 redraws/sec cap, coalesces rapid interim updates

_STATE_LABEL = {
    LiveAssistState.ARMING: ("Starting…", (250, 204, 21)),
    LiveAssistState.ACTIVE: ("Live", (74, 222, 128)),
    LiveAssistState.PAUSED: ("Paused", (250, 204, 21)),
    LiveAssistState.STOPPING: ("Ending…", (250, 204, 21)),
    LiveAssistState.ENDED: ("Ended", (148, 163, 184)),
    LiveAssistState.ERROR: ("Error", (248, 113, 113)),
}
_SECTIONS = (("me", "YOU", (167, 139, 250)), ("they", "THEM", (96, 165, 250)))
_ME_ACCENT = (167, 139, 250)
_SUGGESTION_ACCENT = (250, 204, 21)


def _palette(ui_theme: str, opacity_pct: int) -> dict:
    """Pure color-lookup, kept outside the win32 class so it's unit-testable
    without a real display."""
    opacity_pct = max(15, min(100, opacity_pct))
    alpha = round(opacity_pct / 100 * 255)
    if ui_theme == "light":
        return {
            "panel_fill": (247, 247, 250, alpha),
            "panel_edge": (0, 0, 0, 40),
            "text": (24, 24, 27, 255),
            "text_dim": (24, 24, 27, 150),
            "bubble_them_fill": (228, 228, 233, 255),
            "bubble_them_text": (24, 24, 27, 255),
        }
    return {
        "panel_fill": (13, 13, 16, alpha),
        "panel_edge": (255, 255, 255, 60),
        "text": (255, 255, 255, 255),
        "text_dim": (255, 255, 255, 140),
        "bubble_them_fill": (40, 40, 46, 255),
        "bubble_them_text": (255, 255, 255, 255),
    }


def _clamp_position(x: int, y: int, pw: int, ph: int,
                     vx: int, vy: int, vw: int, vh: int,
                     min_visible: int = _MIN_VISIBLE_PX) -> tuple[int, int]:
    """Keeps at least `min_visible` px of a pw x ph window reachable inside
    the combined virtual-screen rect (vx, vy, vw, vh) - which may have
    negative vx/vy for a monitor placed left of/above the primary - without
    forcing the window fully onto any single monitor. Lets it sit on any
    monitor, including ones the window fully overlaps."""
    x = max(vx - pw + min_visible, min(x, vx + vw - min_visible))
    y = max(vy - ph + min_visible, min(y, vy + vh - min_visible))
    return x, y


def _resize_caps(x: int, y: int, vx: int, vy: int, vw: int, vh: int) -> tuple[int, int]:
    """Max (width, height) a window anchored at top-left (x, y) can grow to
    without its right/bottom edge leaving the combined virtual screen."""
    max_w = max(MIN_W, min(round(vw * 0.6), vx + vw - x))
    max_h = max(MIN_H, min(round(vh * 0.85), vy + vh - y))
    return max_w, max_h


def _draw_pause_icon(d: ImageDraw.ImageDraw, rect: tuple[float, float, float, float], u: float, color) -> None:
    x0, y0, x1, y1 = (v * u for v in rect)
    w = x1 - x0
    bar_w = w * 0.22
    gap = w * 0.2
    d.rectangle([x0 + w / 2 - gap / 2 - bar_w, y0, x0 + w / 2 - gap / 2, y1], fill=color)
    d.rectangle([x0 + w / 2 + gap / 2, y0, x0 + w / 2 + gap / 2 + bar_w, y1], fill=color)


def _draw_play_icon(d: ImageDraw.ImageDraw, rect: tuple[float, float, float, float], u: float, color) -> None:
    x0, y0, x1, y1 = (v * u for v in rect)
    d.polygon([(x0 + (x1 - x0) * 0.2, y0), (x0 + (x1 - x0) * 0.2, y1), (x1, (y0 + y1) / 2)], fill=color)


def _draw_stop_icon(d: ImageDraw.ImageDraw, rect: tuple[float, float, float, float], u: float, color) -> None:
    x0, y0, x1, y1 = (v * u for v in rect)
    pad = (x1 - x0) * 0.2
    d.rectangle([x0 + pad, y0 + pad, x1 - pad, y1 - pad], fill=color)


def _wrap(text: str, max_width: float, font) -> list[str]:
    """Greedy word-wrap. `font`/`max_width` are both in the SAME reference
    scale (plain, unscaled by DPI/supersampling) - wrap decisions are then
    scale-invariant, so the actual draw-time font (sized for supersampling)
    can render these exact pre-wrapped lines without re-measuring."""
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if font.getlength(candidate) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


class _FeedState:
    def __init__(self):
        self.final_utterances: list[str] = []  # bounded to 40, oldest dropped
        self.final_wrapped: list[str] = []  # bounded to 150, oldest dropped
        self.interim_wrapped: list[str] = []
        self.interim_text: str = ""
        # Monotonic timestamp of the last interim update for this feed - see
        # _live_interim()'s docstring for why this exists.
        self.interim_at: float = 0.0


class LiveAssistOverlayWindow:
    def __init__(self):
        self._lock = threading.RLock()
        self._hwnd = 0
        self._owner_hwnd = 0
        self._scale = 1.0
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

        settings = Settings.load()
        self._width = settings.live_assist_panel_w or _DEFAULT_W
        self._height = settings.live_assist_panel_h or _DEFAULT_H
        self._manual_pos = settings.live_assist_panel_x is not None and settings.live_assist_panel_y is not None
        self._x: int | None = settings.live_assist_panel_x if self._manual_pos else None
        self._y: int | None = settings.live_assist_panel_y if self._manual_pos else None
        self._ui_theme = settings.ui_theme
        self._bubble_theme = settings.live_assist_bubble_theme
        self._opacity = settings.live_assist_panel_opacity
        # Reuses the same Settings flag as the main window's own capture-
        # exclusion toggle (exclude_main_window_from_capture), rather than a
        # second standalone flag - it was always unconditionally excluded
        # before this, which meant there was no way to actually screen-
        # record or screenshot this panel at all, even with that toggle off.
        self._exclude_from_capture = settings.exclude_main_window_from_capture
        self._capture_excluded: bool | None = None

        self._state: LiveAssistState = LiveAssistState.ENDED
        self._feeds: dict[str, _FeedState] = {"me": _FeedState(), "they": _FeedState()}
        # Chronological (source_id, text) pairs across BOTH feeds, bubble-
        # theme only - the plain theme keeps its own per-feed lists/sections,
        # but a bubble chat log reads naturally as one interleaved thread.
        self._timeline: list[tuple[str, str]] = []
        # LA-09: the latest suggestion, shown in a reserved strip below the
        # two transcript sections - only reserved when there's something to
        # show (pending or a result), so the panel stays compact otherwise.
        self._suggestion_text = ""
        self._suggestion_pending = False
        self._dirty = False
        self._last_redraw_at = 0.0

        # Transport controls (header buttons) - set by app.py, same callback-
        # attribute pattern as overlay_window.py's on_mic_click/on_record_click.
        # A single on_pause_resume_click rather than separate on_pause_click/
        # on_resume_click: the overlay already tracks self._state to decide
        # which icon to draw, so app.py's handler can use the orchestrator's
        # own state the same way to decide pause-vs-resume, instead of this
        # window needing to know which action is "current".
        self.on_pause_resume_click: Callable[[], None] | None = None
        self.on_stop_click: Callable[[], None] | None = None
        self._pause_btn_rect: tuple[float, float, float, float] = (0, 0, 0, 0)
        self._stop_btn_rect: tuple[float, float, float, float] = (0, 0, 0, 0)

        self._resizing = False
        self._resize_start_mouse: tuple[int, int] | None = None
        self._resize_start_size: tuple[int, int] | None = None
        self._resize_grip_rect: tuple[float, float, float, float] = (0, 0, 0, 0)
        self._max_w = 10_000
        self._max_h = 10_000
        self._last_resize_render = 0.0

        self._dragging = False
        self._drag_start_mouse: tuple[int, int] | None = None
        self._drag_start_pos: tuple[int, int] | None = None
        self._header_rect: tuple[float, float, float, float] = (0, 0, 0, 0)

        self._last_work_area: tuple[int, int, int, int] | None = None
        self._focus_debounce_until = 0.0

        # Measurement-only fonts: plain, unscaled - see _wrap()'s docstring.
        self._measure_final_font = _font(13)
        self._measure_interim_font = _font(13, italic=True)

    # ---- lifecycle ---------------------------------------------------

    def create(self) -> None:
        self._thread = threading.Thread(target=self._run, name="liveassist-overlay", daemon=True)
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
            pass

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
            0, owner_class, "TalkFlowLiveAssistOwner", win32con.WS_POPUP,
            0, 0, 0, 0, 0, 0, wc.hInstance, None,
        )
        self._owner_hwnd = owner_hwnd

        ex = (win32con.WS_EX_LAYERED | win32con.WS_EX_TOPMOST |
              win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE)
        self._hwnd = win32gui.CreateWindowEx(
            ex, WINDOW_CLASS, "TalkFlowLiveAssist", win32con.WS_POPUP,
            0, 0, 10, 10, owner_hwnd, 0, wc.hInstance, None,
        )
        self._apply_capture_exclusion(self._exclude_from_capture)
        try:
            self._scale = _user32.GetDpiForWindow(self._hwnd) / 96.0
        except Exception:
            self._scale = 1.0
        _user32.SetTimer(ctypes.c_void_p(self._hwnd), TIMER_ID, 33, None)
        self._render()
        # Starts hidden - app.py calls show() only once a session is active.
        self._ready.set()
        win32gui.PumpMessages()

    # ---- window procedure ----------------------------------------------

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == win32con.WM_MOUSEACTIVATE:
                return 3  # MA_NOACTIVATE
            if msg == win32con.WM_LBUTTONDOWN:
                x = win32api.LOWORD(lparam) / self._scale
                y = win32api.HIWORD(lparam) / self._scale
                gx0, gy0, gx1, gy1 = self._resize_grip_rect
                if gx0 <= x <= gx1 and gy0 <= y <= gy1:
                    self._resizing = True
                    self._resize_start_mouse = win32gui.GetCursorPos()
                    self._resize_start_size = (self._width, self._height)
                    vx, vy, vw, vh = self._virtual_screen()
                    self._max_w, self._max_h = _resize_caps(self._x or 0, self._y or 0, vx, vy, vw, vh)
                    win32gui.SetCapture(hwnd)
                    return 0
                px0, py0, px1, py1 = self._pause_btn_rect
                if px0 <= x <= px1 and py0 <= y <= py1:
                    if self.on_pause_resume_click:
                        self.on_pause_resume_click()
                    return 0
                sx0b, sy0b, sx1b, sy1b = self._stop_btn_rect
                if sx0b <= x <= sx1b and sy0b <= y <= sy1b:
                    if self.on_stop_click:
                        self.on_stop_click()
                    return 0
                hx0, hy0, hx1, hy1 = self._header_rect
                if hx0 <= x <= hx1 and hy0 <= y <= hy1:
                    self._dragging = True
                    self._drag_start_mouse = win32gui.GetCursorPos()
                    self._drag_start_pos = (self._x or 0, self._y or 0)
                    win32gui.SetCapture(hwnd)
                return 0
            if msg == win32con.WM_MOUSEMOVE and self._resizing:
                now = time.monotonic()
                if now - self._last_resize_render < 1 / 120:
                    return 0
                cx, cy = win32gui.GetCursorPos()
                sx, sy = self._resize_start_mouse
                sw, sh = self._resize_start_size
                dx, dy = (cx - sx) / self._scale, (cy - sy) / self._scale
                # Top-left corner (self._x, self._y) is fixed for the whole
                # drag, so the window grows toward wherever the bottom-right
                # grip is actually dragged - the previous version recomputed
                # the right edge as pinned to the screen's right edge on
                # every frame, which fought a rightward drag and made the
                # window appear to grow in the opposite direction.
                new_w = max(MIN_W, min(self._max_w, round(sw + dx)))
                new_h = max(MIN_H, min(self._max_h, round(sh + dy)))
                if (new_w, new_h) != (self._width, self._height):
                    self._width, self._height = new_w, new_h
                    self._rewrap_all()
                    self._render()
                    self._last_resize_render = now
                return 0
            if msg == win32con.WM_MOUSEMOVE and self._dragging:
                now = time.monotonic()
                if now - self._last_resize_render < 1 / 120:
                    return 0
                cx, cy = win32gui.GetCursorPos()
                sx, sy = self._drag_start_mouse
                sx0, sy0 = self._drag_start_pos
                dx, dy = round((cx - sx) / self._scale), round((cy - sy) / self._scale)
                self._x, self._y = sx0 + dx, sy0 + dy
                self._manual_pos = True
                self._render()
                self._last_resize_render = now
                return 0
            if msg in (win32con.WM_LBUTTONUP, win32con.WM_CAPTURECHANGED):
                if self._resizing:
                    self._resizing = False
                    try:
                        win32gui.ReleaseCapture()
                    except Exception:
                        pass
                    width, height = self._width, self._height
                    threading.Thread(target=self._persist_size, args=(width, height), daemon=True).start()
                if self._dragging:
                    self._dragging = False
                    try:
                        win32gui.ReleaseCapture()
                    except Exception:
                        pass
                    x, y = self._x, self._y
                    threading.Thread(target=self._persist_position, args=(x, y), daemon=True).start()
                return 0
            if msg == WM_REFRESH:
                self._render()
                return 0
            if msg == win32con.WM_TIMER:
                self._check_monitor_change()
                if self._dirty:
                    now = time.monotonic()
                    if now - self._last_redraw_at >= _REDRAW_INTERVAL:
                        self._render()
                        self._last_redraw_at = now
                        self._dirty = False
                return 0
            if msg == win32con.WM_DESTROY:
                win32gui.PostQuitMessage(0)
                return 0
        except Exception:
            logger.exception("live assist overlay wndproc error")
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _apply_capture_exclusion(self, excluded: bool) -> None:
        if not self._hwnd:
            return
        if not excluded:
            _user32.SetWindowDisplayAffinity(ctypes.c_void_p(self._hwnd), 0)  # WDA_NONE
            self._capture_excluded = False
            return
        exclude_hwnd_from_capture(self._hwnd)
        self._capture_excluded = True

    @staticmethod
    def _persist_size(width: int, height: int) -> None:
        try:
            s = Settings.load()
            s.live_assist_panel_w, s.live_assist_panel_h = width, height
            s.save()
        except Exception:
            logger.exception("Could not persist Live Assist panel size")

    @staticmethod
    def _persist_position(x: int, y: int) -> None:
        try:
            s = Settings.load()
            s.live_assist_panel_x, s.live_assist_panel_y = x, y
            s.save()
        except Exception:
            logger.exception("Could not persist Live Assist panel position")

    # ---- monitor-follow / positioning ------------------------------------

    def _work_area(self) -> tuple[int, int, int, int]:
        try:
            fg = win32gui.GetForegroundWindow()
            if not fg or fg in (self._hwnd, self._owner_hwnd):
                hwnd = self._hwnd or win32gui.GetDesktopWindow()
            else:
                hwnd = fg
            info = win32api.GetMonitorInfo(
                win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST))
            return tuple(info["Work"])  # type: ignore[return-value]
        except Exception:
            return (0, 0, 1920, 1040)

    @staticmethod
    def _virtual_screen() -> tuple[int, int, int, int]:
        """Combined bounding box of ALL monitors (Windows' virtual-screen
        coordinate space) - lets the panel be dragged onto any monitor,
        including one to the left of/above the primary (negative coords)."""
        try:
            vx = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
            vy = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
            vw = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
            vh = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
            return vx, vy, vw, vh
        except Exception:
            return (0, 0, 1920, 1080)

    def _check_monitor_change(self) -> None:
        if self._resizing or self._dragging or self._manual_pos:
            return
        now = time.monotonic()
        if now < self._focus_debounce_until:
            return
        wa = self._work_area()
        if wa != self._last_work_area:
            self._last_work_area = wa
            self._focus_debounce_until = now + 0.15
            self._render()

    def _resolve_position(self, pw: int, ph: int) -> tuple[int, int]:
        """The top-left corner to present at, in physical pixels. Auto mode
        recomputes from the active monitor's top-right corner (original
        behavior); manual mode reuses/clamps the dragged position."""
        if not self._manual_pos:
            left, top, right, _bottom = self._work_area()
            x = round((right - pw / self._scale - 12) * self._scale)
            y = round((top + 12) * self._scale)
            self._x, self._y = round(x / self._scale), round(y / self._scale)
            return x, y
        vx, vy, vw, vh = self._virtual_screen()
        cx, cy = self._x or 0, self._y or 0
        cx_px, cy_px = round(cx * self._scale), round(cy * self._scale)
        cx_px, cy_px = _clamp_position(cx_px, cy_px, pw, ph, vx, vy, vw, vh)
        return cx_px, cy_px

    # ---- transcript state -----------------------------------------------

    def _avail_width(self) -> float:
        return self._width - 2 * _MARGIN

    def _rewrap_all(self) -> None:
        with self._lock:
            width = self._avail_width()
            for feed in self._feeds.values():
                wrapped: list[str] = []
                for text in feed.final_utterances:
                    wrapped.extend(_wrap(text, width, self._measure_final_font))
                feed.final_wrapped = wrapped[-150:]

    def append_utterance(self, source_id: str, text: str, is_final: bool) -> None:
        if source_id not in self._feeds:
            return
        with self._lock:
            feed = self._feeds[source_id]
            width = self._avail_width()
            if is_final:
                feed.final_utterances.append(text)
                feed.final_utterances = feed.final_utterances[-40:]
                feed.final_wrapped.extend(_wrap(text, width, self._measure_final_font))
                feed.final_wrapped = feed.final_wrapped[-150:]
                feed.interim_wrapped = []
                feed.interim_text = ""
                # Bubble theme reads this as one bubble per CONTINUOUS turn,
                # not one per ASR-finalized segment - a single person
                # talking for a while finalizes on every pause, which used
                # to spawn a new bubble each time ("300 bubbles even if I
                # don't stop talking"). Merge into the previous entry while
                # the same source keeps talking; only start a new entry when
                # the other source's speech appears. Capped so one very long
                # uninterrupted turn can't grow a merged entry unboundedly.
                if self._timeline and self._timeline[-1][0] == source_id:
                    merged = f"{self._timeline[-1][1]} {text}".strip()
                    self._timeline[-1] = (source_id, merged[-4000:])
                else:
                    self._timeline.append((source_id, text))
                self._timeline = self._timeline[-40:]
            else:
                feed.interim_wrapped = _wrap(text, width, self._measure_interim_font)
                feed.interim_text = text
                feed.interim_at = time.monotonic()
            self._dirty = True

    def _live_interim(self, source_id: str) -> tuple[str, list[str]]:
        """A feed's interim text/wrapped-lines, or ("", []) if it hasn't
        been updated in _INTERIM_STALE_S - see that constant's docstring."""
        feed = self._feeds[source_id]
        if not feed.interim_text:
            return "", []
        if time.monotonic() - feed.interim_at > _INTERIM_STALE_S:
            return "", []
        return feed.interim_text, feed.interim_wrapped

    def set_session_state(self, state: LiveAssistState) -> None:
        self._state = state
        if state == LiveAssistState.ARMING:
            # A fresh session - clear whatever the previous one left behind.
            with self._lock:
                self._feeds = {"me": _FeedState(), "they": _FeedState()}
                self._timeline = []
                self._suggestion_text = ""
                self._suggestion_pending = False
        self._poke()

    def set_suggestion_pending(self) -> None:
        with self._lock:
            self._suggestion_pending = True
            self._suggestion_text = ""
        self._dirty = True

    def set_suggestion(self, text: str, has_suggestion: bool) -> None:
        with self._lock:
            self._suggestion_pending = False
            self._suggestion_text = text if has_suggestion else "(nothing to suggest yet)"
        self._dirty = True

    # ---- settings (live-applied, no restart needed) ----------------------

    def apply_settings(self, s: Settings) -> None:
        with self._lock:
            self._ui_theme = s.ui_theme
            self._bubble_theme = s.live_assist_bubble_theme
            self._opacity = s.live_assist_panel_opacity
            if s.exclude_main_window_from_capture != self._exclude_from_capture:
                self._exclude_from_capture = s.exclude_main_window_from_capture
                self._apply_capture_exclusion(self._exclude_from_capture)
        self._dirty = True
        self._poke()

    # ---- rendering --------------------------------------------------------

    def _poke(self) -> None:
        if self._hwnd:
            win32gui.PostMessage(self._hwnd, WM_REFRESH, 0, 0)

    def _render(self) -> None:
        with self._lock:
            if not self._hwnd:
                return
            s = self._scale
            cw, ch = self._width, self._height
            pw, ph = round(cw * s), round(ch * s)
            img = self._draw(cw, ch, s)
            img = img.resize((pw, ph), Image.LANCZOS)
            x, y = self._resolve_position(pw, ph)
            present_layered(self._hwnd, img, pw, ph, x, y)

    def _draw(self, cw: int, ch: int, s: float) -> Image.Image:
        u = s * SS
        pal = _palette(self._ui_theme, self._opacity)
        img = Image.new("RGBA", (round(cw * u), round(ch * u)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        d.rounded_rectangle([0, 0, cw * u - 1, ch * u - 1], radius=10 * u,
                             fill=pal["panel_fill"], outline=pal["panel_edge"], width=round(u))

        # Header: title + transport buttons + state chip. This whole strip
        # (minus the resize grip, which lives in the opposite corner, and
        # the buttons themselves) is the drag handle.
        self._header_rect = (0, 0, cw, _HEADER_H)
        title_font = _font(13 * u, bold=True)
        d.text((_MARGIN * u, (_HEADER_H / 2) * u), "Live Assist", font=title_font, fill=pal["text"], anchor="lm")
        label, color = _STATE_LABEL.get(self._state, ("", (148, 163, 184)))
        state_font = _font(11 * u)
        tw = state_font.getlength(label)
        chip_x = cw * u - _MARGIN * u - tw - 14 * u

        # Pause/resume + stop, only meaningful (and only clickable) while a
        # session is actually running - placed just left of the state chip.
        showing_controls = self._state in (LiveAssistState.ACTIVE, LiveAssistState.PAUSED)
        if showing_controls:
            btn_y0 = (_HEADER_H - _BTN_SIZE) / 2
            stop_x1 = chip_x / u - _BTN_GAP
            stop_x0 = stop_x1 - _BTN_SIZE
            pause_x1 = stop_x0 - _BTN_GAP
            pause_x0 = pause_x1 - _BTN_SIZE
            self._stop_btn_rect = (stop_x0, btn_y0, stop_x1, btn_y0 + _BTN_SIZE)
            self._pause_btn_rect = (pause_x0, btn_y0, pause_x1, btn_y0 + _BTN_SIZE)
            _draw_stop_icon(d, self._stop_btn_rect, u, pal["text"])
            if self._state == LiveAssistState.PAUSED:
                _draw_play_icon(d, self._pause_btn_rect, u, pal["text"])
            else:
                _draw_pause_icon(d, self._pause_btn_rect, u, pal["text"])
            chip_x = pause_x0 * u - 10 * u
        else:
            self._stop_btn_rect = (0, 0, 0, 0)
            self._pause_btn_rect = (0, 0, 0, 0)

        d.ellipse([chip_x, (_HEADER_H / 2 - 4) * u, chip_x + 8 * u, (_HEADER_H / 2 + 4) * u],
                  fill=color + (255,))
        d.text((chip_x + 14 * u, (_HEADER_H / 2) * u), label, font=state_font, fill=pal["text_dim"], anchor="lm")
        d.line([_MARGIN * u, _HEADER_H * u, (cw - _MARGIN) * u, _HEADER_H * u], fill=pal["panel_edge"], width=round(u))

        if self._bubble_theme:
            self._draw_bubbles(d, cw, ch, u, pal)
        else:
            self._draw_plain(d, cw, ch, u, pal)

        # Resize grip - three diagonal lines, bottom-right corner. Growth is
        # always anchored at the window's current top-left (self._x/_y),
        # so dragging this grip tracks the cursor in both directions now.
        self._resize_grip_rect = (cw - _GRIP_SIZE - 4, ch - _GRIP_SIZE - 4, cw - 4, ch - 4)
        gx0, gy0, gx1, gy1 = self._resize_grip_rect
        for offset in (0, 4, 8):
            d.line([(gx0 + offset) * u, gy1 * u, gx1 * u, (gy0 + offset) * u], fill=pal["text_dim"], width=round(u))

        return img

    def _draw_plain(self, d: ImageDraw.ImageDraw, cw: int, ch: int, u: float, pal: dict) -> None:
        content_top = _HEADER_H + 6
        show_suggestion = bool(self._suggestion_pending or self._suggestion_text)
        label_font = _font(11 * u, bold=True)
        final_font = _font(13 * u)
        interim_font = _font(13 * u, italic=True)

        # Size the suggestion strip from its ACTUAL wrapped line count,
        # instead of a fixed height - confirmed live: a fixed 70px strip
        # silently truncated every real answer to 2 lines (70-18)/18, no
        # matter how long the model's actual response was. Bounded so a
        # very long answer still leaves at least a sliver for the two
        # transcript sections rather than eating the whole panel.
        suggestion_lines: list[str] = []
        if show_suggestion and not self._suggestion_pending:
            suggestion_lines = _wrap(self._suggestion_text, self._avail_width(), self._measure_final_font)
        available_for_suggestion = max(0, ch - content_top - _MARGIN - 2 * _LABEL_H)
        max_suggestion_lines = max(1, int((available_for_suggestion * 0.7) / _LINE_H))
        if show_suggestion:
            line_count = 1 if self._suggestion_pending else max(1, len(suggestion_lines))
            suggestion_h = _LABEL_H + min(line_count, max_suggestion_lines) * _LINE_H
        else:
            suggestion_h = 0
        content_h = ch - content_top - _MARGIN - suggestion_h
        section_h = content_h / 2

        for i, (source_id, label_text, accent) in enumerate(_SECTIONS):
            top = content_top + i * section_h
            d.text((_MARGIN * u, (top + _LABEL_H / 2) * u), label_text, font=label_font,
                   fill=accent + (255,), anchor="lm")
            feed = self._feeds[source_id]
            _interim_text, interim_wrapped = self._live_interim(source_id)
            lines_top = top + _LABEL_H
            lines_h = section_h - _LABEL_H
            reserved_for_interim = len(interim_wrapped)
            max_lines = max(0, int(lines_h / _LINE_H) - reserved_for_interim)
            tail = list(islice(reversed(feed.final_wrapped), max_lines))
            tail.reverse()
            y = lines_top
            for line in tail:
                d.text((_MARGIN * u, y * u), line, font=final_font, fill=pal["text"], anchor="lm")
                y += _LINE_H
            for line in interim_wrapped:
                d.text((_MARGIN * u, y * u), line, font=interim_font, fill=pal["text_dim"], anchor="lm")
                y += _LINE_H

        if show_suggestion:
            sugg_top = content_top + content_h
            d.line([_MARGIN * u, sugg_top * u, (cw - _MARGIN) * u, sugg_top * u], fill=pal["panel_edge"], width=round(u))
            d.text((_MARGIN * u, (sugg_top + _LABEL_H / 2) * u), "SUGGESTION", font=label_font,
                   fill=_SUGGESTION_ACCENT + (255,), anchor="lm")
            text_top = sugg_top + _LABEL_H
            if self._suggestion_pending:
                d.text((_MARGIN * u, text_top * u), "Thinking…", font=interim_font, fill=pal["text_dim"], anchor="lm")
            else:
                max_lines = max(1, int((suggestion_h - _LABEL_H) / _LINE_H))
                lines = suggestion_lines[:max_lines]
                y = text_top
                for line in lines:
                    d.text((_MARGIN * u, y * u), line, font=final_font, fill=pal["text"], anchor="lm")
                    y += _LINE_H

    def _draw_bubbles(self, d: ImageDraw.ImageDraw, cw: int, ch: int, u: float, pal: dict) -> None:
        """Perssua/iMessage-style: one chronological thread, "me" bubbles
        solid-accent and right-aligned, "they" bubbles neutral and
        left-aligned, and - per the explicit ask - the model's own
        suggestion rendered the SAME way (a bubble, not a plain text block),
        just amber-outlined so it still reads as distinct from either
        speaker."""
        final_font = _font(13 * u)
        interim_font = _font(13 * u, italic=True)
        bubble_max_w = self._avail_width() * _BUBBLE_MAX_WIDTH_FRAC

        # Build the full ordered entry list from the already turn-merged
        # timeline (see append_utterance). An in-progress interim utterance
        # streams straight INTO the current bubble - visually extending the
        # last entry - for fast feedback without opening a new bubble, as
        # long as it's the SAME speaker still talking; a genuinely new
        # speaker starting (no committed turn for them yet) gets its own
        # provisional entry, since that IS a real speaker change. Only this
        # local `entries` list is touched - self._timeline itself stays
        # exactly as committed by append_utterance.
        entries: list[tuple[str, str, bool]] = list(
            (source_id, text, False) for source_id, text in self._timeline
        )
        for source_id in ("me", "they"):
            interim, _wrapped = self._live_interim(source_id)
            if not interim:
                continue
            if entries and entries[-1][0] == source_id and not entries[-1][2]:
                merged = f"{entries[-1][1]} {interim}".strip()
                entries[-1] = (source_id, merged, False)
            else:
                entries.append((source_id, interim, False))
        show_suggestion = bool(self._suggestion_pending or self._suggestion_text)
        if show_suggestion:
            entries.append(("suggestion", self._suggestion_text, self._suggestion_pending))

        def _style(source_id: str) -> tuple[tuple[int, int, int], tuple[int, int, int, int], bool]:
            if source_id == "me":
                return (255, 255, 255), _ME_ACCENT + (255,), True
            if source_id == "suggestion":
                return pal["text"][:3], (0, 0, 0, 0), False
            return pal["bubble_them_text"][:3], pal["bubble_them_fill"], False

        # Pre-wrap every entry and compute its pixel height, working from
        # the MOST RECENT entry backward, so a long history never pushes
        # today's conversation off the bottom of a short panel - the same
        # "tail that fits" behavior the plain theme already has.
        content_top = _HEADER_H + 6
        available_h = ch - content_top - _MARGIN
        laid_out: list[tuple[str, str, bool, list[str]]] = []  # (source_id, _, pending, lines)
        used_h = 0.0
        for source_id, text, pending in reversed(entries):
            lines = ["Thinking…"] if pending else (_wrap(text, bubble_max_w, self._measure_final_font) or [""])
            bubble_h = _BUBBLE_PAD_Y * 2 + len(lines) * _LINE_H
            if used_h + bubble_h > available_h and laid_out:
                break
            laid_out.append((source_id, text, pending, lines))
            used_h += bubble_h + _BUBBLE_GAP
        laid_out.reverse()

        y = content_top
        for source_id, _text, pending, lines in laid_out:
            font = interim_font if pending else final_font
            text_color, fill, right_align = _style(source_id)
            # Width math MUST use the plain, unscaled measure fonts (same
            # reference scale as bubble_max_w/cw/_MARGIN etc.) - using the
            # SCALED draw font here (sized for DPI x supersampling, often
            # several times bigger) was computing a pixel width several
            # times too large, which is exactly why bubbles were rendering
            # bigger than the panel itself with huge empty space after the
            # text: the rectangle was being sized off a wildly inflated
            # measurement, then that same oversized box got drawn (at the
            # correct scale) around text that was actually much narrower.
            measure_font = self._measure_interim_font if pending else self._measure_final_font
            line_widths = [measure_font.getlength(line) for line in lines]
            bubble_w = min(max(line_widths, default=0) + _BUBBLE_PAD_X * 2, self._avail_width())
            bubble_h = _BUBBLE_PAD_Y * 2 + len(lines) * _LINE_H
            if right_align:
                bx1 = cw - _MARGIN
                bx0 = max(_MARGIN, bx1 - bubble_w)
            else:
                bx0 = _MARGIN
                bx1 = min(cw - _MARGIN, bx0 + bubble_w)
            by0, by1 = y, y + bubble_h
            if source_id == "suggestion":
                d.rounded_rectangle([bx0 * u, by0 * u, bx1 * u, by1 * u], radius=_BUBBLE_RADIUS * u,
                                     outline=_SUGGESTION_ACCENT + (255,), width=round(1.5 * u))
            else:
                d.rounded_rectangle([bx0 * u, by0 * u, bx1 * u, by1 * u], radius=_BUBBLE_RADIUS * u, fill=fill)
            ty = by0 + _BUBBLE_PAD_Y
            tx = bx0 + _BUBBLE_PAD_X
            fill_color = text_color + (255,) if len(text_color) == 3 else text_color
            for line in lines:
                d.text((tx * u, ty * u), line, font=font, fill=fill_color, anchor="lt")
                ty += _LINE_H
            y += bubble_h + _BUBBLE_GAP

    # ---- public API used by app.py --------------------------------------

    def show(self) -> None:
        if self._hwnd:
            win32gui.ShowWindow(self._hwnd, win32con.SW_SHOWNOACTIVATE)

    def hide(self) -> None:
        if self._hwnd:
            win32gui.ShowWindow(self._hwnd, win32con.SW_HIDE)
