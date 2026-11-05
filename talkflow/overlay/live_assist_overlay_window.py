"""LiveAssistOverlayWindow - LA-06: a second native overlay, separate from
the dictation pill, showing a Live Assist session's two transcript feeds
("You" / mic, "Them" / system-output) live as they're recognized, plus
session state. Built the same way as the pill (talkflow/overlay/
overlay_window.py): raw Win32 layered window, PIL-rendered, own thread +
message pump, excluded from screen capture - but kept as a separate class
rather than folded into the pill's modes, since this window needs to stay
open for a whole session instead of auto-reverting like the pill's transient
idle/hover/recording states.

Resizable via a corner grip (drag-to-move does not exist anywhere in this
app and wasn't asked for - position always auto-anchors to the active
monitor's top-right corner); the chosen size is remembered across restarts.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from itertools import islice

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
_LABEL_H = 18
_LINE_H = 18
_REDRAW_INTERVAL = 0.1  # ~10 redraws/sec cap, coalesces rapid interim updates

_FILL = (13, 13, 16)
_STATE_LABEL = {
    LiveAssistState.ARMING: ("Starting…", (250, 204, 21)),
    LiveAssistState.ACTIVE: ("Live", (74, 222, 128)),
    LiveAssistState.PAUSED: ("Paused", (250, 204, 21)),
    LiveAssistState.STOPPING: ("Ending…", (250, 204, 21)),
    LiveAssistState.ENDED: ("Ended", (148, 163, 184)),
    LiveAssistState.ERROR: ("Error", (248, 113, 113)),
}
_SECTIONS = (("me", "YOU", (167, 139, 250)), ("they", "THEM", (96, 165, 250)))


class _FeedState:
    def __init__(self):
        self.final_utterances: list[str] = []  # bounded to 40, oldest dropped
        self.final_wrapped: list[str] = []  # bounded to 150, oldest dropped
        self.interim_wrapped: list[str] = []


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

        self._state: LiveAssistState = LiveAssistState.ENDED
        self._feeds: dict[str, _FeedState] = {"me": _FeedState(), "they": _FeedState()}
        # LA-09: the latest suggestion, shown in a reserved strip below the
        # two transcript sections - only reserved when there's something to
        # show (pending or a result), so the panel stays compact otherwise.
        self._suggestion_text = ""
        self._suggestion_pending = False
        self._dirty = False
        self._last_redraw_at = 0.0

        self._resizing = False
        self._resize_start_mouse: tuple[int, int] | None = None
        self._resize_start_size: tuple[int, int] | None = None
        self._resize_grip_rect: tuple[float, float, float, float] = (0, 0, 0, 0)
        self._max_w = 10_000
        self._max_h = 10_000
        self._last_resize_render = 0.0

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
        exclude_hwnd_from_capture(self._hwnd)
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
                    left, top, right, bottom = self._work_area()
                    self._max_w = round((right - left) * 0.55)
                    self._max_h = round((bottom - top) * 0.80)
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
                new_w = max(MIN_W, min(self._max_w, round(sw + dx)))
                new_h = max(MIN_H, min(self._max_h, round(sh + dy)))
                if (new_w, new_h) != (self._width, self._height):
                    self._width, self._height = new_w, new_h
                    self._rewrap_all()
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

    @staticmethod
    def _persist_size(width: int, height: int) -> None:
        try:
            s = Settings.load()
            s.live_assist_panel_w, s.live_assist_panel_h = width, height
            s.save()
        except Exception:
            logger.exception("Could not persist Live Assist panel size")

    # ---- monitor-follow (own copy - see overlay_window.py's identical
    # logic; each window tracks its own last-known work area/debounce) ----

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

    def _check_monitor_change(self) -> None:
        now = time.monotonic()
        if now < self._focus_debounce_until:
            return
        wa = self._work_area()
        if wa != self._last_work_area:
            self._last_work_area = wa
            self._focus_debounce_until = now + 0.15
            self._render()

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
            else:
                feed.interim_wrapped = _wrap(text, width, self._measure_interim_font)
            self._dirty = True

    def set_session_state(self, state: LiveAssistState) -> None:
        self._state = state
        if state == LiveAssistState.ARMING:
            # A fresh session - clear whatever the previous one left behind.
            with self._lock:
                self._feeds = {"me": _FeedState(), "they": _FeedState()}
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
            left, top, right, _bottom = self._work_area()
            x = right - pw - 12
            y = top + 12
            present_layered(self._hwnd, img, pw, ph, x, y)

    def _draw(self, cw: int, ch: int, s: float) -> Image.Image:
        u = s * SS
        img = Image.new("RGBA", (round(cw * u), round(ch * u)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        solid = _FILL + (235,)
        edge = (255, 255, 255, 60)
        white = (255, 255, 255, 255)
        dim = (255, 255, 255, 140)

        d.rounded_rectangle([0, 0, cw * u - 1, ch * u - 1], radius=10 * u, fill=solid, outline=edge, width=round(u))

        # Header: title + state chip.
        title_font = _font(13 * u, bold=True)
        d.text((_MARGIN * u, (_HEADER_H / 2) * u), "Live Assist", font=title_font, fill=white, anchor="lm")
        label, color = _STATE_LABEL.get(self._state, ("", (148, 163, 184)))
        state_font = _font(11 * u)
        tw = state_font.getlength(label)
        chip_x = cw * u - _MARGIN * u - tw - 14 * u
        d.ellipse([chip_x, (_HEADER_H / 2 - 4) * u, chip_x + 8 * u, (_HEADER_H / 2 + 4) * u],
                  fill=color + (255,))
        d.text((chip_x + 14 * u, (_HEADER_H / 2) * u), label, font=state_font, fill=dim, anchor="lm")
        d.line([_MARGIN * u, _HEADER_H * u, (cw - _MARGIN) * u, _HEADER_H * u], fill=edge, width=round(u))

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
            lines_top = top + _LABEL_H
            lines_h = section_h - _LABEL_H
            reserved_for_interim = len(feed.interim_wrapped)
            max_lines = max(0, int(lines_h / _LINE_H) - reserved_for_interim)
            tail = list(islice(reversed(feed.final_wrapped), max_lines))
            tail.reverse()
            y = lines_top
            for line in tail:
                d.text((_MARGIN * u, y * u), line, font=final_font, fill=white, anchor="lm")
                y += _LINE_H
            for line in feed.interim_wrapped:
                d.text((_MARGIN * u, y * u), line, font=interim_font, fill=dim, anchor="lm")
                y += _LINE_H

        if show_suggestion:
            sugg_top = content_top + content_h
            d.line([_MARGIN * u, sugg_top * u, (cw - _MARGIN) * u, sugg_top * u], fill=edge, width=round(u))
            d.text((_MARGIN * u, (sugg_top + _LABEL_H / 2) * u), "SUGGESTION", font=label_font,
                   fill=(250, 204, 21, 255), anchor="lm")
            text_top = sugg_top + _LABEL_H
            if self._suggestion_pending:
                d.text((_MARGIN * u, text_top * u), "Thinking…", font=interim_font, fill=dim, anchor="lm")
            else:
                max_lines = max(1, int((suggestion_h - _LABEL_H) / _LINE_H))
                lines = suggestion_lines[:max_lines]
                y = text_top
                for line in lines:
                    d.text((_MARGIN * u, y * u), line, font=final_font, fill=white, anchor="lm")
                    y += _LINE_H

        # Resize grip - three diagonal lines, bottom-right corner.
        self._resize_grip_rect = (cw - _GRIP_SIZE - 4, ch - _GRIP_SIZE - 4, cw - 4, ch - 4)
        gx0, gy0, gx1, gy1 = self._resize_grip_rect
        for offset in (0, 4, 8):
            d.line([(gx0 + offset) * u, gy1 * u, gx1 * u, (gy0 + offset) * u], fill=dim, width=round(u))

        return img

    # ---- public API used by app.py --------------------------------------

    def show(self) -> None:
        if self._hwnd:
            win32gui.ShowWindow(self._hwnd, win32con.SW_SHOWNOACTIVATE)

    def hide(self) -> None:
        if self._hwnd:
            win32gui.ShowWindow(self._hwnd, win32con.SW_HIDE)
