from __future__ import annotations

import threading
from typing import Callable

from PIL import Image, ImageDraw
import pystray


def _make_icon_image() -> Image.Image:
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
        items = [pystray.MenuItem("Open TalkFlow", lambda: on_open_main())]
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
