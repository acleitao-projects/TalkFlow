"""Windows "launch at login" via a Shortcut in the per-user Startup folder -
no admin rights needed (unlike a scheduled task or a registry Run key under
HKLM). The shortcut launches scripts/launch_talkflow.vbs, which runs the app
through pythonw.exe (no console window) - see that file's docstring.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
VBS_PATH = PROJECT_ROOT / "scripts" / "launch_talkflow.vbs"


def _startup_dir() -> Path:
    appdata = os.environ["APPDATA"]  # Roaming - standard per-user Startup location
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _shortcut_path() -> Path:
    return _startup_dir() / "TalkFlow.lnk"


def is_enabled() -> bool:
    return _shortcut_path().exists()


def set_enabled(enabled: bool) -> None:
    path = _shortcut_path()
    if not enabled:
        path.unlink(missing_ok=True)
        return
    import win32com.client
    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortcut(str(path))
    shortcut.TargetPath = str(Path("C:/Windows/System32/wscript.exe"))
    shortcut.Arguments = f'"{VBS_PATH}"'
    shortcut.WorkingDirectory = str(PROJECT_ROOT)
    shortcut.Description = "TalkFlow voice dictation"
    shortcut.Save()
