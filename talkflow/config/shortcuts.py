"""Windows shortcuts (.lnk) that launch TalkFlow - used for the Start Menu
entry (scripts/create_start_menu_shortcut.py) and launch-at-login
(autostart.py). No admin rights needed: both live under the per-user
%APPDATA%\\Microsoft\\Windows\\Start Menu.

Each shortcut carries TalkFlow's AppUserModelID (the same one app.py sets on
the running process), so a pinned taskbar button, the Start Menu entry and
the running window are one taskbar item with TalkFlow's icon instead of
Python's.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_USER_MODEL_ID = "TalkFlow.App"

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
EXE_PATH = PROJECT_ROOT / "TalkFlow.exe"  # optional PyInstaller launcher (scripts/launch_talkflow_exe.py)
PYTHONW_PATH = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
ICON_PATH = PROJECT_ROOT / "talkflow" / "assets" / "icon.ico"


def start_menu_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _launch_command() -> tuple[str, str]:
    """(target, arguments). The compiled launcher if one was built, otherwise
    the project's own .venv pythonw.exe (no console window)."""
    if EXE_PATH.exists():
        return str(EXE_PATH), ""
    return str(PYTHONW_PATH), "-m talkflow.app"


def create_shortcut(path: Path) -> None:
    import pythoncom
    import win32com.client
    from win32com.propsys import propsys, pscon
    from win32com.shell import shellcon

    path.parent.mkdir(parents=True, exist_ok=True)
    target, args = _launch_command()
    shortcut = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(path))
    shortcut.TargetPath = target
    shortcut.Arguments = args
    shortcut.WorkingDirectory = str(PROJECT_ROOT)
    shortcut.Description = "TalkFlow voice dictation"
    if ICON_PATH.exists():
        shortcut.IconLocation = f"{ICON_PATH},0"
    shortcut.Save()
    del shortcut  # releases the file; the property store below needs write access

    store = propsys.SHGetPropertyStoreFromParsingName(
        str(path), None, shellcon.GPS_READWRITE, propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID,
                   propsys.PROPVARIANTType(APP_USER_MODEL_ID, pythoncom.VT_LPWSTR))
    store.Commit()
