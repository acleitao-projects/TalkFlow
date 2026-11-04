"""TwoSourceRecorder - LA-04: mic ("me") and system-output loopback ("they")
captured as two genuinely SEPARATE PCM streams, never summed. Modelled on
talkflow/features/notetaker/capture.py::MeetingRecorder's soundcard usage,
but split into two independent threads instead of one mixed loop - that
file's `mixed = mic_chunk + loop_chunk` is exactly what this must avoid.

Deliberately not reusing talkflow/capture/recorder.py::AudioRecorder either -
its warm-stream-reuse design is tuned for hold-to-talk dictation, with no
notion of an indefinite two-source session. It IS reusing that module's
producer/consumer split, though - see below.

Each source gets its OWN capture thread that does nothing but poll the
device and enqueue raw PCM, plus its own worker thread that drains that
queue and calls on_frame(). Confirmed live that this separation matters: an
earlier version called on_frame() (which runs straight into ASR decode)
inline on the same thread that polls WASAPI. Under two concurrent sherpa-onnx
decode_stream() calls competing for the GIL, that stalled each capture
thread's ability to service the hardware promptly, surfacing as hundreds of
"data discontinuity in recording" warnings per session and garbled
transcripts - the exact failure mode talkflow/capture/recorder.py's
callback-thread/drain-thread split (_callback only enqueues, _drain_loop
does the work) already avoids for dictation's single mic stream.
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME_SAMPLES = int(SAMPLE_RATE * 0.03)  # 480 - matches vad.py's FRAME_BYTES (960 bytes / 2)
POLL_SAMPLES = int(SAMPLE_RATE * 0.1)  # 1600 - 100ms; bigger than the VAD frame so
# soundcard/WASAPI isn't polled more often than it reliably delivers.

OnFrame = Callable[[str, bytes], None]  # (source_id, pcm16_bytes)
_SENTINEL = object()


class TwoSourceRecorder:
    def __init__(self):
        # Local import: soundcard initializes COM on Windows at import time,
        # which conflicts with comtypes/uiautomation's own CoInitializeEx if
        # it happens first at module-import time (observed directly - it
        # broke other test files' collection). Importing lazily, only when a
        # TwoSourceRecorder is actually constructed, avoids that ordering
        # hazard, same as sherpa_onnx's local import in the ASR adapters.
        import soundcard as sc

        self._mic = sc.default_microphone()
        self._loopback = sc.get_microphone(sc.default_speaker().name, include_loopback=True)
        self._running = False
        self._capture_threads: list[threading.Thread] = []
        self._worker_threads: list[threading.Thread] = []
        self._queues: dict[str, queue.Queue] = {}

    def start(self, on_frame: OnFrame) -> None:
        self._running = True
        self._queues = {"me": queue.Queue(), "they": queue.Queue()}
        self._capture_threads = [
            threading.Thread(target=self._capture_loop, args=("me", self._mic), daemon=True),
            threading.Thread(target=self._capture_loop, args=("they", self._loopback), daemon=True),
        ]
        self._worker_threads = [
            threading.Thread(target=self._drain_loop, args=(source_id, on_frame), daemon=True)
            for source_id in self._queues
        ]
        for t in self._capture_threads + self._worker_threads:
            t.start()

    def _capture_loop(self, source_id: str, device) -> None:
        """Does ONLY capture + enqueue - never calls on_frame directly, so
        nothing downstream (ASR decode) can ever delay the next poll of the
        hardware."""
        pending = np.empty(0, dtype=np.float32)
        q = self._queues[source_id]
        try:
            with device.recorder(samplerate=SAMPLE_RATE, channels=1) as rec:
                while self._running:
                    chunk = rec.record(numframes=POLL_SAMPLES)
                    if len(chunk) == 0:
                        continue
                    pending = np.concatenate([pending, chunk[:, 0]])
                    while len(pending) >= FRAME_SAMPLES:
                        frame, pending = pending[:FRAME_SAMPLES], pending[FRAME_SAMPLES:]
                        pcm16 = (np.clip(frame, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
                        q.put(pcm16)
        except Exception:
            # One source failing (device unplugged, no default speaker, etc.)
            # must not silently kill the other - log and exit this thread only.
            logger.exception("Live Assist capture failed for source=%s", source_id)
        finally:
            q.put(_SENTINEL)

    def _drain_loop(self, source_id: str, on_frame: OnFrame) -> None:
        q = self._queues[source_id]
        while True:
            pcm = q.get()
            if pcm is _SENTINEL:
                break
            on_frame(source_id, pcm)

    def stop(self) -> None:
        self._running = False
        for t in self._capture_threads:
            t.join(timeout=3)
        for t in self._worker_threads:
            t.join(timeout=3)
        self._capture_threads = []
        self._worker_threads = []
