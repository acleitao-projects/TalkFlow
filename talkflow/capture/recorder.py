"""Microphone capture via sounddevice, 16kHz mono 16-bit PCM (Whisper/webrtcvad
native rate). Runs a background InputStream and exposes a simple
start()/stop()/cancel() API plus a callback invoked with each VAD frame.
"""
from __future__ import annotations

import queue
import threading
from typing import Callable

import numpy as np
import sounddevice as sd

from talkflow.capture.vad import FRAME_BYTES, SAMPLE_RATE

OnFrame = Callable[[bytes], None]


class AudioRecorder:
    def __init__(self, device_id: int | None = None):
        self.device_id = device_id
        self._stream: sd.InputStream | None = None
        self._q: "queue.Queue[bytes]" = queue.Queue()
        self._leftover = b""
        self._worker: threading.Thread | None = None
        self._running = False
        self._on_frame: OnFrame | None = None
        self._all_pcm = bytearray()

    def start(self, on_frame: OnFrame) -> None:
        self._on_frame = on_frame
        self._all_pcm = bytearray()
        self._leftover = b""
        self._running = True

        def _callback(indata, frames, time_info, status):
            pcm16 = (indata[:, 0] * 32767).astype(np.int16).tobytes()
            self._q.put(pcm16)

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            device=self.device_id, callback=_callback, blocksize=int(SAMPLE_RATE * 0.03),
        )
        self._stream.start()
        self._worker = threading.Thread(target=self._drain_loop, daemon=True)
        self._worker.start()

    def _drain_loop(self) -> None:
        while self._running:
            try:
                data = self._q.get(timeout=0.02)
            except queue.Empty:
                continue
            self._all_pcm.extend(data)
            buf = self._leftover + data
            n_frames = len(buf) // FRAME_BYTES
            for i in range(n_frames):
                frame = buf[i * FRAME_BYTES:(i + 1) * FRAME_BYTES]
                if self._on_frame:
                    self._on_frame(frame)
            self._leftover = buf[n_frames * FRAME_BYTES:]

    def stop(self) -> bytes:
        self._running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if self._worker:
            self._worker.join(timeout=2)
        # The drain thread exits as soon as _running flips, which can leave
        # the last audio blocks (the end of the sentence) still queued.
        while True:
            try:
                self._all_pcm.extend(self._q.get_nowait())
            except queue.Empty:
                break
        return bytes(self._all_pcm)

    def cancel(self) -> None:
        self.stop()
