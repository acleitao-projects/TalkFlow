from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw
import pystray

_ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "icon.png"


def _make_icon_image() -> Image.Image:
    if _ICON_PATH.exists():
        return Image.open(_ICON_PATH).convert("RGBA")
    # Fallback if assets/icon.png is missing (e.g. a dev checkout without it).
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(30, 30, 35, 255))
    d.ellipse((24, 14, 40, 38), fill=(240, 240, 245, 255))
    d.rectangle((28, 36, 36, 46), fill=(240, 240, 245, 255))
    return img


class TrayIcon:
    def __init__(self, on_open_main: Callable[[], None], on_quit: Callable[[], None],
                 on_toggle_meeting: Callable[[], None] | None = None,
                 meeting_active: Callable[[], bool] | None = None):
        self._meeting_active = meeting_active or (lambda: False)
        # default=True makes this the action triggered by double-clicking
        # the tray icon (Windows' MFS_DEFAULT), not just a menu entry.
        items = [pystray.MenuItem("Open TalkFlow", lambda: on_open_main(), default=True)]
        if on_toggle_meeting:
            items.append(pystray.MenuItem(
                lambda item: "Stop Meeting Recording" if self._meeting_active() else "Start Meeting Recording",
                lambda: on_toggle_meeting(),
            ))
        items.append(pystray.MenuItem("Quit", lambda: self._quit(on_quit)))
        self._icon = pystray.Icon("talkflow", _make_icon_image(), "TalkFlow", menu=pystray.Menu(*items))

    def _quit(self, on_quit: Callable[[], None]) -> None:
        on_quit()
        self._icon.stop()

    def run_in_background(self) -> threading.Thread:
        t = threading.Thread(target=self._icon.run, daemon=True)
        t.start()
        return t
