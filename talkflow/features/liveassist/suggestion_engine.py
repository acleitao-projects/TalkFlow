"""LA-09: live suggestion engine. Runs LlmAdapter.suggest() on a background
thread (the orchestrator's caller - the hotkey handler or overlay click -
must not block), tagged with a monotonically increasing request id so a
second manual ask supersedes a still-streaming first one: a stale result
arriving after a newer request started is dropped rather than overwriting
the UI with outdated text.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from talkflow.features.liveassist import assistants as assistant_store
from talkflow.features.liveassist import store
from talkflow.llm.base import LlmAdapter

logger = logging.getLogger(__name__)

_GENERAL_ASSISTANT_FALLBACK = ("General Assistant", "Help the user in their conversation.", "Clear and direct.", None)


class SuggestionEngine:
    def __init__(self, llm: LlmAdapter, on_result: Callable[[str, bool], None]):
        self.llm = llm
        # (suggestion_text, has_suggestion) -> None, called on the main
        # thread's behalf by whichever background thread's request is still
        # current when it finishes.
        self.on_result = on_result
        self._lock = threading.Lock()
        self._latest_request_id = 0

    def request(self, session_id: str, assistant_id: str | None, language: str, trigger: str = "manual") -> None:
        with self._lock:
            self._latest_request_id += 1
            my_request_id = self._latest_request_id

        def _run() -> None:
            try:
                transcript_lines = store.list_utterances(session_id)
                name, task, style, custom_prompt = self._resolve_assistant(assistant_id)
                result = self.llm.suggest(transcript_lines, name, task, style, language, custom_prompt)
            except Exception:
                logger.exception("Live Assist: suggestion request failed")
                result = None
            with self._lock:
                if my_request_id != self._latest_request_id:
                    # A newer ask superseded this one while it was in
                    # flight - drop the stale result instead of showing it.
                    return
            if result is None:
                self.on_result("", False)
                return
            if result.has_suggestion:
                store.save_suggestion(session_id, trigger=trigger, text=result.suggestion_text)
            self.on_result(result.suggestion_text, result.has_suggestion)

        threading.Thread(target=_run, daemon=True).start()

    @staticmethod
    def _resolve_assistant(assistant_id: str | None) -> tuple[str, str, str, str | None]:
        if assistant_id:
            assistant = assistant_store.get_assistant(assistant_id)
            if assistant:
                return assistant.name, assistant.task, assistant.response_style, assistant.custom_system_prompt
        assistants = assistant_store.list_assistants()
        if assistants:
            a = assistants[0]
            return a.name, a.task, a.response_style, a.custom_system_prompt
        return _GENERAL_ASSISTANT_FALLBACK
