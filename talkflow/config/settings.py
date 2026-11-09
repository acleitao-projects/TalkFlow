"""App-level user settings: hotkey mode, language allowlist, retention flags.

Stored as a small JSON file (not sqlite) because it's a single singleton
object edited as a whole from the Settings UI, not a row collection.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from talkflow.config.paths import app_data_dir

TriggerMode = Literal["hold", "toggle"]


@dataclass
class Settings:
    trigger_mode: TriggerMode = "hold"
    # Ctrl+Shift+<letter> combos by default, NOT Ctrl+Alt: on international
    # keyboard layouts (e.g. pt-BR/ABNT2) Ctrl+Alt together IS the AltGr key,
    # so Windows doesn't deliver it as two independent modifiers and the
    # combo silently never fires. Also avoiding F-keys (laptop Fn-lock/media
    # key interception, F5=browser refresh) and Chrome DevTools letters
    # (C/I/J). See talkflow/capture/hotkey.py for the "<mod>+<mod>+key" spec
    # format, and the Settings tab's key-capture button for setting your own
    # without needing to know this format at all.
    # Dictate defaults to bare Caps Lock while testing (one key, easy to hit).
    # The key still toggles Caps Lock itself; in toggle mode start + stop
    # nets out to no change.
    hotkey: str = "<caps_lock>"
    transform_polish_hotkey: str = "<ctrl>+<shift>+p"
    transform_prompt_engineer_hotkey: str = "<ctrl>+<shift>+e"
    command_mode_hotkey: str = "<ctrl>+<shift>+m"
    panic_hide_hotkey: str = "<ctrl>+<shift>+h"
    live_assist_hotkey: str = "<ctrl>+<shift>+l"
    live_assist_ask_hotkey: str = "<ctrl>+<shift>+k"  # LA-07: manual suggestion request
    live_assist_capture_hotkey: str = "<ctrl>+<shift>+g"  # LA-07: screen capture + question
    language_allowlist: list[str] = field(default_factory=lambda: ["en"])
    # How aggressively the cleanup stage edits the transcript, independent of
    # the per-app/category formality register: "none" skips the LLM call
    # entirely (verbatim transcript), "light" (default, previous fixed
    # behavior) only removes disfluencies, "medium" also tightens wording.
    cleanup_level: Literal["none", "light", "medium"] = "light"
    input_device_id: str | None = None  # None = system default
    # Live Assist's "they" (system-audio) source: None captures the whole
    # default playback device via WASAPI loopback (original LA-04 behavior).
    # Some audio interfaces (e.g. RodeCaster Pro II, confirmed live) mix the
    # user's own mic into their USB monitor/return bus, so that loopback
    # captures a copy of the user's own voice too - not acoustic bleed, a
    # hardware routing quirk - showing up as near-duplicate "me"/"they"
    # lines. Setting this to a specific process name (e.g. "zoom.exe")
    # switches to per-process WASAPI capture (see capture.py), which only
    # ever contains that one app's rendered audio.
    live_assist_system_audio_process_name: str | None = None
    # Explicit device overrides for Live Assist's two sources - set when the
    # per-process capture above isn't the right tool (e.g. you want "they"
    # to loopback a specific OUTPUT device, such as a RodeCaster's dedicated
    # "Chat" bus, rather than one specific app's process). None keeps the
    # existing default (default mic / default-speaker loopback).
    live_assist_mic_device_name: str | None = None
    live_assist_system_audio_device_name: str | None = None
    retain_history: bool = True
    retain_audio: bool = False  # transcripts are retained by default; raw audio is not
    launch_at_login: bool = False  # mirrors a Startup-folder shortcut - see talkflow/config/autostart.py
    # Paste cleaned text piece by piece at each pause while still recording.
    # Off by default: each piece is cleaned without the rest of the dictation,
    # which weakens the cleanup (found in live use). The default pipeline
    # still transcribes at each pause while you speak, but cleans and pastes
    # the whole text once at stop. Only takes effect when no modifier is held
    # during recording (partial_paste_allowed()).
    partial_paste: bool = False
    # Shows a small notes icon on the dictation pill that opens the floating
    # multi-tab Scratchpad popup (talkflow/floatingnotes/). Off by default -
    # the pill stays minimal until the user opts in from the Scratchpad tab.
    scratchpad_in_pill: bool = False
    # Floating notes popup geometry, persisted across restarts once the user
    # moves/resizes it at least once; None means "use the computed default
    # (bottom-right corner, near the pill)".
    scratchpad_popup_x: int | None = None
    scratchpad_popup_y: int | None = None
    scratchpad_popup_w: int | None = None
    scratchpad_popup_h: int | None = None
    # Live Assist transcript panel size, persisted once the user resizes it;
    # None means "use the compact default." Position is never persisted -
    # it auto-anchors to the active monitor, same philosophy as the pill.
    live_assist_panel_w: int | None = None
    live_assist_panel_h: int | None = None
    # Live Assist panel position, persisted only once the user drags it by
    # its header - None means "auto-anchor to the active monitor's top-right
    # corner" (the original behavior). Absolute virtual-screen coordinates
    # (can be negative - a monitor to the left of/above the primary one has
    # negative coords in Windows' multi-monitor layout), so a position on
    # any monitor round-trips correctly.
    live_assist_panel_x: int | None = None
    live_assist_panel_y: int | None = None
    # Perssua-style iPhone/iMessage-like chat bubbles instead of plain
    # flowing transcript text. Follows ui_theme (light/dark) automatically -
    # no separate theme field, since this panel should always match the
    # rest of the app rather than have its own independent look.
    live_assist_bubble_theme: bool = False
    # Panel background opacity, 15-100 (percent). Lower = more see-through.
    live_assist_panel_opacity: int = 92
    # How long (minutes) a Gemini Live connection runs before this app
    # proactively replaces it with a freshly-warmed one - see
    # gemini_live_asr.py's TwoSourceGeminiLiveAsrAdapter docstring for why:
    # the connection has a confirmed ~10-minute hard lifetime with no
    # documented minimum warning before it's force-closed, so waiting for
    # that warning and reconnecting reactively risks too little notice.
    # Only applies when gemini-live is the active live_assist_asr provider.
    # Default leaves 2 minutes of margin; lower it if real long sessions
    # ever show the hard limit being hit anyway, raise it to swap less
    # often (fewer brief "fresh connection" warm-up gaps) if 2 minutes of
    # margin proves more than needed in practice.
    live_assist_gemini_hotswap_minutes: int = 8
    # LA-08: which live_assist_assistants row the suggestion engine (LA-09)
    # applies. None means "use the builtin General Assistant" - resolved at
    # call time (features/liveassist/assistants.py), not cached here, so a
    # change in the main window's Assistants section reaches the next
    # suggestion request without a restart.
    live_assist_active_assistant_id: str | None = None
    # Main-window UI preferences (not pipeline behavior) - persisted the same
    # way as everything else in this file, not via browser localStorage,
    # since pywebview's storage_path handling is inconsistent across windows
    # and this file is already the single source of truth for "how TalkFlow
    # looks/behaves for this user".
    ui_theme: str = "light"  # "light" | "dark"
    ui_language: str = "en"  # "en" | "pt-BR" - interface language, independent of dictation language_allowlist
    ui_sidebar_collapsed: bool = False
    ui_dictionary_banner_dismissed: bool = False
    ui_snippets_banner_dismissed: bool = False
    # Hides the main window from screen capture/recording/Print Screen (same
    # mechanism as the dictation pill) - on by default for privacy. Turn off
    # temporarily when you actually want to screenshot the main window
    # yourself (e.g. to show someone a settings page).
    exclude_main_window_from_capture: bool = True

    def hotkey_fields(self) -> dict[str, str]:
        """Every app-level (non-per-transform) hotkey, keyed by its Settings
        field name - the set LA-07's conflict check validates against."""
        return {
            "hotkey": self.hotkey,
            "transform_polish_hotkey": self.transform_polish_hotkey,
            "transform_prompt_engineer_hotkey": self.transform_prompt_engineer_hotkey,
            "command_mode_hotkey": self.command_mode_hotkey,
            "panic_hide_hotkey": self.panic_hide_hotkey,
            "live_assist_hotkey": self.live_assist_hotkey,
            "live_assist_ask_hotkey": self.live_assist_ask_hotkey,
            "live_assist_capture_hotkey": self.live_assist_capture_hotkey,
        }

    def conflicting_hotkey(self, changing_key: str, new_spec: str) -> str | None:
        """The field/transform name of another binding that collides with
        new_spec, or None. Compares parsed (modifier-set, main-key) rather
        than raw strings so "<shift>+<ctrl>+l" and "<ctrl>+<shift>+l" are
        correctly seen as the same combo. Before this, rebinding any hotkey
        - old or new - just silently saved over whatever already used that
        combo; nothing checked at all."""
        from talkflow.capture.hotkey import _parse_spec
        try:
            new_parsed = _parse_spec(new_spec)
        except ValueError:
            return None  # an invalid spec is a different problem, not a conflict
        for key, spec in self.hotkey_fields().items():
            if key == changing_key or not spec:
                continue
            try:
                if _parse_spec(spec) == new_parsed:
                    return key
            except ValueError:
                continue
        from talkflow.features.transforms import store as transform_store
        for transform in transform_store.list_custom_transforms():
            if not transform.hotkey or transform.id == changing_key:
                continue
            try:
                if _parse_spec(transform.hotkey) == new_parsed:
                    return f"transform:{transform.name}"
            except ValueError:
                continue
        return None

    def partial_paste_allowed(self) -> bool:
        """Partial paste synthesizes Ctrl+V mid-recording; with a held
        Ctrl/Shift/Alt hotkey that would become a different shortcut."""
        if not self.partial_paste:
            return False
        held_modifiers = any(m in self.hotkey for m in ("<ctrl>", "<alt>", "<shift>", "<win>"))
        return self.trigger_mode == "toggle" or not held_modifiers

    @classmethod
    def load(cls) -> "Settings":
        path = _settings_path()
        if not path.exists():
            s = cls()
            s.save()
            return s
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(**{**asdict(cls()), **data})
        except (json.JSONDecodeError, TypeError):
            return cls()

    def save(self) -> None:
        _settings_path().write_text(
            json.dumps(asdict(self), indent=2), encoding="utf-8"
        )


def _settings_path() -> Path:
    return app_data_dir() / "settings.json"
