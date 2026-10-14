"""One-shot smoke test for NemotronSherpaAdapter (in-process sherpa-onnx, no
container/network): feeds a WAV file in small chunks, real-time paced, so
interim partials actually have time to arrive, and prints every partial plus
the final transcript.

    .venv\\Scripts\\python.exe scripts\\smoke_test_nemotron.py path\\to\\real_speech.wav

Needs real speech, not a pure tone - this adapter correctly returns empty
text for silence/non-speech, so there's nothing meaningful to show without a
real recording. Requires the sherpa-onnx model directory at the path in
talkflow.registry.store.SHERPA_NEMOTRON_MODEL_DIR (download per that
module's docstring if missing).
"""
from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from talkflow.asr.nemotron_sherpa_adapter import NemotronSherpaAdapter  # noqa: E402
from talkflow.registry.store import SHERPA_NEMOTRON_MODEL_DIR  # noqa: E402

CHUNK_MS = 100
SAMPLE_RATE = 16000


def _load_wav(path: str) -> bytes:
    with wave.open(path, "rb") as wf:
        assert wf.getframerate() == SAMPLE_RATE, f"expected {SAMPLE_RATE}Hz, got {wf.getframerate()}"
        assert wf.getnchannels() == 1, "expected mono"
        assert wf.getsampwidth() == 2, "expected 16-bit PCM"
        return wf.readframes(wf.getnframes())


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    pcm = _load_wav(sys.argv[1])
    print(f"Loaded {len(pcm)} bytes ({len(pcm) / (SAMPLE_RATE * 2):.1f}s) of audio")

    print(f"Loading model from {SHERPA_NEMOTRON_MODEL_DIR} ...")
    t0 = time.monotonic()
    adapter = NemotronSherpaAdapter(model_dir=SHERPA_NEMOTRON_MODEL_DIR)
    print(f"Model loaded in {time.monotonic() - t0:.1f}s")
    adapter.start_session(["en", "pt-BR"])

    chunk_bytes = int(SAMPLE_RATE * (CHUNK_MS / 1000.0)) * 2
    last_partial = ""
    t0 = time.monotonic()
    for offset in range(0, len(pcm), chunk_bytes):
        chunk = pcm[offset:offset + chunk_bytes]
        result = adapter.feed_chunk(chunk, SAMPLE_RATE)
        if result and result.text != last_partial:
            last_partial = result.text
            print(f"  t={time.monotonic() - t0:.2f}s partial: {last_partial!r}")
        time.sleep(CHUNK_MS / 1000.0)

    final = adapter.finalize()
    print(f"FINAL: text={final.text!r} (total {time.monotonic() - t0:.1f}s)")


if __name__ == "__main__":
    main()
