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
import time
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

        from talkflow.config.settings import Settings

        settings = Settings.load()
        # Some audio interfaces - confirmed live on a RodeCaster Pro II - mix
        # the user's own mic into their USB monitor/return bus, so a plain
        # default-speaker loopback for "they" captures a copy of the user's
        # own voice too (not acoustic bleed - a hardware routing quirk),
        # showing up as near-duplicate "me"/"they" lines. Three ways to
        # avoid that, in priority order:
        #   1. live_assist_system_audio_process_name: per-process WASAPI
        #      loopback via flexaudio - only that one app's rendered audio.
        #   2. live_assist_system_audio_device_name: loopback a SPECIFIC
        #      named output device (e.g. a RodeCaster's dedicated "Chat" bus,
        #      built by the hardware itself to not loop back into the mic).
        #   3. neither set: whole default-speaker loopback, as before.
        # Same idea for the mic: live_assist_mic_device_name picks a named
        # input device instead of whatever Windows calls "default".
        mic_name = (settings.live_assist_mic_device_name or "").strip() or None
        self._mic = self._find_microphone(sc, mic_name) if mic_name else sc.default_microphone()

        self._system_audio_process_name = (settings.live_assist_system_audio_process_name or "").strip() or None
        system_audio_device_name = (settings.live_assist_system_audio_device_name or "").strip() or None
        if self._system_audio_process_name:
            self._loopback = None  # resolved per-session in _capture_loop_process
        elif system_audio_device_name:
            # Deliberately NOT a silent fallback: a *missing* setting (the
            # branches above/below) means "use the default", but an
            # EXPLICIT, now-invalid override must fail loud instead of
            # quietly substituting a different device than the one asked
            # for - that silent-substitution behavior is exactly the kind
            # of device-identity confusion this project spent a long live
            # debugging session chasing. Raises ValueError; the caller
            # (orchestrator.start()) is responsible for surfacing it.
            self._loopback = self._resolve_output_loopback(sc, system_audio_device_name)
        else:
            self._loopback = sc.get_microphone(sc.default_speaker().name, include_loopback=True)
        self._running = False
        self._capture_threads: list[threading.Thread] = []
        self._worker_threads: list[threading.Thread] = []
        self._queues: dict[str, queue.Queue] = {}

    def start(self, on_frame: OnFrame) -> None:
        self._running = True
        self._queues = {"me": queue.Queue(), "they": queue.Queue()}
        if self._system_audio_process_name:
            logger.info("Live Assist: 'they' capture using per-process audio (process=%r)",
                        self._system_audio_process_name)
            they_thread = threading.Thread(
                target=self._capture_loop_process, args=("they", self._system_audio_process_name), daemon=True)
        else:
            logger.info("Live Assist: 'they' capture using device loopback (device=%r)", self._loopback.name)
            they_thread = threading.Thread(target=self._capture_loop, args=("they", self._loopback), daemon=True)
        logger.info("Live Assist: 'me' capture using mic device (device=%r)", self._mic.name)
        self._capture_threads = [
            threading.Thread(target=self._capture_loop, args=("me", self._mic), daemon=True),
            they_thread,
        ]
        self._worker_threads = [
            threading.Thread(target=self._drain_loop, args=(source_id, on_frame), daemon=True)
            for source_id in self._queues
        ]
        for t in self._capture_threads + self._worker_threads:
            t.start()

    @staticmethod
    def _resolve_output_loopback(sc, name: str):
        """Resolves `name` to an EXACT playback device (`sc.all_speakers()`,
        not a fuzzy substring match), then that device's loopback
        microphone, verified via `isloopback` so it can never silently
        resolve to an unrelated recording device instead. Raises ValueError
        on any failure - see the call site for why this one path fails loud
        instead of falling back."""
        lowered = name.strip().lower()
        match = next((s for s in sc.all_speakers() if s.name.strip().lower() == lowered), None)
        if match is None:
            raise ValueError(f"Live Assist: configured system-audio output device {name!r} not found")
        loopback = sc.get_microphone(match.name, include_loopback=True)
        if not getattr(loopback, "isloopback", False):
            raise ValueError(f"Live Assist: device {name!r} did not resolve to a loopback source")
        return loopback

    @staticmethod
    def _find_microphone(sc, name: str):
        """Exact match first, then substring (case-insensitive) - device
        names are long and exact-sensitive ("Microfone (RODECaster Pro II
        Main Stereo)"), so a forgiving match avoids a trivial typo silently
        falling back to the wrong device. Falls back to the default mic
        with a warning if nothing matches, rather than raising and killing
        the whole session."""
        candidates = sc.all_microphones(include_loopback=False)
        lowered = name.strip().lower()
        for m in candidates:
            if m.name.strip().lower() == lowered:
                return m
        for m in candidates:
            if lowered in m.name.strip().lower():
                return m
        logger.warning("Live Assist: configured mic device %r not found; using the system default instead.", name)
        return sc.default_microphone()

    @staticmethod
    def _resolve_pid(process_name: str) -> int | None:
        import flexaudio
        name = process_name.strip().lower().removesuffix(".exe")
        for p in flexaudio.processes():
            candidate = (p.executable or p.name or "").lower().removesuffix(".exe")
            if candidate == name or (p.name or "").lower() == name:
                return p.pid
        return None

    def _capture_loop_process(self, source_id: str, process_name: str) -> None:
        """Per-process WASAPI loopback via flexaudio - captures ONLY the
        named app's rendered audio, not whatever the physical audio
        interface mixes into "the system output" as a whole. Same
        capture-only/enqueue-only discipline as _capture_loop(): decoding
        never happens on this thread."""
        import flexaudio

        q = self._queues[source_id]
        try:
            pid = self._resolve_pid(process_name)
            if pid is None:
                logger.warning(
                    "Live Assist: process %r not found for system-audio capture "
                    "(is it running yet?) - 'they' will receive no audio this session.",
                    process_name,
                )
                return
            pending = np.empty(0, dtype=np.float32)
            with flexaudio.open(
                # flexaudio enforces a fixed 20ms delivery contract (confirmed
                # live: 30 raised InvalidArgumentError) - this only controls how
                # often it hands us samples, not our own FRAME_SAMPLES re-slicing
                # below, so it doesn't need to match capture.py's 30ms frame size.
                "process", process_id=pid, output_rate=SAMPLE_RATE, output_channels=1, chunk_ms=20,
            ) as stream:
                while self._running:
                    chunk = stream.poll_chunk()
                    if chunk is None:
                        time.sleep(0.005)
                        continue
                    samples = np.frombuffer(chunk.data, dtype=np.float32)
                    pending = np.concatenate([pending, samples])
                    while len(pending) >= FRAME_SAMPLES:
                        frame, pending = pending[:FRAME_SAMPLES], pending[FRAME_SAMPLES:]
                        pcm16 = (np.clip(frame, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
                        q.put(pcm16)
        except Exception:
            logger.exception("Live Assist process-audio capture failed for source=%s process=%s",
                              source_id, process_name)
        finally:
            q.put(_SENTINEL)

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
