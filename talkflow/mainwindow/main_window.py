from __future__ import annotations

from pathlib import Path

import webview

from talkflow.mainwindow.api import Api

_HTML_PATH = Path(__file__).parent / "web" / "index.html"
MIN_WIDTH = 720
MIN_HEIGHT = 480


class MainWindow:
    def __init__(self):
        self._window: webview.Window | None = None
        self.api = Api()
        self.api.main_window = self

    def create(self) -> webview.Window:
        self._window = webview.create_window(
            title="TalkFlow",
            url=_HTML_PATH.as_uri(),
            width=1040,
            height=680,
            min_size=(MIN_WIDTH, MIN_HEIGHT),
            js_api=self.api,
            hidden=True,
            # Frameless so the HTML title bar (collapse/minimize/maximize/
            # close) replaces the native Windows chrome - see index.html's
            # #titlebar. Closing it hides to tray instead of destroying it
            # (wired via the close button calling hide_main_window(), not
            # window.destroy()) so re-opening from the tray still works.
            frameless=True,
            easy_drag=True,
        )
        return self._window

    def show(self) -> None:
        if self._window:
            self._window.show()

    def hide(self) -> None:
        if self._window:
            self._window.hide()

    def minimize(self) -> None:
        if self._window:
            self._window.minimize()

    def maximize(self) -> None:
        if self._window:
            self._window.maximize()

    def restore(self) -> None:
        if self._window:
            self._window.restore()

    def resize(self, width: int, height: int) -> None:
        if self._window:
            self._window.resize(max(MIN_WIDTH, int(width)), max(MIN_HEIGHT, int(height)))
