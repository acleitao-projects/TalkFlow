"""Unit tests for the pure, display-independent helpers in
talkflow/overlay/live_assist_overlay_window.py (_palette, _clamp_position,
_resize_caps, _wrap). The window class itself is raw Win32 GUI code with no
existing test coverage (same as the dictation pill) - these are the only
genuinely testable pieces: theme/opacity color selection, and the
multi-monitor position/resize math fixed in this change.

append_utterance()/_live_interim() ARE also tested directly below despite
being instance methods: the class's __init__ creates no win32 resources
(only .create() does - see its own docstring), so a plain
LiveAssistOverlayWindow() is safe to construct and exercise without a real
window or display.
"""
import time

from talkflow.overlay.live_assist_overlay_window import (
    MIN_H,
    MIN_W,
    LiveAssistOverlayWindow,
    _clamp_position,
    _palette,
    _resize_caps,
    _wrap,
)


def test_palette_light_vs_dark_differ():
    light = _palette("light", 92)
    dark = _palette("dark", 92)
    assert light["panel_fill"][:3] != dark["panel_fill"][:3]
    assert light["text"][:3] != dark["text"][:3]


def test_palette_opacity_sets_fill_alpha():
    full = _palette("dark", 100)
    half = _palette("dark", 50)
    low = _palette("dark", 15)
    assert full["panel_fill"][3] == 255
    assert half["panel_fill"][3] == round(0.5 * 255)
    assert low["panel_fill"][3] < half["panel_fill"][3]


def test_palette_opacity_is_clamped_to_valid_range():
    too_low = _palette("dark", 0)
    too_high = _palette("dark", 1000)
    assert too_low["panel_fill"][3] == round(0.15 * 255)
    assert too_high["panel_fill"][3] == 255


def test_clamp_position_keeps_window_within_single_monitor():
    # A 300x200 window dragged to sit entirely inside a 1920x1080 monitor
    # at (0,0) needs no adjustment.
    x, y = _clamp_position(500, 400, 300, 200, 0, 0, 1920, 1080)
    assert (x, y) == (500, 400)


def test_clamp_position_allows_secondary_monitor_with_negative_coords():
    # A monitor placed to the LEFT of the primary has negative virtual-
    # screen coordinates in Windows' multi-monitor layout; the panel must
    # be draggable there, not clamped back onto the primary monitor.
    vx, vy, vw, vh = -1920, 0, 3840, 1080  # secondary (left) + primary
    x, y = _clamp_position(-1000, 300, 300, 200, vx, vy, vw, vh)
    assert (x, y) == (-1000, 300)


def test_clamp_position_never_lets_window_fully_disappear():
    vx, vy, vw, vh = 0, 0, 1920, 1080
    # Dragged far off the right/bottom edge.
    x, y = _clamp_position(100_000, 100_000, 300, 200, vx, vy, vw, vh, min_visible=40)
    assert x <= vx + vw - 40
    assert y <= vy + vh - 40
    # Dragged far off the left/top edge.
    x, y = _clamp_position(-100_000, -100_000, 300, 200, vx, vy, vw, vh, min_visible=40)
    assert x >= vx - 300 + 40
    assert y >= vy - 200 + 40


def test_resize_caps_shrink_toward_available_room_near_an_edge():
    vx, vy, vw, vh = 0, 0, 1920, 1080
    max_w, max_h = _resize_caps(1200, 700, vx, vy, vw, vh)
    # Clearly less room than when anchored near the top-left (next test),
    # but the floor never drops below the absolute minimum size even when
    # that leaves slightly less room than is technically available.
    assert MIN_W <= max_w <= 720
    assert MIN_H <= max_h <= 380


def test_resize_caps_never_below_minimum_even_flush_with_the_edge():
    vx, vy, vw, vh = 0, 0, 1920, 1080
    max_w, max_h = _resize_caps(1900, 1070, vx, vy, vw, vh)
    assert max_w == MIN_W
    assert max_h == MIN_H


def test_resize_caps_generous_when_anchored_near_top_left():
    vx, vy, vw, vh = 0, 0, 1920, 1080
    max_w, max_h = _resize_caps(10, 10, vx, vy, vw, vh)
    assert max_w > 500
    assert max_h > 500


def test_wrap_splits_long_text_at_word_boundaries():
    class _FakeFont:
        def getlength(self, s: str) -> float:
            return len(s)

    lines = _wrap("one two three four five", max_width=11, font=_FakeFont())
    assert lines == ["one two", "three four", "five"]
    assert all(len(line) <= 11 for line in lines)


def test_wrap_empty_text_returns_no_lines():
    class _FakeFont:
        def getlength(self, s: str) -> float:
            return len(s)

    assert _wrap("", max_width=100, font=_FakeFont()) == []


def test_turn_merging_same_speaker_collapses_into_one_entry():
    w = LiveAssistOverlayWindow()
    for chunk in ["one", "two", "three"]:
        w.append_utterance("me", chunk, True)
    assert w._timeline == [("me", "one two three")]


def test_turn_merging_starts_a_new_entry_on_speaker_change():
    w = LiveAssistOverlayWindow()
    w.append_utterance("me", "hello", True)
    w.append_utterance("they", "hi there", True)
    w.append_utterance("me", "how are you", True)
    assert w._timeline == [("me", "hello"), ("they", "hi there"), ("me", "how are you")]


def test_live_interim_returns_fresh_interim_text():
    w = LiveAssistOverlayWindow()
    w.append_utterance("me", "partial words", False)
    text, wrapped = w._live_interim("me")
    assert text == "partial words"
    assert wrapped


def test_live_interim_expires_stale_leftover_after_final():
    """Regression test: the realtime ASR adapter emits one more interim
    event carrying the SAME text right after committing a final for it
    (confirmed live - see _INTERIM_STALE_S's docstring). Once that speaker
    goes quiet, nothing ever overwrites or clears that leftover interim
    again - without expiry, it would be redrawn forever as a phantom
    one-word bubble/trailing line, which is exactly the bug reported live:
    a committed "...gestão de produtos" bubble, correctly followed by the
    OTHER speaker's turn, but then a stray leftover "Tudo" bubble kept
    reappearing afterward with no one actually still talking."""
    w = LiveAssistOverlayWindow()
    w.append_utterance("me", "gestão de produtos", True)
    w.append_utterance("me", "Tudo", True)  # commits, merges into the same turn
    w.append_utterance("me", "Tudo", False)  # the adapter's trailing echo interim
    assert w._timeline == [("me", "gestão de produtos Tudo")]
    text, _wrapped = w._live_interim("me")
    assert text == "Tudo"  # still fresh immediately after
    w._feeds["me"].interim_at -= 3.0  # simulate 3s of silence from "me"
    text, wrapped = w._live_interim("me")
    assert text == ""
    assert wrapped == []
