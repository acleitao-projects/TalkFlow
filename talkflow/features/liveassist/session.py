"""LiveAssistSession - the state/identity object for one Live Assist session,
modelled directly on talkflow/pipeline/session.py::DictationSession. This is
the LA-03 skeleton: no transcript/suggestion fields yet (those arrive with
LA-05/LA-09) - just enough to prove the session lifecycle and persistence.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class LiveAssistState(str, Enum):
    ARMING = "arming"
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPING = "stopping"
    ENDED = "ended"
    ERROR = "error"


@dataclass
class LiveAssistSession:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None
    state: LiveAssistState = LiveAssistState.ARMING
    assistant_id: str | None = None
    language: str | None = None
    goal: str | None = None

    def finish(self, state: LiveAssistState) -> None:
        self.state = state
        self.ended_at = datetime.now(timezone.utc)
