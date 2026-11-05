"""TwoSourceWhisperLiveKitAdapter - true low-latency streaming transcription
for Live Assist via a self-hosted WhisperLiveKit server (faster-whisper
backend), as a free, LAN-local alternative after Deepgram's real-world
latency (1.4-3s, confirmed via live timing logs) turned out far worse than
its documented sub-300ms claim - likely network/routing related, not fixable
from our side.

Why self-hosted: a dual-Xeon R730 on the LAN (24 cores/48 threads, mostly
idle) has far more real headroom than this laptop's throttled CPU sherpa-onnx
setup, and a LAN round trip (sub-5ms) removes the WAN latency variable
entirely - see the container setup at 192.168.1.36 (LXC vmid 141,
"whisper-live", systemd service `whisper-live.service`, faster-whisper
"small" model). Zero cost beyond electricity already being paid for an idle
box.

WhisperLiveKit exposes a Deepgram-API-compatible endpoint
(`deepgram_compat.py`) alongside its native one, which is why this module is
a close port of deepgram_asr.py - same Results-message shape (`is_final`,
`speech_final`, `channel.alternatives[0].transcript`), same raw-binary-PCM
send, same KeepAlive mechanism. One real protocol difference, confirmed by
reading WhisperLiveKit's own source before assuming compatibility:
**`{"type": "Finalize"}` is NOT supported here** - WhisperLiveKit's
architecture can't force a flush the way Deepgram can, and sending it
actually gets the connection closed (code 4400, "Finalize is unsupported").
So unlike deepgram_asr.py, finalize_source() never sends anything - it
relies entirely on the server's own natural `speech_final` signal (its own
endpointing decides turn boundaries same as Deepgram's did) and returns
immediately with whatever's accumulated, even if that's empty. No explicit
request, no wait, no risk of killing the connection.

No API key: this is our own LAN server with no --api-token configured.
"""
from __future__ import annotations

import json
import logging
import threading
import time

import websocket

from talkflow.asr.base import FinalResult, PartialResult
from talkflow.features.liveassist.realtime_asr import _NoiseGate

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000  # matches capture.py's SAMPLE_RATE - no resampling needed
_CONNECT_TIMEOUT_S = 10.0
# WhisperLiveKit's own idle cutoff is 30s ("No audio or KeepAlive message was
# received for 30 seconds", confirmed in its source) - more forgiving than
# real Deepgram's 10s, but the same KeepAlive trick still applies so a quiet
# source never risks hitting it.
_KEEPALIVE_INTERVAL_S = 10.0


def _language_param(allowed_languages: list[str]) -> str:
    languages = [lang.strip() for lang in (allowed_languages or []) if lang.strip()]
    if len(languages) == 1:
        return languages[0]
    return "multi"


class _SourceConnection:
    """One persistent WebSocket for one source_id ("me"/"they")."""

    def __init__(self, base_url: str, allowed_languages: list[str], source_id: str = "?"):
        self._source_id = source_id
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._connect_error: str | None = None
        self._latest_partial = ""
        self._last_returned_partial = ""
        self._final_pieces: list[str] = []
        self._utterance_audio_started_at: float | None = None
        self._last_sent_at = time.monotonic()

        url = (
            f"{base_url}/v1/listen?language={_language_param(allowed_languages)}"
            f"&encoding=linear16&sample_rate={_SAMPLE_RATE}&channels=1&interim_results=true"
        )
        self._ws = websocket.WebSocketApp(
            url,
            on_open=self._on_open, on_message=self._on_message,
            on_error=self._on_error, on_close=self._on_close,
        )
        self._thread = threading.Thread(target=self._ws.run_forever, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=_CONNECT_TIMEOUT_S):
            self.close()
            raise RuntimeError("Live Assist WhisperLiveKit ASR: timed out connecting")
        if self._connect_error:
            self.close()
            raise RuntimeError(f"Live Assist WhisperLiveKit ASR: connection rejected: {self._connect_error}")

    def _on_open(self, ws) -> None:
        self._ready.set()

    def _on_message(self, ws, raw: str) -> None:
        try:
            event = json.loads(raw)
        except ValueError:
            return
        if event.get("type") != "Results":
            return
        alternatives = (event.get("channel") or {}).get("alternatives") or [{}]
        text = alternatives[0].get("transcript", "")
        if text and self._utterance_audio_started_at is not None:
            elapsed_ms = (time.monotonic() - self._utterance_audio_started_at) * 1000
            logger.info("Live Assist WhisperLiveKit ASR [%s]: first text %.0fms after audio started "
                        "(is_final=%s speech_final=%s)",
                        self._source_id, elapsed_ms, event.get("is_final"), event.get("speech_final"))
            self._utterance_audio_started_at = None
        if event.get("is_final"):
            with self._lock:
                if text:
                    self._final_pieces.append(text)
                self._latest_partial = ""
                self._last_returned_partial = ""
        elif text:
            with self._lock:
                self._latest_partial = text

    def _on_error(self, ws, error) -> None:
        logger.warning("Live Assist WhisperLiveKit ASR socket error: %s", error)
        self._connect_error = str(error)
        self._ready.set()

    def _on_close(self, ws, code, msg) -> None:
        logger.info("Live Assist WhisperLiveKit ASR socket closed: %s %s", code, msg)
        if not self._ready.is_set():
            self._connect_error = self._connect_error or f"closed before ready: {code} {msg}"
        self._ready.set()

    def send_audio(self, pcm16: bytes) -> None:
        self._last_sent_at = time.monotonic()
        if self._utterance_audio_started_at is None:
            self._utterance_audio_started_at = self._last_sent_at
        try:
            self._ws.send(pcm16, opcode=websocket.ABNF.OPCODE_BINARY)
        except Exception:
            logger.exception("Live Assist WhisperLiveKit ASR: failed to send audio")

    def maybe_send_keepalive(self) -> None:
        now = time.monotonic()
        if now - self._last_sent_at < _KEEPALIVE_INTERVAL_S:
            return
        self._last_sent_at = now
        try:
            self._ws.send(json.dumps({"type": "KeepAlive"}))
        except Exception:
            logger.exception("Live Assist WhisperLiveKit ASR: failed to send KeepAlive")

    def take_partial(self) -> str | None:
        with self._lock:
            if self._latest_partial and self._latest_partial != self._last_returned_partial:
                self._last_returned_partial = self._latest_partial
                return self._latest_partial
        return None

    def take_final(self) -> str:
        """No Finalize request exists here (sending one closes the
        connection, code 4400 - confirmed in WhisperLiveKit's source). Just
        return whatever the server has already naturally finalized
        (speech_final-driven, same endpointing concept as Deepgram's) - zero
        network wait, zero risk, possibly empty if nothing closed yet."""
        with self._lock:
            text = " ".join(piece.strip() for piece in self._final_pieces if piece.strip())
            self._final_pieces = []
            self._latest_partial = ""
            self._last_returned_partial = ""
        self._utterance_audio_started_at = None
        return text.strip()

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


class TwoSourceWhisperLiveKitAdapter:
    def __init__(self, base_url: str):
        self._base_url = base_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
        self._connections: dict[str, _SourceConnection] = {}
        self._gates: dict[str, _NoiseGate] = {}

    def start_source(self, source_id: str, allowed_languages: list[str]) -> None:
        if source_id not in self._connections:
            self._connections[source_id] = _SourceConnection(
                self._base_url, allowed_languages, source_id=source_id)
            self._gates[source_id] = _NoiseGate()

    def feed_source_chunk(self, source_id: str, pcm: bytes) -> PartialResult | None:
        gate = self._gates.get(source_id)
        conn = self._connections.get(source_id)
        if gate is None or conn is None:
            return None
        if gate.should_send(pcm):
            conn.send_audio(pcm)
        else:
            conn.maybe_send_keepalive()
        text = conn.take_partial()
        return PartialResult(text=text, detected_language=None) if text else None

    def finalize_source(self, source_id: str) -> FinalResult:
        conn = self._connections.get(source_id)
        if conn is None:
            return FinalResult(text="", detected_language=None, detected_language_confidence=None)
        return FinalResult(text=conn.take_final(), detected_language=None, detected_language_confidence=None)

    def close(self) -> None:
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()
        self._gates.clear()
