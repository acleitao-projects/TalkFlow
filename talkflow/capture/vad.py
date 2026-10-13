"""Voice-activity based chunk-boundary detection: decides WHEN to trigger a
rolling re-transcription (on a natural pause, or periodically as a safety net
during long continuous speech), not where to split audio into separate
independent segments - the ASR adapter keeps growing its own session buffer.
"""
from __future__ import annotations

import webrtcvad

FRAME_MS = 30  # webrtcvad supports 10/20/30ms frames
SAMPLE_RATE = 16000
FRAME_BYTES = int(SAMPLE_RATE * (FRAME_MS / 1000.0)) * 2  # 16-bit mono


class ChunkBoundaryDetector:
    def __init__(self, aggressiveness: int = 2, silence_ms_for_boundary: int = 400,
                 max_ms_between_boundaries: int = 3000):
        self._vad = webrtcvad.Vad(aggressiveness)
        self._silence_frames_needed = max(1, silence_ms_for_boundary // FRAME_MS)
        self._max_frames_between = max(1, max_ms_between_boundaries // FRAME_MS)
        self._consecutive_silence = 0
        self._frames_since_boundary = 0
        self._heard_speech_since_boundary = False
        # Read by the orchestrator's partial-paste path after each feed_frame().
        self.last_frame_was_speech = False
        self.last_boundary_was_silence = False  # False = forced by the max-interval safety net
        self._silence_run = 0  # unlike _consecutive_silence, not reset at a boundary

    @property
    def trailing_silence_ms(self) -> int:
        return self._silence_run * FRAME_MS

    def feed_frame(self, frame: bytes) -> bool:
        """Feed one FRAME_BYTES-sized 16kHz mono int16 frame. Returns True if
        a chunk boundary (time to re-transcribe) has just been reached."""
        if len(frame) != FRAME_BYTES:
            return False
        is_speech = self._vad.is_speech(frame, SAMPLE_RATE)
        self.last_frame_was_speech = is_speech
        self._frames_since_boundary += 1

        if is_speech:
            self._consecutive_silence = 0
            self._silence_run = 0
            self._heard_speech_since_boundary = True
        else:
            self._consecutive_silence += 1
            self._silence_run += 1

        boundary = False
        if self._heard_speech_since_boundary and self._consecutive_silence >= self._silence_frames_needed:
            boundary = True
            self.last_boundary_was_silence = True
        elif self._frames_since_boundary >= self._max_frames_between and self._heard_speech_since_boundary:
            boundary = True
            self.last_boundary_was_silence = False

        if boundary:
            self._frames_since_boundary = 0
            self._consecutive_silence = 0
            self._heard_speech_since_boundary = False
        return boundary
