"""Filesystem locations TalkFlow uses, all under the user's home directory so
nothing requires admin rights to create or write.

Deliberately NOT under %LOCALAPPDATA%: a process started from a packaged
(MSIX) app - e.g. a terminal inside the Claude desktop app - gets AppData
silently redirected to a private per-package copy, so the app launched from
there and the app launched from a normal terminal each had their own
settings.json / talkflow.db (different model registry, hotkey, history).
The home directory is not redirected, so every launcher sees the same data.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

APP_NAME = "TalkFlow"


def _legacy_app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME


def app_data_dir() -> Path:
    path = Path.home() / ".talkflow"
    if not path.exists():
        legacy = _legacy_app_data_dir()
        if legacy.is_dir():
            # One-time move from the old location (as seen by this process).
            shutil.copytree(legacy, path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return app_data_dir() / "talkflow.db"


def log_dir() -> Path:
    path = app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def tmp_audio_dir() -> Path:
    path = app_data_dir() / "tmp_audio"
    path.mkdir(parents=True, exist_ok=True)
    return path
