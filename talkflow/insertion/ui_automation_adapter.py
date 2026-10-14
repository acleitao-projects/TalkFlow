"""UI Automation insertion via the `uiautomation` package. Tries to set the
focused control's value directly (faster, no clipboard disturbance) when the
control exposes ValuePattern; raises NotSupportedError otherwise so
InsertionService can fall back to the clipboard path. Coverage is inherently
inconsistent across apps (terminals, some Electron apps) - see the design notes' Risk 3.
"""
from __future__ import annotations

import uiautomation as auto

from talkflow.insertion.base import InsertionAdapter, InsertionOutcome


class NotSupportedError(Exception):
    pass


class UiAutomationAdapter(InsertionAdapter):
    def insert(self, text: str) -> InsertionOutcome:
        try:
            control = auto.GetFocusedControl()
            if control is None:
                raise NotSupportedError("no focused control")
            pattern = control.GetValuePattern()
            if pattern is None or not getattr(pattern, "IsReadOnly", False) is False:
                raise NotSupportedError("control has no writable ValuePattern")
            existing = pattern.Value or ""
            pattern.SetValue(existing + text)
            return InsertionOutcome(success=True, method="ui_automation")
        except NotSupportedError as e:
            return InsertionOutcome(success=False, method="ui_automation", error=str(e))
        except Exception as e:
            return InsertionOutcome(success=False, method="ui_automation", error=str(e))
