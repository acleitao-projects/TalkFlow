"""TwoSourceDeepgramAsrAdapter - true low-latency streaming transcription for
Live Assist via Deepgram's real-time WebSocket API, as a cheaper alternative
to realtime_asr.py's OpenAI Realtime adapter.

Why this exists: confirmed against both vendors' official docs (not blog
estimates) that Deepgram's Nova-3 streaming is roughly half OpenAI Realtime's
per-minute cost ($0.0077/min vs $0.017/min), bills per second of audio
actually processed rather than connection-open time (same "don't bill
silence" property realtime_asr.py's noise gate already exploits for OpenAI),
and documents sub-300ms transcription latency - fast enough for live
captions. Same two-connection-per-session shape as realtime_asr.py
(start_source/feed_source_chunk/finalize_source/close), so it's a close port
rather than a redesign - with one real simplification: Deepgram's streaming
API accepts raw 16kHz linear16 PCM directly, so the 16kHz->24kHz resampling
realtime_asr.py needs for OpenAI isn't needed here at all.

Protocol specifics (confirmed via Deepgram's own docs, not inferred):
- Auth: `Authorization: Token <api_key>` header, same for the WebSocket as
  for REST.
- All session config (model, language, encoding, sample_rate, interim
  results, endpointing) is passed as query params on the connect URL, not a
  separate session.update message like OpenAI's - validated during the HTTP
  upgrade handshake itself, so a successful on_open is sufficient proof the
  config was accepted (no extra ack round-trip needed, unlike OpenAI's
  session.updated wait in realtime_asr.py).
- Audio is sent as raw binary WebSocket frames (no base64/JSON envelope).
- `{"type": "Finalize"}` (JSON text frame) forces the server to flush
  whatever's buffered and return final text for it without closing the
  connection - this is Deepgram's equivalent of OpenAI's
  input_audio_buffer.commit, used here for finalize_source(). Per Deepgram's
  docs this isn't strictly guaranteed to produce a response if there's
  nothing significant buffered, so commit_and_wait() still needs a timeout
  fallback, same shape as realtime_asr.py's.
- Nova-3 "multi" mode auto-detects/code-switches across its supported
  language set when more than one language is allowed; a single allowed
  language uses Nova-3's more accurate monolingual mode instead.
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

_WS_URL = "wss://api.deepgram.com/v1/listen"
_SAMPLE_RATE = 16000  # matches capture.py's SAMPLE_RATE - no resampling needed
_CONNECT_TIMEOUT_S = 10.0
# Confirmed live as the actual cause of Live Assist feeling slower than the
# local Nemotron path: orchestrator.py's ChunkBoundaryDetector calls
# finalize_source() on EVERY VAD boundary - at minimum every ~400ms of
# silence, at most every 3s during continuous speech (orchestrator.py's
# `_boundaries`/ChunkBoundaryDetector) - and that call runs synchronously on
# the audio drain thread (capture.py's _drain_loop), blocking it until this
# wait returns. Deepgram's own docs say the `from_finalize` ack "is not
# guaranteed if there is no significant amount of audio to process" - a
# boundary fired on just 400ms of silence is exactly that case, so a 5s
# timeout here meant the drain thread could stall for a full 5 seconds on
# nearly every boundary, repeatedly, for a source that's actually talking
# normally. Local Nemotron's finalize_source() has no network wait at all
# (pure local compute, <50ms), so this was the real "worse than local"
# latency source, not Deepgram's actual transcription speed (confirmed
# sub-300ms). 300ms keeps real margin for a genuine in-flight ack without
# risking a multi-second stall on the common no-ack case.
_FINALIZE_TIMEOUT_S = 0.3
# Confirmed live and in Deepgram's own docs (developers.deepgram.com/docs/
# keep-alive): the server closes the socket (NET-0001, "no audio received
# recently") if it sees neither real audio nor a KeepAlive message within
# 10 seconds. Our noise gate deliberately withholds audio during silence to
# avoid being billed for it (same trick as realtime_asr.py's for OpenAI) -
# with two sources that don't talk at the same time, "the other source is
# quiet while this one talks" alone can exceed 10s, so without a KeepAlive
# the quiet source's connection would get killed mid-session and silently
# stop transcribing from then on. Docs recommend sending one every 3-5s;
# 3s leaves real margin under the 10s cutoff.
_KEEPALIVE_INTERVAL_S = 3.0


def _language_param(allowed_languages: list[str]) -> str:
    languages = [lang.strip() for lang in (allowed_languages or []) if lang.strip()]
    if len(languages) == 1:
        return languages[0]
    return "multi"  # Nova-3 multilingual auto-detection/code-switching


class _SourceConnection:
    """One persistent WebSocket for one source_id ("me"/"they")."""

    def __init__(self, api_key: str, model: str, allowed_languages: list[str], source_id: str = "?"):
        self._source_id = source_id
        self._lock = threading.Lock()
        self._ready = threading.Event()
        # Diagnostic timing only (LA-11 speed investigation) - not used for
        # any decision-making, just logged. Marks when the FIRST audio frame
        # of a fresh utterance was sent, so we can log real "audio sent" ->
        # "text received" latency instead of inferring it from gaps between
        # log lines (which conflate real lag with normal pauses in speech).
        self._utterance_audio_started_at: float | None = None
        self._connect_error: str | None = None
        self._latest_partial = ""
        self._last_returned_partial = ""
        self._final_pieces: list[str] = []
        self._finalize_seen = threading.Event()
        # Deepgram's own server-side endpointing (we set endpointing=300)
        # already detects end-of-turn on its own and marks the Results
        # message that closes it with speech_final=true - no request from us
        # needed. Confirmed in Deepgram's docs: "trigger when a transcript
        # with speech_final=true is received." Tracked separately from
        # _finalize_seen (which just means "some is_final result arrived,"
        # not necessarily a turn boundary) so commit_and_wait() can skip
        # sending an explicit Finalize + waiting when this already fired -
        # that was costing a full extra network round trip on nearly every
        # utterance, since our own orchestrator's local VAD boundary (400ms
        # silence) almost always lands after Deepgram's 300ms endpointing
        # has already closed the segment server-side.
        self._speech_final_seen = threading.Event()
        self._last_sent_at = time.monotonic()

        url = (
            f"{_WS_URL}?model={model}&language={_language_param(allowed_languages)}"
            f"&encoding=linear16&sample_rate={_SAMPLE_RATE}&channels=1"
            f"&interim_results=true&endpointing=300&utterance_end_ms=1000"
        )
        self._ws = websocket.WebSocketApp(
            url,
            header=[f"Authorization: Token {api_key}"],
            on_open=self._on_open, on_message=self._on_message,
            on_error=self._on_error, on_close=self._on_close,
        )
        self._thread = threading.Thread(target=self._ws.run_forever, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=_CONNECT_TIMEOUT_S):
            self.close()
            raise RuntimeError("Live Assist Deepgram ASR: timed out connecting")
        if self._connect_error:
            self.close()
            raise RuntimeError(f"Live Assist Deepgram ASR: connection rejected: {self._connect_error}")

    def _on_open(self, ws) -> None:
        # Config is already validated via the connect URL's query params at
        # this point (a rejected config fails the handshake itself, caught
        # by _on_error/_on_close below) - no separate ack message to wait for.
        self._ready.set()

    def _on_message(self, ws, raw: str) -> None:
        try:
            event = json.loads(raw)
        except ValueError:
            return
        etype = event.get("type")
        if etype == "Results":
            alternatives = (event.get("channel") or {}).get("alternatives") or [{}]
            text = alternatives[0].get("transcript", "")
            if text and self._utterance_audio_started_at is not None:
                elapsed_ms = (time.monotonic() - self._utterance_audio_started_at) * 1000
                logger.info("Live Assist Deepgram ASR [%s]: first text %.0fms after audio started "
                            "(is_final=%s speech_final=%s)",
                            self._source_id, elapsed_ms, event.get("is_final"), event.get("speech_final"))
                self._utterance_audio_started_at = None  # only log the first result per utterance
            if event.get("is_final"):
                with self._lock:
                    if text:
                        self._final_pieces.append(text)
                    self._latest_partial = ""
                    self._last_returned_partial = ""
                self._finalize_seen.set()
                if event.get("speech_final"):
                    self._speech_final_seen.set()
            elif text:
                with self._lock:
                    self._latest_partial = text
        elif etype == "Error":
            logger.warning("Live Assist Deepgram ASR error event: %s", event)

    def _on_error(self, ws, error) -> None:
        logger.warning("Live Assist Deepgram ASR socket error: %s", error)
        self._connect_error = str(error)
        self._ready.set()  # unblock a connect-time wait that will now never succeed
        self._finalize_seen.set()  # same reasoning for a finalize-time wait

    def _on_close(self, ws, code, msg) -> None:
        logger.info("Live Assist Deepgram ASR socket closed: %s %s", code, msg)
        if not self._ready.is_set():
            self._connect_error = self._connect_error or f"closed before ready: {code} {msg}"
        self._ready.set()
        self._finalize_seen.set()

    def send_audio(self, pcm16: bytes) -> None:
        self._last_sent_at = time.monotonic()
        if self._utterance_audio_started_at is None:
            self._utterance_audio_started_at = self._last_sent_at
        try:
            self._ws.send(pcm16, opcode=websocket.ABNF.OPCODE_BINARY)
        except Exception:
            logger.exception("Live Assist Deepgram ASR: failed to send audio")

    def maybe_send_keepalive(self) -> None:
        """Called instead of send_audio() for frames the noise gate withheld -
        keeps the connection alive through a quiet source without paying for
        (or sending) the silence itself."""
        now = time.monotonic()
        if now - self._last_sent_at < _KEEPALIVE_INTERVAL_S:
            return
        self._last_sent_at = now
        try:
            self._ws.send(json.dumps({"type": "KeepAlive"}))
        except Exception:
            logger.exception("Live Assist Deepgram ASR: failed to send KeepAlive")

    def take_partial(self) -> str | None:
        with self._lock:
            if self._latest_partial and self._latest_partial != self._last_returned_partial:
                self._last_returned_partial = self._latest_partial
                return self._latest_partial
        return None

    def commit_and_wait(self) -> str:
        if self._speech_final_seen.is_set():
            # Deepgram's own endpointing already closed this segment
            # server-side (speech_final=true already arrived) - our local
            # VAD boundary just caught up to a decision Deepgram already
            # made, so there's nothing to request and nothing to wait for.
            self._speech_final_seen.clear()
            return self._drain_final_pieces()

        self._finalize_seen.clear()
        try:
            self._ws.send(json.dumps({"type": "Finalize"}))
        except Exception:
            logger.exception("Live Assist Deepgram ASR: failed to send Finalize")
        # Finalize isn't guaranteed to produce a response when nothing
        # significant was buffered (per Deepgram's own docs) - the timeout
        # is a normal, expected exit path here, not just an error fallback.
        acked = self._finalize_seen.wait(timeout=_FINALIZE_TIMEOUT_S)
        if not acked:
            logger.debug("Live Assist Deepgram ASR: Finalize ack timed out after %.2fs (expected sometimes)",
                         _FINALIZE_TIMEOUT_S)
        self._speech_final_seen.clear()
        return self._drain_final_pieces()

    def _drain_final_pieces(self) -> str:
        with self._lock:
            text = " ".join(piece.strip() for piece in self._final_pieces if piece.strip())
            self._final_pieces = []
            self._latest_partial = ""
            self._last_returned_partial = ""
        self._utterance_audio_started_at = None  # next send_audio() starts a fresh utterance clock
        return text.strip()

    def close(self) -> None:
        try:
            self._ws.send(json.dumps({"type": "CloseStream"}))
        except Exception:
            pass
        try:
            self._ws.close()
        except Exception:
            pass


class TwoSourceDeepgramAsrAdapter:
    def __init__(self, api_key: str, model: str = "nova-3"):
        self._api_key = api_key
        self._model = model
        self._connections: dict[str, _SourceConnection] = {}
        self._gates: dict[str, _NoiseGate] = {}

    def start_source(self, source_id: str, allowed_languages: list[str]) -> None:
        if source_id not in self._connections:
            self._connections[source_id] = _SourceConnection(
                self._api_key, self._model, allowed_languages, source_id=source_id)
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
        return FinalResult(text=conn.commit_and_wait(), detected_language=None, detected_language_confidence=None)

    def close(self) -> None:
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()
        self._gates.clear()
