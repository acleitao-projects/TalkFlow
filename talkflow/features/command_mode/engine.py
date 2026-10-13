"""Command Mode: hold its own shortcut, speak an INSTRUCTION (not content to
transcribe verbatim), release -> the instruction either edits/drafts text in
the focused field or runs a web search. No trigger phrase; requires a
focused editable field for edit/draft. Reuses the same ASR adapter as
ordinary dictation (just a short one-shot recording, no rolling chunks
needed since instructions are short) and the LLM's generic rewrite().
"""
from __future__ import annotations

import logging
import webbrowser
from dataclasses import dataclass
from urllib.parse import quote_plus

from talkflow.asr.base import AsrAdapter
from talkflow.capture.recorder import AudioRecorder
from talkflow.features.command_mode.intents import Intent, classify, extract_search_query
from talkflow.features.transforms.engine import TransformsEngine
from talkflow.insertion.insertion_service import InsertionService
from talkflow.llm.base import LlmAdapter

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    success: bool
    intent: str | None = None
    instruction: str | None = None
    detail: str | None = None
    error: str | None = None


class CommandModeEngine:
    def __init__(self, asr: AsrAdapter, llm: LlmAdapter, insertion: InsertionService | None = None):
        self.asr = asr
        self.llm = llm
        self.insertion = insertion or InsertionService()
        self._transforms = TransformsEngine(llm, self.insertion)
        self._recorder: AudioRecorder | None = None

    def start(self) -> None:
        self.asr.start_session(["en"])  # instructions are typically spoken in the UI's configured language
        self._recorder = AudioRecorder()
        self._recorder.start(on_frame=lambda frame: None)  # no rolling transcription needed for short instructions

    def stop(self) -> CommandResult:
        if not self._recorder:
            return CommandResult(False, error="not recording")
        pcm = self._recorder.stop()
        self._recorder = None

        try:
            self.asr.feed_chunk(pcm, sample_rate=16000)
            final = self.asr.finalize()
        except Exception as e:
            return CommandResult(False, error=f"transcription failed: {e}")

        instruction = final.text.strip()
        if not instruction:
            return CommandResult(False, error="no speech detected")

        intent = classify(instruction)
        if intent == Intent.SEARCH:
            query = extract_search_query(instruction)
            url = f"https://www.google.com/search?q={quote_plus(query)}"
            webbrowser.open(url)
            return CommandResult(True, intent=intent.value, instruction=instruction, detail=url)

        # EDIT (covers both "edit existing text" and "draft new text" - the
        # transforms engine's _read_selection naturally returns None/empty
        # when nothing is selected, and rewrite() handles that case).
        selection = self._transforms._read_selection() or ""
        prompt_text = selection if selection else "(no existing text - compose new content from the instruction alone)"
        try:
            result = self.llm.rewrite(prompt_text, instruction)
        except Exception as e:
            return CommandResult(False, intent=intent.value, instruction=instruction, error=str(e))

        outcome = self.insertion.insert(result.cleaned_text)
        if not outcome.success:
            return CommandResult(False, intent=intent.value, instruction=instruction, error=outcome.error)
        return CommandResult(True, intent=intent.value, instruction=instruction, detail=result.cleaned_text)
