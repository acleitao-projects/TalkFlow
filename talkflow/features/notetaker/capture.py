"""Notetaker's capture layer: mic (your voice) + WASAPI loopback (other
participants' system audio) mixed into one mono stream. Confirmed working
without admin rights via `soundcard` (see the design notes' Risk 5 - resolved).

On Windows, loopback only captures the default output device, so whatever
meeting app is in use must be routed to that device (same caveat documented
for Wispr's own Notetaker).
"""
from __future__ import annotations

import threading

import numpy as np
import soundcard as sc

SAMPLE_RATE = 16000


class MeetingRecorder:
    def __init__(self):
        self._mic = sc.default_microphone()
        speaker = sc.default_speaker()
        self._loopback = sc.get_microphone(speaker.name, include_loopback=True)
        self._running = False
        self._frames: list[np.ndarray] = []
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._running = True
        self._frames = []
        self._thread = threading.Thread(target=self._record_loop, daemon=True)
        self._thread.start()

    def _record_loop(self) -> None:
        block = int(SAMPLE_RATE * 0.5)
        with self._mic.recorder(samplerate=SAMPLE_RATE, channels=1) as mic_rec, \
             self._loopback.recorder(samplerate=SAMPLE_RATE, channels=1) as loop_rec:
            while self._running:
                mic_chunk = mic_rec.record(numframes=block)
                loop_chunk = loop_rec.record(numframes=block)
                n = min(len(mic_chunk), len(loop_chunk))
                mixed = np.clip(mic_chunk[:n, 0] + loop_chunk[:n, 0], -1.0, 1.0)
                self._frames.append(mixed.copy())

    def stop(self) -> bytes:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        if not self._frames:
            return b""
        full = np.concatenate(self._frames)
        pcm16 = (full * 32767).astype(np.int16)
        return pcm16.tobytes()
