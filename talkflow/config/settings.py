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
    language_allowlist: list[str] = field(default_factory=lambda: ["en"])
    input_device_id: str | None = None  # None = system default
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
