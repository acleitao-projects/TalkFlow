"""Clipboard-paste insertion: snapshot current clipboard (text, best-effort),
set the cleaned text, synthesize Ctrl+V, restore the original clipboard.
This is the PRIMARY, always-available insertion path (see CLAUDE.md Risk 3)
- UI Automation is layered in later as a per-app enhancement, not required
for v1.

The restore runs on a background thread so the caller gets control back as
soon as the paste is sent. For a dictation pasted in several pieces
(partial paste), begin_session()/end_session() bracket the pieces so the
clipboard is snapshotted once and restored once, not around every piece.
"""
from __future__ import annotations

import threading
import time

import win32clipboard
import win32con
from pynput.keyboard import Controller, Key

from talkflow.insertion.base import InsertionAdapter, InsertionOutcome

_PASTE_SETTLE_S = 0.03
_RESTORE_DELAY_S = 0.35


class ClipboardAdapter(InsertionAdapter):
    def __init__(self):
        self._kb = Controller()
        self._lock = threading.Lock()
        self._in_session = False
        self._session_previous: str | None = None
        self._generation = 0  # bumped per paste; a pending restore yields to a newer paste

    def begin_session(self) -> None:
        with self._lock:
            if self._in_session:
                return
            try:
                self._session_previous = self._read_clipboard_text()
            except Exception:
                self._session_previous = None
            self._in_session = True

    def end_session(self) -> None:
        with self._lock:
            if not self._in_session:
                return
            self._in_session = False
            previous, self._session_previous = self._session_previous, None
            generation = self._generation
        self._restore_later(previous, generation)

    def insert(self, text: str) -> InsertionOutcome:
        with self._lock:
            in_session = self._in_session
            self._generation += 1
            generation = self._generation

        previous = None
        if not in_session:
            try:
                previous = self._read_clipboard_text()
            except Exception:
                previous = None

        try:
            self._set_clipboard_text(text)
        except Exception as e:
            return InsertionOutcome(success=False, method="clipboard", error=f"set clipboard failed: {e}")

        time.sleep(_PASTE_SETTLE_S)
        try:
            with self._kb.pressed(Key.ctrl):
                self._kb.press("v")
                self._kb.release("v")
        except Exception as e:
            return InsertionOutcome(success=False, method="clipboard", error=f"paste send failed: {e}")

        if not in_session:
            self._restore_later(previous, generation)
        return InsertionOutcome(success=True, method="clipboard")

    def _restore_later(self, previous: str | None, generation: int) -> None:
        if previous is None:
            return

        def _restore() -> None:
            # The target app reads the clipboard asynchronously after Ctrl+V.
            time.sleep(_RESTORE_DELAY_S)
            with self._lock:
                if self._generation != generation or self._in_session:
                    return  # a newer paste owns the clipboard now
            try:
                self._set_clipboard_text(previous)
            except Exception:
                pass  # best-effort restore; insertion itself already succeeded

        threading.Thread(target=_restore, daemon=True).start()

    @staticmethod
    def _read_clipboard_text() -> str | None:
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            return None
        finally:
            win32clipboard.CloseClipboard()

    @staticmethod
    def _set_clipboard_text(text: str) -> None:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()
