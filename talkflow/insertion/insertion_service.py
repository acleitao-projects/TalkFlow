from __future__ import annotations

from talkflow.insertion.base import InsertionOutcome
from talkflow.insertion.clipboard_adapter import ClipboardAdapter
from talkflow.insertion.ui_automation_adapter import UiAutomationAdapter


class InsertionService:
    """Clipboard-paste is primary (always available); UI Automation is tried
    first only when explicitly enabled per-app once confirmed reliable there
    (see CLAUDE.md Risk 3) - disabled by default for the v1 vertical slice."""

    def __init__(self, prefer_ui_automation: bool = False):
        self._ui = UiAutomationAdapter()
        self._clipboard = ClipboardAdapter()
        self.prefer_ui_automation = prefer_ui_automation

    def begin_session(self) -> None:
        """Bracket a dictation pasted in several pieces: the clipboard is
        snapshotted here and restored once by end_session()."""
        self._clipboard.begin_session()

    def end_session(self) -> None:
        self._clipboard.end_session()

    def insert(self, text: str) -> InsertionOutcome:
        if self.prefer_ui_automation:
            outcome = self._ui.insert(text)
            if outcome.success:
                return outcome
        return self._clipboard.insert(text)
