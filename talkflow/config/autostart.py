"""Windows "launch at login" via a Shortcut in the per-user Startup folder -
no admin rights needed (unlike a scheduled task or a registry Run key under
HKLM). The shortcut is built by talkflow/config/shortcuts.py, the same as the
Start Menu entry.
"""
from __future__ import annotations

from pathlib import Path

from talkflow.config import shortcuts


def _shortcut_path() -> Path:
    return shortcuts.start_menu_dir() / "Startup" / "TalkFlow.lnk"


def is_enabled() -> bool:
    return _shortcut_path().exists()


def set_enabled(enabled: bool) -> None:
    path = _shortcut_path()
    if not enabled:
        path.unlink(missing_ok=True)
        return
    shortcuts.create_shortcut(path)
