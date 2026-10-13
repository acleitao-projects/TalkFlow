"""Transforms: on-demand rewrite of text ALREADY sitting in the focused field
(polish/restructure/custom instruction), separate from the live dictation
pipeline. Invoked by its own shortcut. Mechanism: select-all + copy to read
the field's current content (best-effort, since there's no universal "get
field text" API without UI Automation support), send it + the transform's
instruction to the cleanup-stage LLM, then replace the selection with the
result via the clipboard insertion path.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import win32clipboard
import win32con
from pynput.keyboard import Controller, Key

from talkflow.features.transforms.store import list_custom_transforms
from talkflow.insertion.clipboard_adapter import ClipboardAdapter
from talkflow.insertion.insertion_service import InsertionService
from talkflow.llm.base import LlmAdapter

BUILTIN_TRANSFORMS = {
    "polish": "Polish this text: fix grammar, tighten wording, keep the author's voice and meaning intact. Do not add new content.",
    "prompt_engineer": "Rewrite this as a clear, well-structured prompt for an AI assistant: state the goal, relevant context, and desired output format.",
}


@dataclass
class TransformResult:
    success: bool
    original_text: str | None
    new_text: str | None
    error: str | None = None


class TransformsEngine:
    def __init__(self, llm: LlmAdapter, insertion: InsertionService | None = None):
        self.llm = llm
        self.insertion = insertion or InsertionService()
        self._kb = Controller()

    def run(self, transform_key: str, custom_instruction: str | None = None) -> TransformResult:
        instruction = custom_instruction or BUILTIN_TRANSFORMS.get(transform_key)
        if not instruction:
            # Not a built-in - try resolving it as a custom transform's id.
            for ct in list_custom_transforms():
                if ct.id == transform_key:
                    instruction = ct.instruction
                    break
        if not instruction:
            return TransformResult(False, None, None, error=f"Unknown transform: {transform_key}")

        original = self._read_selection()
        if not original:
            return TransformResult(False, None, None, error="No text selected / selection empty")

        try:
            result = self.llm.rewrite(original, instruction)
        except Exception as e:
            return TransformResult(False, original, None, error=str(e))

        outcome = self.insertion.insert(result.cleaned_text)
        if not outcome.success:
            return TransformResult(False, original, result.cleaned_text, error=outcome.error)
        return TransformResult(True, original, result.cleaned_text)

    def _read_selection(self) -> str | None:
        previous = ClipboardAdapter._read_clipboard_text()
        try:
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.CloseClipboard()
        except Exception:
            pass

        with self._kb.pressed(Key.ctrl):
            self._kb.press("c")
            self._kb.release("c")
        time.sleep(0.15)

        selected = ClipboardAdapter._read_clipboard_text()

        if previous is not None:
            try:
                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, previous)
                win32clipboard.CloseClipboard()
            except Exception:
                pass
        return selected
