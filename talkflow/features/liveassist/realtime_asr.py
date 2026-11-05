"""TwoSourceRealtimeAsrAdapter - true low-latency streaming transcription for
Live Assist via OpenAI's Realtime transcription API
(wss://api.openai.com/v1/realtime?intent=transcription), instead of
batch_asr.py's one-shot HTTP POST per utterance.

Why this exists: batch_asr.py's adapter has zero interim text - nothing
appears on screen until a whole chunk finishes decoding, which during
continuous speech can mean many seconds of dead air (see that module's
docstring). This adapter streams audio continuously over a persistent
per-source WebSocket and reports real partial text as
conversation.item.input_audio_transcription.delta events arrive.

The reference app Persua (research/perssua-0.28.0 in the sibling project)
uses this exact mechanism - confirmed by reading its decompiled renderer
code (TRANSCRIPTION-RENDERER.md, and renderer-readable/js/1479...:1190-1260)
- including the trick that keeps it cheap: a per-source RMS noise gate with
a hangover window decides what actually gets appended to the socket at all.
Without it, a persistent full-duplex connection streams - and is billed for
- silence for the whole session, which is why a naive $/minute estimate
didn't match a real user's much lower reported cost.

Each source gets its own WebSocket connection, opened once in start_source()
and reused for the rest of the Live Assist session (re-opening per utterance
would pay a fresh handshake every few seconds and defeat the point of
streaming) - matches the orchestrator's existing per-source model
(start_source/feed_source_chunk/finalize_source) it already uses for
MultiStreamAsrAdapter and TwoSourceBatchAsrAdapter.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time

import numpy as np
import websocket

from talkflow.asr.base import FinalResult, PartialResult
from talkflow.asr.whisper_http_adapter import base_language

logger = logging.getLogger(__name__)

_REALTIME_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
_INPUT_SAMPLE_RATE = 16000  # talkflow/features/liveassist/capture.py's SAMPLE_RATE
_API_SAMPLE_RATE = 24000  # required by the Realtime API's audio.input.format
_CONNECT_TIMEOUT_S = 10.0
_COMMIT_TIMEOUT_S = 8.0


def _resample_to_api_rate(pcm16: bytes) -> bytes:
    """16kHz -> 24kHz via linear interpolation - good enough for speech ASR,
    avoids pulling in scipy just for this one fixed ratio."""
    samples = np.frombuffer(pcm16, dtype=np.int16)
    if samples.size == 0:
        return b""
    out_n = int(round(samples.size * _API_SAMPLE_RATE / _INPUT_SAMPLE_RATE))
    x_old = np.linspace(0.0, 1.0, samples.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, out_n, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.int16).tobytes()


def _rms(pcm16: bytes) -> float:
    samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float64)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples * samples)))


class _NoiseGate:
    """Confirmed in Persua's own code: a per-source RMS gate with a hangover
    window (so a brief pause mid-sentence doesn't cut audio early) decides
    what's forwarded to the socket. frame_ms=30 matches capture.py's frame
    size, so hangover_frames=10 is a 300ms tail - the same figure Persua's
    own noise-gate config documents."""

    def __init__(self, rms_threshold: float = 500.0, hangover_frames: int = 10):
        self._threshold = rms_threshold
        self._hangover_frames = hangover_frames
        self._silence_run = hangover_frames  # start closed - no speech seen yet

    def should_send(self, pcm16: bytes) -> bool:
        if _rms(pcm16) >= self._threshold:
            self._silence_run = 0
            return True
        self._silence_run += 1
        return self._silence_run <= self._hangover_frames


class _SourceConnection:
    """One persistent WebSocket for one source_id ("me"/"they")."""

    def __init__(self, api_key: str, model: str, allowed_languages: list[str]):
        self._model = model
        self._allowed_languages = allowed_languages
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._latest_partial = ""
        self._last_returned_partial = ""
        self._committed_item_id: str | None = None
        self._completed_text: dict[str, str] = {}
        self._completed_event = threading.Event()
        self._empty_commit = threading.Event()
        self._session_config_error: str | None = None
        # Set when the server confirms our session.update - either with an
        # explicit "session.updated" ack or an "error" event naming it
        # rejected (confirmed live: the real API sends "session.created" on
        # connect, then "session.updated" once the config we sent is
        # actually applied, or "error" if it's rejected - there's a real ack
        # event, not just "no error arrived within some window", which is
        # not evidence of anything other than nothing having failed *yet*).
        self._session_update_acked = threading.Event()
        self._ws = websocket.WebSocketApp(
            _REALTIME_URL,
            header=[f"Authorization: Bearer {api_key}"],
            on_open=self._on_open, on_message=self._on_message,
            on_error=self._on_error, on_close=self._on_close,
        )
        self._thread = threading.Thread(target=self._ws.run_forever, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=_CONNECT_TIMEOUT_S):
            raise RuntimeError("Live Assist realtime ASR: timed out connecting to OpenAI")
        if not self._session_update_acked.wait(timeout=_CONNECT_TIMEOUT_S):
            self.close()
            raise RuntimeError("Live Assist realtime ASR: timed out waiting for session.updated "
                                "acknowledgement - treating this as a failure, not as ready, since "
                                "no response is not evidence the config was ever applied")
        if self._session_config_error:
            self.close()
            raise RuntimeError(f"Live Assist realtime ASR: session setup rejected: {self._session_config_error}")

    def _on_open(self, ws) -> None:
        # The Realtime API only accepts bare language codes ("pt", not
        # "pt-BR") - confirmed live: sending "pt-BR" rejects the whole
        # session.update with invalid_value, which silently broke every
        # subsequent commit for the rest of the session (every finalize
        # timed out, nothing ever transcribed).
        languages = [base_language(lang) for lang in self._allowed_languages] or None
        ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": _API_SAMPLE_RATE},
                        "transcription": {
                            "model": self._model,
                            "languages": languages,
                        },
                        # We drive turn boundaries ourselves (VAD + hard cap
                        # in orchestrator.py) via explicit commit, not the
                        # model's own server-side VAD.
                        "turn_detection": None,
                    },
                },
            },
        }))
        self._ready.set()

    def _on_message(self, ws, raw: str) -> None:
        try:
            event = json.loads(raw)
        except ValueError:
            return
        etype = event.get("type")
        if etype == "session.updated":
            self._session_update_acked.set()
        elif etype == "conversation.item.input_audio_transcription.delta":
            with self._lock:
                self._latest_partial += event.get("delta", "")
        elif etype == "conversation.item.input_audio_transcription.completed":
            with self._lock:
                self._completed_text[event.get("item_id")] = event.get("transcript", "")
            self._completed_event.set()
        elif etype == "conversation.item.input_audio_transcription.failed":
            with self._lock:
                self._completed_text[event.get("item_id")] = ""
            self._completed_event.set()
        elif etype == "input_audio_buffer.committed":
            with self._lock:
                self._committed_item_id = event.get("item_id")
            self._completed_event.set()
        elif etype == "error":
            error = event.get("error") or {}
            if error.get("code") == "input_audio_buffer_commit_empty":
                # Expected and common: the noise gate let through nothing
                # since the last commit (a genuinely silent turn on this
                # source, e.g. "they" when the other side hasn't spoken).
                # No "committed" event follows this, so without handling it
                # explicitly commit_and_wait() would block for the full
                # _COMMIT_TIMEOUT_S every single time - confirmed live.
                self._empty_commit.set()
                self._completed_event.set()
            elif str(error.get("param") or "").startswith("session."):
                self._session_config_error = error.get("message", str(error))
                self._session_update_acked.set()  # unblock the connect wait immediately, don't time out
            else:
                logger.warning("Live Assist realtime ASR error event: %s", error)

    def _on_error(self, ws, error) -> None:
        logger.warning("Live Assist realtime ASR socket error: %s", error)
        # Unblock a connect-time wait for the ack that will now never
        # arrive - without this, a transport error during setup would just
        # stall the __init__ wait for the full timeout instead of failing
        # promptly.
        self._session_update_acked.set()

    def _on_close(self, ws, code, msg) -> None:
        logger.info("Live Assist realtime ASR socket closed: %s %s", code, msg)
        self._ready.clear()
        self._session_update_acked.set()  # same reasoning as _on_error - don't stall a connect-time wait

    def send_audio(self, pcm16_24k: bytes) -> None:
        if not self._ready.is_set():
            return
        try:
            self._ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm16_24k).decode("ascii"),
            }))
        except Exception:
            logger.exception("Live Assist realtime ASR: failed to send audio")

    def take_partial(self) -> str | None:
        with self._lock:
            if self._latest_partial and self._latest_partial != self._last_returned_partial:
                self._last_returned_partial = self._latest_partial
                return self._latest_partial
        return None

    def commit_and_wait(self) -> str:
        if not self._ready.is_set():
            return ""
        with self._lock:
            self._completed_text.clear()
            self._committed_item_id = None
            self._latest_partial = ""
            self._last_returned_partial = ""
        self._completed_event.clear()
        self._empty_commit.clear()
        try:
            self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        except Exception:
            logger.exception("Live Assist realtime ASR: failed to commit")
            return ""

        deadline = time.monotonic() + _COMMIT_TIMEOUT_S
        committed_id = None
        while committed_id is None and time.monotonic() < deadline:
            if self._empty_commit.is_set():
                return ""
            with self._lock:
                committed_id = self._committed_item_id
            if committed_id is None:
                self._completed_event.wait(timeout=0.5)
                self._completed_event.clear()
        if committed_id is None:
            # Nothing was buffered to commit - the noise gate dropped
            # everything since the last commit (a genuinely silent turn).
            return ""

        while time.monotonic() < deadline:
            with self._lock:
                if committed_id in self._completed_text:
                    return self._completed_text[committed_id]
            self._completed_event.wait(timeout=0.5)
            self._completed_event.clear()
        logger.warning("Live Assist realtime ASR: timed out waiting for final transcript")
        return ""

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


class TwoSourceRealtimeAsrAdapter:
    def __init__(self, api_key: str, model: str = "gpt-live-transcribe"):
        self._api_key = api_key
        self._model = model
        self._connections: dict[str, _SourceConnection] = {}
        self._gates: dict[str, _NoiseGate] = {}

    def start_source(self, source_id: str, allowed_languages: list[str]) -> None:
        if source_id not in self._connections:
            self._connections[source_id] = _SourceConnection(self._api_key, self._model, allowed_languages)
            self._gates[source_id] = _NoiseGate()

    def feed_source_chunk(self, source_id: str, pcm: bytes) -> PartialResult | None:
        gate = self._gates.get(source_id)
        conn = self._connections.get(source_id)
        if gate is None or conn is None:
            return None
        if gate.should_send(pcm):
            conn.send_audio(_resample_to_api_rate(pcm))
        text = conn.take_partial()
        return PartialResult(text=text, detected_language=None) if text else None

    def finalize_source(self, source_id: str) -> FinalResult:
        conn = self._connections.get(source_id)
        if conn is None:
            return FinalResult(text="", detected_language=None, detected_language_confidence=None)
        return FinalResult(text=conn.commit_and_wait().strip(),
                            detected_language=None, detected_language_confidence=None)

    def close(self) -> None:
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()
        self._gates.clear()
