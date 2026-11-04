"""MultiStreamAsrAdapter - LA-05: two concurrent sherpa-onnx streaming
sessions (one per source_id, "me"/"they") off a SINGLE loaded recognizer.

talkflow/asr/nemotron_sherpa_adapter.py::NemotronSherpaAdapter already proves
the real sherpa-onnx pattern this wraps - one OnlineRecognizer (expensive,
loaded once, ~2-3s) + cheap create_stream() calls - it just hardcodes a
single `self._stream`, overwritten by a second start_session() call. This
copies that adapter's exact accept_waveform/is_ready/decode_stream/get_result
sequence, keyed by source_id in a dict instead of one instance attribute, so
two sources can run at once without clobbering each other.

Only the "nemotron-local" ASR provider is supported here - a different
provider would need a per-source-adapter fallback (accepting the double
model-load cost), not built yet; see talkflow/app.py::get_live_assist_asr().
"""
from __future__ import annotations

import numpy as np

from talkflow.asr.base import FinalResult, PartialResult

START_OF_TURN_PAD_S = 0.3  # same priming rationale as nemotron_sherpa_adapter.py
END_OF_TURN_PAD_S = 0.3


class MultiStreamAsrAdapter:
    def __init__(self, model_dir: str, num_threads: int = 4, sample_rate: int = 16000):
        import sherpa_onnx  # local import: optional dep, only needed when this adapter is active

        self.sample_rate = sample_rate
        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=f"{model_dir}/tokens.txt",
            encoder=f"{model_dir}/encoder.int8.onnx",
            decoder=f"{model_dir}/decoder.int8.onnx",
            joiner=f"{model_dir}/joiner.int8.onnx",
            num_threads=num_threads, sample_rate=sample_rate, feature_dim=128,
            enable_endpoint_detection=False,  # the orchestrator owns turn boundaries, not the model
            decoding_method="greedy_search",
        )
        self._streams: dict[str, object] = {}

    def start_source(self, source_id: str, allowed_languages: list[str]) -> None:
        stream = self._recognizer.create_stream()
        # Prime with silence so the cache-aware encoder isn't cold on the
        # first real audio - same rationale as NemotronSherpaAdapter.
        pad = np.zeros(int(self.sample_rate * START_OF_TURN_PAD_S), dtype=np.float32)
        stream.accept_waveform(self.sample_rate, pad)
        while self._recognizer.is_ready(stream):
            self._recognizer.decode_stream(stream)
        self._streams[source_id] = stream

    def feed_source_chunk(self, source_id: str, pcm: bytes) -> PartialResult | None:
        stream = self._streams.get(source_id)
        if stream is None:
            return None
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        stream.accept_waveform(self.sample_rate, samples)
        while self._recognizer.is_ready(stream):
            self._recognizer.decode_stream(stream)
        text = self._recognizer.get_result(stream)
        return PartialResult(text=text, detected_language=None) if text else None

    def finalize_source(self, source_id: str) -> FinalResult:
        stream = self._streams.pop(source_id, None)
        if stream is None:
            return FinalResult(text="", detected_language=None, detected_language_confidence=None)
        pad = np.zeros(int(self.sample_rate * END_OF_TURN_PAD_S), dtype=np.float32)
        stream.accept_waveform(self.sample_rate, pad)
        stream.input_finished()
        while self._recognizer.is_ready(stream):
            self._recognizer.decode_stream(stream)
        text = self._recognizer.get_result(stream).strip()
        return FinalResult(text=text, detected_language=None, detected_language_confidence=None)
