"""Windows "launch at login" via a Shortcut in the per-user Startup folder -
no admin rights needed (unlike a scheduled task or a registry Run key under
HKLM). The shortcut launches TalkFlow.exe (a PyInstaller-compiled launcher
that shells out to the project's own .venv pythonw.exe - see
scripts/launch_talkflow_exe.py), so the same executable backs both the
Start Menu entry and autostart.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
EXE_PATH = PROJECT_ROOT / "TalkFlow.exe"


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
    shortcut.TargetPath = str(EXE_PATH)
    shortcut.WorkingDirectory = str(PROJECT_ROOT)
    shortcut.Description = "TalkFlow voice dictation"
    shortcut.Save()
