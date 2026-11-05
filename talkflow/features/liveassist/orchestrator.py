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
import threading
from typing import Callable

from talkflow.capture.vad import ChunkBoundaryDetector
from talkflow.features.liveassist.capture import TwoSourceRecorder
from talkflow.features.liveassist.multi_stream_asr import MultiStreamAsrAdapter
from talkflow.features.liveassist.session import LiveAssistSession, LiveAssistState
from talkflow.features.liveassist.store import save_session, save_utterance

logger = logging.getLogger(__name__)

_SOURCES = ("me", "they")
# ChunkBoundaryDetector's own safety net (max_ms_between_boundaries) only
# fires if it has EVER seen a speech frame since the last boundary - if a
# source's audio never once classifies as speech (very plausible for
# system/music audio on "they", or long quiet stretches on "me"),
# _heard_speech_since_boundary stays False and NEITHER of its boundary
# conditions can ever trigger, so the sherpa-onnx stream for that source
# never finalizes and keeps accumulating state for the rest of the session -
# confirmed live: this produced unbounded memory growth (22GB+) and CPU
# climbing over a multi-minute session. This hard cap is independent of
# VAD ever detecting speech at all, so no stream can run forever regardless
# of how unreliable VAD is on a given source.
_HARD_CAP_FRAMES = 10_000 // 30  # ~10s of 30ms frames


class LiveAssistOrchestrator:
    def __init__(
        self,
        asr: MultiStreamAsrAdapter | None = None,
        allowed_languages: list[str] | None = None,
        on_state_change: Callable[[LiveAssistSession], None] | None = None,
        on_utterance: Callable[[str, str, bool], None] | None = None,
        recorder: TwoSourceRecorder | None = None,
        asr_factory: Callable[[], MultiStreamAsrAdapter] | None = None,
    ):
        if asr is None and asr_factory is None:
            raise ValueError("LiveAssistOrchestrator requires either asr or asr_factory")
        # Injectable (asr=) so tests can supply a fixed fake instance used for
        # the whole test, same shape as the recorder below. In production
        # app.py passes asr_factory= instead: start() calls it fresh every
        # session and stop() closes whatever it built. This fixes a real bug
        # - previously the orchestrator itself (cached in app.py for the
        # process's lifetime) held one `self.asr` set at construction time
        # forever, so clearing app.py's own adapter cache alone did nothing;
        # the already-built orchestrator never saw a new adapter. A fresh
        # adapter instance per session also means no stale-session WebSocket
        # callback can affect the next session - there's no shared mutable
        # state to race on, which is simpler than threading a generation id
        # through every adapter's internal callbacks.
        self._injected_asr = asr
        self._asr_factory = asr_factory
        # Not built eagerly here when not injected - start() builds it
        # (inside its own try/except, surfacing a bad config as a visible
        # ERROR state rather than an exception out of the constructor) so
        # construction never touches real devices/network at all when the
        # caller intends to use the factory path. `self.asr`/`self._recorder`
        # are None until the first start() in that case.
        self.asr = asr
        self.allowed_languages = allowed_languages or []
        self.on_state_change = on_state_change
        self.on_utterance = on_utterance  # (source_id, text, is_final) -> None, for the UI later
        self._session: LiveAssistSession | None = None
        # Injectable so tests can supply a fake instead of opening real
        # audio devices (TwoSourceRecorder() touches sc.default_microphone()
        # at construction time). None in production: start() then builds a
        # FRESH TwoSourceRecorder() every session instead of reusing the one
        # built here - confirmed live that reusing it meant a changed
        # live_assist_system_audio_process_name setting never took effect
        # until the whole app was restarted, since app.py's orchestrator is
        # itself a cached singleton for the process's lifetime.
        self._injected_recorder = recorder
        self._recorder = recorder
        self._boundaries = {source: ChunkBoundaryDetector() for source in _SOURCES}
        self._sequence = {source: 0 for source in _SOURCES}
        self._frames_since_finalize = {source: 0 for source in _SOURCES}
        # Guards each source's finalize against running concurrently from
        # two threads at once (a hard-cap/VAD-boundary finalize triggered
        # from the capture drain thread, racing stop()'s own explicit
        # finalize call from whatever thread the hotkey/UI runs on) -
        # confirmed by review as a real risk for the realtime adapter's
        # single-connection commit/wait state, not just a theoretical one.
        self._finalize_locks = {source: threading.Lock() for source in _SOURCES}

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
        try:
            if self._injected_recorder is None:
                self._recorder = TwoSourceRecorder()
            if self._injected_asr is None:
                self.asr = self._asr_factory()
        except Exception:
            # An explicit, invalid device/API-key override must fail loud
            # here rather than the exception propagating unhandled out of
            # whatever thread called start() (a hotkey callback) - see
            # capture.py's _resolve_output_loopback for the "fail loud, no
            # silent device substitution" rationale this surfaces.
            logger.exception("Live Assist: failed to start (device or ASR adapter setup)")
            self._session.finish(LiveAssistState.ERROR)
            save_session(self._session)
            self._emit()
            return
        # Fresh per-session state - this orchestrator instance is reused
        # across many start/stop cycles for the process's lifetime, so
        # leftover VAD boundaries/sequence numbers/hard-cap counters from a
        # previous session must not leak into this one.
        self._boundaries = {source: ChunkBoundaryDetector() for source in _SOURCES}
        self._sequence = {source: 0 for source in _SOURCES}
        self._frames_since_finalize = {source: 0 for source in _SOURCES}
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
        try:
            # Finalize each source independently - one source's finalize
            # failing must not drop the other's already-decoded final text.
            for source in _SOURCES:
                try:
                    self._finalize_source(source, rearm=False)
                except Exception:
                    logger.exception("Live Assist: finalizing source=%s failed", source)
        finally:
            # Always close this session's adapter connections, even if a
            # finalize call above raised - matches the recorder's own
            # cleanup, and means the realtime adapter's WebSockets don't
            # outlive the session that opened them.
            if hasattr(self.asr, "close"):
                try:
                    self.asr.close()
                except Exception:
                    logger.exception("Live Assist: closing ASR adapter failed")
        self._session.finish(LiveAssistState.ENDED)
        save_session(self._session)
        self._emit()

    def _on_frame(self, source_id: str, pcm: bytes) -> None:
        if self._session is None or self._session.state not in (
            LiveAssistState.ARMING, LiveAssistState.ACTIVE,
        ):
            return  # PAUSED (or ending/ended) - drop frames, don't forward or bill
        boundary = self._boundaries[source_id].feed_frame(pcm)
        self._frames_since_finalize[source_id] += 1
        partial = self.asr.feed_source_chunk(source_id, pcm)
        if partial and partial.text:
            if self.on_utterance:
                self.on_utterance(source_id, partial.text, False)
        if boundary or self._frames_since_finalize[source_id] >= _HARD_CAP_FRAMES:
            self._finalize_source(source_id)

    def _finalize_source(self, source_id: str, rearm: bool = True) -> None:
        # A hard-cap/VAD-boundary finalize (from the capture drain thread)
        # can otherwise race stop()'s own explicit finalize call (from
        # whichever thread the hotkey/UI runs on) for the same source - both
        # calling into a single-connection adapter's commit/wait state at
        # once. The lock serializes them; if the racing call turns out
        # redundant (stream already finalized, nothing new fed to it since),
        # it harmlessly returns an empty result below, which the `if
        # final.text` check already skips saving.
        with self._finalize_locks[source_id]:
            self._frames_since_finalize[source_id] = 0
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
