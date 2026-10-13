"""DictationSession - the single object threaded through capture -> ASR ->
LLM cleanup -> insertion -> history. Raw and cleaned transcripts are always
kept as separate fields; neither stage is allowed to overwrite the other.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Literal


class SessionState(str, Enum):
    ARMING = "arming"
    RECORDING = "recording"
    STOPPING = "stopping"
    TRANSCRIBING = "transcribing"
    FORMATTING = "formatting"
    INSERTING = "inserting"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass
class DictationSession:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None
    state: SessionState = SessionState.ARMING

    input_device_id: str | None = None
    trigger_mode: Literal["hold", "toggle"] = "hold"
    allowed_languages: list[str] = field(default_factory=lambda: ["en"])

    detected_language: str | None = None
    detected_language_confidence: float | None = None

    raw_transcript: str | None = None
    cleaned_transcript: str | None = None
    applied_preset_id: str | None = None

    target_process_name: str | None = None
    target_window_title: str | None = None

    insertion_outcome: str | None = None
    error_code: str | None = None

    def finish(self, state: SessionState) -> None:
        self.state = state
        self.ended_at = datetime.now(timezone.utc)
