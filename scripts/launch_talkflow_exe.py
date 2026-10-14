"""Entry point compiled by PyInstaller into TalkFlow.exe. A thin launcher
(not a repackaging of the app itself - the real app keeps running from the
project's own .venv, same as launch_talkflow.vbs) so double-clicking an exe
can start TalkFlow without a visible console window, from the Desktop or a
Start Menu shortcut, same as the Startup-folder autostart path does already.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> None:
    if getattr(sys, "frozen", False):
        project_root = Path(sys.executable).resolve().parent
    else:
        project_root = Path(__file__).resolve().parent.parent

    pythonw = project_root / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0,
            f"Could not find {pythonw}\n\nTalkFlow.exe must stay in the project root "
            "(next to the .venv folder) or be given that path explicitly.",
            "TalkFlow launcher",
            0x10,  # MB_ICONERROR
        )
        sys.exit(1)

    subprocess.Popen(
        [str(pythonw), "-m", "talkflow.app"],
        cwd=str(project_root),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


if __name__ == "__main__":
    main()
