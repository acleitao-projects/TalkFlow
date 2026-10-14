"""ASR adapter for any OpenAI-compatible /v1/audio/transcriptions endpoint.

This is the SAME adapter whether the server is the local Docker-hosted
speaches container (http://localhost:8000, GPU-accelerated, default) or a
cloud Whisper-compatible host - only base_url/model_id/api_key differ, both
coming from the model registry. "Streaming" is implemented as rolling
re-transcription of the growing audio buffer each time the VAD layer signals
a chunk boundary, per the project's design notes' "chunked/rolling, not word-by-word" target
- faster-whisper (what speaches runs) has no native incremental-decoding API.

Language handling: the allowlist isn't natively enforceable by the API, so
we call without a forced `language` (auto-detect across everything Whisper
knows) and then, once finalized, snap the detection to the closest allowed
language if the raw detection isn't already in the allowlist by re-running
the final pass with that language forced. See `finalize()`.
"""
from __future__ import annotations

import io
import wave

import httpx

from talkflow.asr.base import AsrAdapter, AsrCapabilities, FinalResult, PartialResult


def _pcm_to_wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


# Some hosts report the detected language as a name instead of a code.
_LANGUAGE_NAMES = {
    "english": "en", "portuguese": "pt", "spanish": "es", "french": "fr", "german": "de",
    "italian": "it", "dutch": "nl", "turkish": "tr", "russian": "ru", "japanese": "ja",
    "chinese": "zh", "korean": "ko",
}


def base_language(lang: str | None) -> str | None:
    """"pt-BR" / "PT" / "portuguese" -> "pt" (the form Whisper accepts and reports)."""
    if not lang:
        return None
    lang = lang.strip().lower()
    return _LANGUAGE_NAMES.get(lang, lang.split("-")[0].split("_")[0])


def ensure_model_downloaded(base_url: str, model_id: str, client: httpx.Client | None = None) -> None:
    """For the local Docker (speaches) server: models are not preloaded into
    the image, so the first call after a fresh container would otherwise 404.
    Call this once at app startup for the active ASR assignment."""
    c = client or httpx.Client(timeout=120.0)
    try:
        check = c.get(f"{base_url.rstrip('/')}/v1/models/{model_id.replace('/', '%2F')}")
        if check.status_code == 200:
            return
    except Exception:
        pass
    c.post(f"{base_url.rstrip('/')}/v1/models/{model_id.replace('/', '%2F')}")


class WhisperHttpAdapter(AsrAdapter):
    capabilities = AsrCapabilities(streaming=True, supported_languages=None)

    def __init__(self, base_url: str, model_id: str, api_key: str | None = None,
                 timeout: float = 20.0, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.api_key = api_key
        self.timeout = timeout
        # Long keep-alive: httpx's 5s default meant every dictation paid a new
        # TCP/TLS handshake to a cloud host.
        self._client = client or httpx.Client(
            timeout=timeout,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=300),
        )
        self._buffer = bytearray()
        self._sample_rate = 16000
        self._allowed_languages: list[str] = ["en"]
        self._last_detected: str | None = None
        self._initial_prompt: str | None = None
        # Language to force when detection lands outside the allowlist; set by
        # the orchestrator to the language of the user's last good dictation.
        self.preferred_language: str | None = None
        self._plain_json_only = False  # host rejected verbose_json once; don't retry it

    def warm(self) -> None:
        """Open the connection ahead of the first transcription request."""
        try:
            self._client.head(self.base_url, timeout=5.0)
        except Exception:
            pass

    def start_session(self, allowed_languages: list[str], initial_prompt: str | None = None) -> None:
        self._buffer = bytearray()
        self._allowed_languages = allowed_languages or ["en"]
        self._last_detected = None
        self._initial_prompt = initial_prompt

    def feed_chunk(self, pcm: bytes, sample_rate: int) -> PartialResult | None:
        self._sample_rate = sample_rate
        # Buffer only. Rolling re-transcription during recording used to fire
        # a full-buffer request at every pause; nothing consumed the partials,
        # and an in-flight one delayed the final request on the single GPU.
        self._buffer.extend(pcm)
        return None

    def set_full_audio(self, pcm: bytes, sample_rate: int) -> None:
        """Replace the rolling buffer with the complete recording. The VAD
        only feeds chunks at pauses, so audio after the last pause (i.e. the
        whole clip for a short utterance) would otherwise never reach
        finalize() and every short dictation came back as 'no speech'."""
        self._sample_rate = sample_rate
        self._buffer = bytearray(pcm)

    def finalize(self) -> FinalResult:
        if not self._buffer:
            return FinalResult(text="", detected_language=None, detected_language_confidence=None)
        return self.transcribe_pcm(bytes(self._buffer), self._sample_rate)

    def transcribe_pcm(self, pcm: bytes, sample_rate: int, language: str | None = None) -> FinalResult:
        """Transcribe one self-contained clip without touching the session
        buffer, so the orchestrator's partial-paste worker can call it from
        its own thread while recording continues."""
        language = base_language(language)
        text, lang = self._transcribe(pcm, language=language, sample_rate=sample_rate)
        lang = base_language(lang)
        allowed = [base_language(a) for a in self._allowed_languages]
        if not language and lang and lang not in allowed:
            # Whisper auto-detects across ~100 languages and a short or noisy
            # clip can land on one the user never speaks (Portuguese heard as
            # Turkish gave a Turkish transcript). Redo it constrained to the
            # allowlist: the user's last good language if known, else the first.
            preferred = base_language(self.preferred_language)
            forced = preferred if preferred in allowed else allowed[0]
            text, lang = self._transcribe(pcm, language=forced, sample_rate=sample_rate)
            lang = forced
        return FinalResult(text=text, detected_language=lang or language, detected_language_confidence=None)

    def _transcribe(self, pcm: bytes, language: str | None,
                    sample_rate: int | None = None) -> tuple[str, str | None]:
        wav_bytes = _pcm_to_wav_bytes(pcm, sample_rate or self._sample_rate)
        files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
        data = {"model": self.model_id,
                "response_format": "json" if self._plain_json_only else "verbose_json"}
        if language:
            data["language"] = language
        if self._initial_prompt:
            data["prompt"] = self._initial_prompt
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = f"{self.base_url}/v1/audio/transcriptions"
        resp = self._client.post(url, files=files, data=data, headers=headers)
        if resp.status_code in (400, 415, 422) and data.get("response_format") != "json":
            # Some hosts (e.g. OpenRouter) don't accept verbose_json; fall back
            # to plain json (no language field -> caller uses its default).
            data["response_format"] = "json"
            files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
            resp = self._client.post(url, files=files, data=data, headers=headers)
            if resp.is_success:
                self._plain_json_only = True
        resp.raise_for_status()
        body = resp.json()
        return body.get("text", "").strip(), body.get("language")
