"""ASR adapter for NVIDIA's Nemotron 3.5 streaming model, run entirely
in-process via sherpa-onnx (int8 ONNX export), replacing both WhisperHttpAdapter
and the dead-end NemotronRivaAdapter as the default ASR stage.

Why this exists instead of NemotronRivaAdapter: NVIDIA's own nvcr.io/nvidia/
nemo-speech.cpp:0.1.0 container cannot run this model on this GPU at all
(GPU mode SIGSEGVs - its prebuilt CUDA kernels are missing a compute-
capability-7.5/Turing build of an op used during warmup) and, worse, its CPU
fallback loads and runs without crashing but silently returns an EMPTY
transcript for every real speech sample tested - confirmed with real
recordings (not just a tone) via the streaming API, the non-streaming
"offline" API, multiple chunk sizes, VAD masking on/off, and both decoder
kinds. That's a bug in NVIDIA's very-early (0.1.0) server binary, not in the
model or in CPU inference generally: this exact model, exported to ONNX and
run through sherpa-onnx (github.com/k2-fsa/sherpa-onnx, a mature, widely-used
project - not an NVIDIA binary), transcribes the SAME recordings correctly.
See the project's design notes for the full investigation.

Running in-process (no Docker container, no gRPC) is also simpler than the
Riva approach and removes a network hop; CPU-only is fine either way since
nemo-speech.cpp's GPU path never worked here regardless.

Model files: download one of the "nemotron-3.5-asr-streaming-0.6b-<latency>-
int8-<date>.tar.bz2" assets from
https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models (we use the
160ms chunk export - lowest practical latency without ONNX Runtime).
`model_id`/`base_url` in the registry's ModelAssignment are repurposed here:
`base_url` holds the extracted model directory path (not a network address -
there's no server), `model_id` is just a label.

Language: unlike WhisperHttpAdapter (forces a language) or the intended Riva
contract (language_code reported per-result), sherpa-onnx's result here
carries no language tag - this multilingual model transcribes correctly
without being told the language (verified: fed Portuguese speech with no
language hint, got correct Portuguese text). Without a real per-utterance
signal, the obvious fallback - "assume the user's last good language, else
the first allowlisted one" - turned out to be a real bug in practice, not
just a degraded case: with two+ allowed languages, EVERY dictation kept
getting labeled as whichever language came first in the user's allowlist,
because that fallback is also what feeds Orchestrator._remember_language()'s
"last good language" state, so it could never learn otherwise. A transcript
correctly recognized in Portuguese would be labeled "en" and handed to the
cleanup LLM with "keep this in English" - which plausibly explains reports
of dictations coming out in the wrong language or garbled (the cleanup
model trying to reconcile Portuguese content with an English instruction).
Fixed with `langdetect` classifying the FINAL TEXT itself (not the audio) -
simple, good enough to tell apart the kind of distinct languages a user
would configure (e.g. en vs pt-BR), and sidesteps needing the model to
expose anything it doesn't. Guarded by a minimum length (langdetect is
unreliable on very short text: "ok" detects as Slovak) and constrained to
the allowlist, with the old "preferred/first-allowed" logic only as a last
resort for text too short to classify.
"""
from __future__ import annotations

import logging

import numpy as np
from langdetect import LangDetectException, detect
from langdetect.detector_factory import DetectorFactory

from talkflow.asr.base import AsrAdapter, AsrCapabilities, FinalResult, PartialResult

logger = logging.getLogger(__name__)

DetectorFactory.seed = 0  # langdetect is non-deterministic by default; pin it

# Below this many characters, langdetect's guesses are unreliable enough to
# be actively misleading (e.g. "ok" -> Slovak) - not worth trusting.
MIN_CHARS_FOR_LANGDETECT = 12

# sherpa-onnx's streaming decode needs trailing context to emit the last
# word(s) - without this pad, short utterances especially lose their tail.
END_OF_TURN_PAD_S = 0.5
# Primes the cache-aware encoder before the first real audio arrives - see
# start_session(). Shorter than END_OF_TURN_PAD_S: this only needs to seed
# the encoder's internal state, not catch a trailing word.
START_OF_TURN_PAD_S = 0.3


class NemotronSherpaAdapter(AsrAdapter):
    capabilities = AsrCapabilities(streaming=True, supported_languages=None, frame_level=True)

    def __init__(self, model_dir: str, num_threads: int = 4, sample_rate: int = 16000):
        import sherpa_onnx  # local import: optional dep, only needed when this adapter is active

        self.model_id = "nemotron-3.5-asr-streaming-0.6b (sherpa-onnx int8)"
        self.base_url = model_dir
        self.sample_rate = sample_rate
        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=f"{model_dir}/tokens.txt",
            encoder=f"{model_dir}/encoder.int8.onnx",
            decoder=f"{model_dir}/decoder.int8.onnx",
            joiner=f"{model_dir}/joiner.int8.onnx",
            num_threads=num_threads,
            sample_rate=sample_rate,
            feature_dim=128,
            enable_endpoint_detection=False,  # the orchestrator owns turn boundaries, not the model
            decoding_method="greedy_search",
        )
        self._stream = None
        self._allowed_languages: list[str] = ["en"]
        # Set by the orchestrator to the language of the user's last good
        # dictation; used as finalize()'s best-effort label since this
        # adapter can't report per-result detection (see module docstring).
        self.preferred_language: str | None = None

    def warm(self) -> None:
        """No-op: loading the three ONNX models already happened in
        __init__ (~2-3s), there's nothing further to warm per-session."""

    def start_session(self, allowed_languages: list[str], initial_prompt: str | None = None) -> None:
        self._allowed_languages = allowed_languages or ["en"]
        self._stream = self._recognizer.create_stream()
        # A freshly created stream's cache-aware encoder starts cold, with no
        # left-context - observed in practice as the first word or so of a
        # dictation being dropped or (worse) mis-recognized into a
        # hallucinated token (e.g. a stray foreign-language word) when real
        # speech is the very first audio the stream ever sees. Priming with a
        # short silence pad before any real audio arrives gives the encoder
        # something to build initial context from, same idea as the
        # "preroll" sherpa-onnx's own deployment guidance recommends.
        pad = np.zeros(int(self.sample_rate * START_OF_TURN_PAD_S), dtype=np.float32)
        self._stream.accept_waveform(self.sample_rate, pad)
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)

    def feed_chunk(self, pcm: bytes, sample_rate: int) -> PartialResult | None:
        if self._stream is None:
            return None
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        self._stream.accept_waveform(sample_rate, samples)
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)
        text = self._recognizer.get_result(self._stream)
        if not text:
            return None
        return PartialResult(text=text, detected_language=None)

    def _detect_language(self, text: str) -> str | None:
        """Classify `text` among self._allowed_languages (base codes, e.g.
        "pt" matches "pt-BR"); None if too short/ambiguous to trust."""
        if len(self._allowed_languages) <= 1:
            return self._allowed_languages[0] if self._allowed_languages else None
        if len(text) < MIN_CHARS_FOR_LANGDETECT:
            return None
        try:
            guess = detect(text)
        except LangDetectException:
            return None
        for lang in self._allowed_languages:
            if lang.split("-")[0].lower() == guess.split("-")[0].lower():
                return lang
        return None

    def finalize(self) -> FinalResult:
        if self._stream is None:
            return FinalResult(text="", detected_language=None, detected_language_confidence=None)
        pad = np.zeros(int(self.sample_rate * END_OF_TURN_PAD_S), dtype=np.float32)
        self._stream.accept_waveform(self.sample_rate, pad)
        self._stream.input_finished()
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)
        text = self._recognizer.get_result(self._stream).strip()
        self._stream = None
        lang = self._detect_language(text) or self.preferred_language
        return FinalResult(text=text, detected_language=lang, detected_language_confidence=None)
