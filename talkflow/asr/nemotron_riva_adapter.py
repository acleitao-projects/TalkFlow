"""ASR adapter for NVIDIA's Nemotron 3.5 streaming model over a local
Riva-compatible gRPC server (nvcr.io/nvidia/nemo-speech.cpp), replacing the
HTTP/batch WhisperHttpAdapter as the default ASR stage.

Unlike WhisperHttpAdapter - which only ever gets whole clips (one per pause,
or the whole buffer at stop) - this is genuinely continuous: feed_chunk() is
called by the orchestrator for every captured audio frame (no VAD batching
first), pushed onto a queue that feeds a persistent bidirectional gRPC stream
opened in start_session(). The server's cache-aware FastConformer encoder
does the actual incremental decoding; this adapter is just plumbing. This is
the entire point of the switch from Whisper (batch HTTP) to Nemotron
(cache-aware streaming) - see CLAUDE.md.

`capabilities.frame_level = True` is how the orchestrator recognizes this and
skips both the VAD-boundary batching path (plain mode) and the segment-worker
path (partial_paste/incremental_asr, which calls transcribe_pcm() - this
adapter deliberately has no such method, since it has no stateless/batch
transcription mode to fall back to).

Language: Riva's language_code is set once per stream, not per chunk. With a
single allowed language we pass it directly; with more than one we pass
"auto" (Nemotron 3.5 supports automatic language detection across its 40
locales) and read back whatever language the server reports on the final
result, same "detect among the allowlist, else fall back" contract as
WhisperHttpAdapter - just resolved server-side instead of via Whisper's
"redo with the language forced" dance.
"""
from __future__ import annotations

import logging
import queue
import threading

from talkflow.asr.base import AsrAdapter, AsrCapabilities, FinalResult, PartialResult

logger = logging.getLogger(__name__)

STREAM_END_TIMEOUT_S = 15.0


class NemotronRivaAdapter(AsrAdapter):
    capabilities = AsrCapabilities(streaming=True, supported_languages=None, frame_level=True)

    def __init__(self, uri: str = "127.0.0.1:50051", sample_rate: int = 16000):
        # "localhost" resolves to [::1] first on this machine, but the
        # container's port is published on 127.0.0.1 only (see
        # docker-compose.nemotron.yml) - that mismatch silently fails as a
        # "connection refused"/"End of TCP stream" gRPC error, so the default
        # here is the literal IPv4 address, not the hostname.
        import riva.client  # local import: optional dep, only needed when this adapter is active

        self._riva = riva.client
        self.uri = uri
        self.sample_rate = sample_rate
        self.model_id = "nemotron-3.5-asr-streaming-0.6b"
        self.base_url = uri
        self._auth = riva.client.Auth(uri=uri)
        self._asr = riva.client.ASRService(self._auth)

        self._audio_q: "queue.Queue[bytes | None] | None" = None
        self._thread: threading.Thread | None = None
        self._done = threading.Event()
        self._stream_error: Exception | None = None

        self._latest_partial = ""
        self._final_parts: list[str] = []
        self._detected_language: str | None = None
        self._allowed_languages: list[str] = ["en"]
        self._initial_prompt: str | None = None
        # Set by the orchestrator to the language of the user's last good
        # dictation; read here only as a default if the server never reports one.
        self.preferred_language: str | None = None

    def warm(self) -> None:
        """Touch the gRPC channel ahead of the first real session so the
        connection is already established by the time start_session() runs."""
        try:
            self._auth.get_auth_metadata()
        except Exception:
            pass

    def start_session(self, allowed_languages: list[str], initial_prompt: str | None = None) -> None:
        self._allowed_languages = allowed_languages or ["en"]
        self._initial_prompt = initial_prompt
        self._latest_partial = ""
        self._final_parts = []
        self._detected_language = None
        self._stream_error = None
        self._done.clear()
        self._audio_q = queue.Queue()

        language_code = self._allowed_languages[0] if len(self._allowed_languages) == 1 else "auto"
        recognition_config = self._riva.RecognitionConfig(
            encoding=self._riva.AudioEncoding.LINEAR_PCM,
            sample_rate_hertz=self.sample_rate,
            language_code=language_code,
            max_alternatives=1,
            enable_automatic_punctuation=True,
            speech_contexts=(
                [self._riva.SpeechContext(phrases=[initial_prompt])] if initial_prompt else None
            ),
        )
        streaming_config = self._riva.StreamingRecognitionConfig(
            config=recognition_config, interim_results=True,
        )

        def audio_chunks():
            while True:
                item = self._audio_q.get()
                if item is None:
                    return
                yield item

        def run() -> None:
            try:
                responses = self._asr.streaming_response_generator(
                    audio_chunks=audio_chunks(), streaming_config=streaming_config,
                )
                for response in responses:
                    for result in response.results:
                        if not result.alternatives:
                            continue
                        text = result.alternatives[0].transcript.strip()
                        if getattr(result, "is_final", False):
                            if text:
                                self._final_parts.append(text)
                            lang = getattr(result, "language_code", None)
                            if lang:
                                self._detected_language = lang
                            self._latest_partial = ""
                        else:
                            self._latest_partial = text
            except Exception as e:  # server down, model not loaded, stream reset, etc.
                logger.exception("Nemotron streaming session failed")
                self._stream_error = e
            finally:
                self._done.set()

        self._thread = threading.Thread(target=run, daemon=True, name="nemotron-riva-stream")
        self._thread.start()

    def feed_chunk(self, pcm: bytes, sample_rate: int) -> PartialResult | None:
        if self._audio_q is None or self._done.is_set():
            return None
        self._audio_q.put(pcm)
        text = self._latest_partial
        if not text:
            return None
        return PartialResult(text=text, detected_language=self._detected_language)

    def finalize(self) -> FinalResult:
        if self._audio_q is not None:
            self._audio_q.put(None)
        self._done.wait(timeout=STREAM_END_TIMEOUT_S)
        text = " ".join(p for p in self._final_parts if p).strip()
        if not text and self._latest_partial:
            # Stream ended (e.g. server-side endpointing) before the last bit
            # of speech was ever marked final; use the trailing partial rather
            # than silently drop it.
            text = self._latest_partial.strip()
        lang = self._detected_language or self.preferred_language
        if self._stream_error and not text:
            raise RuntimeError(f"Nemotron ASR stream failed: {self._stream_error}")
        return FinalResult(text=text, detected_language=lang, detected_language_confidence=None)
