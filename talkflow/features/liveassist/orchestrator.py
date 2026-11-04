"""LiveAssistOrchestrator - session lifecycle skeleton (LA-03), modelled on
talkflow/pipeline/orchestrator.py::Orchestrator's shape: a mutable current-
session object, a state machine, an on_state_change callback, and idempotent
stop() guarded by current state. No real capture/ASR/suggestion logic exists
yet (LA-04/LA-05/LA-09) - start()'s "arm" step is a no-op transition, proving
the state machine and persistence without pretending a delay that doesn't
exist yet.
"""
from __future__ import annotations

from typing import Callable

from talkflow.features.liveassist.session import LiveAssistSession, LiveAssistState
from talkflow.features.liveassist.store import save_session


class LiveAssistOrchestrator:
    def __init__(self, on_state_change: Callable[[LiveAssistSession], None] | None = None):
        self.on_state_change = on_state_change
        self._session: LiveAssistSession | None = None

    @property
    def state(self) -> LiveAssistState | None:
        return self._session.state if self._session else None

    def start(self) -> None:
        if self._session is not None and self._session.state in (
            LiveAssistState.ARMING, LiveAssistState.ACTIVE, LiveAssistState.PAUSED,
        ):
            return  # already running - idempotent
        self._session = LiveAssistSession(state=LiveAssistState.ARMING)
        save_session(self._session)
        self._emit()
        self._session.state = LiveAssistState.ACTIVE
        save_session(self._session)
        self._emit()

    def pause(self) -> None:
        if self._session and self._session.state == LiveAssistState.ACTIVE:
            self._session.state = LiveAssistState.PAUSED
            save_session(self._session)
            self._emit()

    def resume(self) -> None:
        if self._session and self._session.state == LiveAssistState.PAUSED:
            self._session.state = LiveAssistState.ACTIVE
            save_session(self._session)
            self._emit()

    def stop(self) -> None:
        if self._session is None or self._session.state in (LiveAssistState.ENDED, LiveAssistState.ERROR):
            return  # idempotent - already stopped
        self._session.state = LiveAssistState.STOPPING
        save_session(self._session)
        self._emit()
        self._session.finish(LiveAssistState.ENDED)
        save_session(self._session)
        self._emit()

    def _emit(self) -> None:
        if self.on_state_change and self._session:
            self.on_state_change(self._session)
