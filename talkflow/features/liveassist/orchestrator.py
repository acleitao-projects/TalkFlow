"""LiveAssistOrchestrator - session lifecycle (LA-03) now wired to real
two-source capture + multi-stream ASR (LA-04/LA-05): talkflow/pipeline/
orchestrator.py::Orchestrator's shape (mutable current-session object, state
machine, on_state_change callback, idempotent stop() guarded by current
state) extended with one TwoSourceRecorder + one MultiStreamAsrAdapter +
one ChunkBoundaryDetector per source, so "me" and "they" each get their own
interim->final utterance cycle without ever being mixed together.
"""
from __future__ import annotations

import logging
from typing import Callable

from talkflow.capture.vad import ChunkBoundaryDetector
from talkflow.features.liveassist.capture import TwoSourceRecorder
from talkflow.features.liveassist.multi_stream_asr import MultiStreamAsrAdapter
from talkflow.features.liveassist.session import LiveAssistSession, LiveAssistState
from talkflow.features.liveassist.store import save_session, save_utterance

logger = logging.getLogger(__name__)

_SOURCES = ("me", "they")


class LiveAssistOrchestrator:
    def __init__(
        self,
        asr: MultiStreamAsrAdapter,
        allowed_languages: list[str],
        on_state_change: Callable[[LiveAssistSession], None] | None = None,
        on_utterance: Callable[[str, str, bool], None] | None = None,
        recorder: TwoSourceRecorder | None = None,
    ):
        self.asr = asr
        self.allowed_languages = allowed_languages
        self.on_state_change = on_state_change
        self.on_utterance = on_utterance  # (source_id, text, is_final) -> None, for the UI later
        self._session: LiveAssistSession | None = None
        # Injectable so tests can supply a fake instead of opening real
        # audio devices (TwoSourceRecorder() touches sc.default_microphone()
        # at construction time).
        self._recorder = recorder if recorder is not None else TwoSourceRecorder()
        self._boundaries = {source: ChunkBoundaryDetector() for source in _SOURCES}
        self._sequence = {source: 0 for source in _SOURCES}

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
        for source in _SOURCES:
            self.asr.start_source(source, self.allowed_languages)
        self._recorder.start(on_frame=self._on_frame)
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
        self._recorder.stop()
        # Finalize each source independently - one source's finalize failing
        # must not drop the other's already-decoded final text.
        for source in _SOURCES:
            try:
                self._finalize_source(source, rearm=False)
            except Exception:
                logger.exception("Live Assist: finalizing source=%s failed", source)
        self._session.finish(LiveAssistState.ENDED)
        save_session(self._session)
        self._emit()

    def _on_frame(self, source_id: str, pcm: bytes) -> None:
        if self._session is None:
            return
        boundary = self._boundaries[source_id].feed_frame(pcm)
        partial = self.asr.feed_source_chunk(source_id, pcm)
        if partial and partial.text:
            if self.on_utterance:
                self.on_utterance(source_id, partial.text, False)
        if boundary:
            self._finalize_source(source_id)

    def _finalize_source(self, source_id: str, rearm: bool = True) -> None:
        final = self.asr.finalize_source(source_id)
        if rearm:
            # Re-arm immediately so the next utterance on this source has a
            # fresh stream - the session as a whole keeps running. Skipped
            # when called from stop() - the session is ending, no next
            # utterance is coming.
            self.asr.start_source(source_id, self.allowed_languages)
        if final.text and self._session:
            self._sequence[source_id] += 1
            save_utterance(
                session_id=self._session.id, source=source_id,
                sequence=self._sequence[source_id], text=final.text,
            )
            if self.on_utterance:
                self.on_utterance(source_id, final.text, True)

    def _emit(self) -> None:
        if self.on_state_change and self._session:
            self.on_state_change(self._session)
