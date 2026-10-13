"""One-shot smoke test for the Nemotron streaming ASR container: opens a real
gRPC streaming session against localhost:50051, feeds a WAV file in small
chunks (simulating live mic frames) with a short sleep between them so
interim results actually have time to come back, and prints every partial
plus the final transcript. Not part of pytest (needs the container running
and a real model loaded) - run manually after `docker compose -f
docker-compose.nemotron.yml up -d`:

    .venv\\Scripts\\python.exe scripts\\smoke_test_nemotron.py [path/to.wav]

With no argument, synthesizes a short spoken-digit-free sine sweep (useless
for WER, but enough to prove the stream opens, accepts audio, and returns
*something* without erroring) - pass a real voice recording to sanity check
actual transcription quality.
"""
from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from talkflow.asr.nemotron_riva_adapter import NemotronRivaAdapter  # noqa: E402

CHUNK_MS = 100
SAMPLE_RATE = 16000


def _load_wav(path: str) -> bytes:
    with wave.open(path, "rb") as wf:
        assert wf.getframerate() == SAMPLE_RATE, f"expected {SAMPLE_RATE}Hz, got {wf.getframerate()}"
        assert wf.getnchannels() == 1, "expected mono"
        assert wf.getsampwidth() == 2, "expected 16-bit PCM"
        return wf.readframes(wf.getnframes())


def _synthetic_tone(seconds: float = 2.0) -> bytes:
    import math
    import struct
    n = int(SAMPLE_RATE * seconds)
    return b"".join(struct.pack("<h", int(4000 * math.sin(2 * math.pi * 220 * i / SAMPLE_RATE)))
                     for i in range(n))


def main() -> None:
    pcm = _load_wav(sys.argv[1]) if len(sys.argv) > 1 else _synthetic_tone()
    print(f"Loaded {len(pcm)} bytes ({len(pcm) / (SAMPLE_RATE * 2):.1f}s) of audio")

    adapter = NemotronRivaAdapter()  # 127.0.0.1:50051 default
    print(f"Connecting to {adapter.uri} ...")
    adapter.start_session(["en"])

    chunk_bytes = int(SAMPLE_RATE * (CHUNK_MS / 1000.0)) * 2
    last_partial = ""
    for offset in range(0, len(pcm), chunk_bytes):
        chunk = pcm[offset:offset + chunk_bytes]
        result = adapter.feed_chunk(chunk, SAMPLE_RATE)
        if result and result.text != last_partial:
            last_partial = result.text
            print(f"  partial: {last_partial!r}")
        time.sleep(CHUNK_MS / 1000.0)

    final = adapter.finalize()
    print(f"FINAL: text={final.text!r} language={final.detected_language!r}")


if __name__ == "__main__":
    main()
