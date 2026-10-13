from __future__ import annotations

from pathlib import Path

import webview

from talkflow.mainwindow.api import Api

_HTML_PATH = Path(__file__).parent / "web" / "index.html"


class MainWindow:
    def __init__(self):
        self._window: webview.Window | None = None
        self.api = Api()

    def create(self) -> webview.Window:
        self._window = webview.create_window(
            title="TalkFlow",
            url=_HTML_PATH.as_uri(),
            width=960,
            height=640,
            js_api=self.api,
            hidden=True,
        )
        return self._window

    def show(self) -> None:
        if self._window:
            self._window.show()

    def hide(self) -> None:
        if self._window:
            self._window.hide()
