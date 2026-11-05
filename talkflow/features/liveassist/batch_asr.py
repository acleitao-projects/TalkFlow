"""TwoSourceBatchAsrAdapter - lets Live Assist use any OpenAI-compatible
batch transcription endpoint (including the real OpenAI API, not just the
local-docker/openrouter providers already supported for plain dictation)
instead of the local Nemotron/sherpa-onnx streaming adapter, for when the
local model is too slow on the user's hardware.

Unlike MultiStreamAsrAdapter, there's no real interim partial text - a batch
HTTP endpoint has no incremental-decoding API - so feed_source_chunk() only
buffers audio and always returns None. The full utterance comes back from
finalize_source() in one shot, which the orchestrator already calls at every
VAD boundary or at worst every ~10s (orchestrator.py's _HARD_CAP_FRAMES), so
this matches the existing finalize cadence - the tradeoff is a few seconds of
added latency per utterance instead of live word-by-word text, not a
redesign of the orchestrator.
"""
from __future__ import annotations

import httpx

from talkflow.asr.base import FinalResult, PartialResult
from talkflow.asr.whisper_http_adapter import WhisperHttpAdapter

_SOURCE_SAMPLE_RATE = 16000


class TwoSourceBatchAsrAdapter:
    def __init__(self, base_url: str, model_id: str, api_key: str | None = None,
                 client: httpx.Client | None = None):
        self._base_url = base_url
        self._model_id = model_id
        self._api_key = api_key
        # Injectable so tests can supply a MockTransport-backed client instead
        # of making real HTTP requests - shared across both sources' adapters,
        # same as WhisperHttpAdapter's own default (a real client per adapter
        # otherwise, which is fine too since two sources rarely transcribe at
        # the exact same instant).
        self._client = client
        self._adapters: dict[str, WhisperHttpAdapter] = {}
        self._buffers: dict[str, bytearray] = {}

    def start_source(self, source_id: str, allowed_languages: list[str]) -> None:
        adapter = self._adapters.get(source_id)
        if adapter is None:
            adapter = WhisperHttpAdapter(self._base_url, self._model_id, self._api_key, client=self._client)
            self._adapters[source_id] = adapter
        adapter.start_session(allowed_languages)
        self._buffers[source_id] = bytearray()

    def feed_source_chunk(self, source_id: str, pcm: bytes) -> PartialResult | None:
        buf = self._buffers.get(source_id)
        if buf is not None:
            buf.extend(pcm)
        return None

    def finalize_source(self, source_id: str) -> FinalResult:
        buf = self._buffers.pop(source_id, None)
        adapter = self._adapters.get(source_id)
        if not buf or adapter is None:
            return FinalResult(text="", detected_language=None, detected_language_confidence=None)
        return adapter.transcribe_pcm(bytes(buf), _SOURCE_SAMPLE_RATE)
