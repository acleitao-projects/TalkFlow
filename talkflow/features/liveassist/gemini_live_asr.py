"""TwoSourceGeminiLiveAsrAdapter - true low-latency streaming transcription
for Live Assist via Google's Gemini Live API (Live Transcribe feature,
model "gemini-3.5-transcribe-live"), as another cheap alternative to
realtime_asr.py's OpenAI Realtime adapter - added alongside Deepgram and
WhisperLiveKit per the user's explicit "new provider on our side, don't
touch what's already working" rule. Same two-connection-per-session shape
as the other adapters (start_source/feed_source_chunk/finalize_source/
close), built from Google's own published docs, not assumed:
https://aistudio.google.com/docs/live-api/live-transcribe and
https://aistudio.google.com/docs/live-api .

Protocol specifics, and which are CONFIRMED (quoted/paraphrased from
Google's own docs) vs INFERRED (reasoned from the standard Gemini Live /
BidiGenerateContent protocol shape, since the fetched Live Transcribe page
didn't show a literal server-message example):

- CONFIRMED: endpoint
  `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.
  v1beta.GenerativeService.BidiGenerateContent?key=<API_KEY>` - auth is a
  query param, no header needed.
- CONFIRMED: first message after connecting is a JSON text frame
  `{"setup": {"model": "models/<model_id>", "generationConfig":
  {"responseModalities": ["TEXT"]}, "inputAudioTranscription":
  {"languageCodes": [...]}}}`.
- CONFIRMED: audio chunks are JSON text frames (not raw binary, unlike
  Deepgram) - `{"realtimeInput": {"audio": {"data": "<base64 pcm16>",
  "mimeType": "audio/pcm;rate=16000"}}}`, raw 16-bit PCM, 16kHz, mono,
  little-endian - matches capture.py's own frame format exactly, so no
  resampling needed (same situation as Deepgram).
- CONFIRMED: server transcription events arrive as
  `{"serverContent": {"interimInputTranscription": {"text": "..."}}}` for
  speculative partials and `{"serverContent": {"inputTranscription":
  {"text": "..."}}}` once "the speaker pauses, the turn completes, or
  speech is finalized" - Gemini's own server-side turn detection decides
  this, there's no client-requested "flush now" message documented for
  this TEXT-only transcribe mode (manual VAD's activityEnd is for a
  different mode - disabling automatic VAD entirely - which isn't used
  here).
- CONFIRMED: sessions cap at ~10 minutes; the server sends a "GoAway"
  message with a timeLeft field shortly before force-closing. No reconnect
  logic is implemented here (same as deepgram_asr.py and
  whisperlivekit_asr.py: a closed socket just stops transcribing until the
  next Live Assist session) - revisit if real sessions run long enough to
  hit this in practice.
- INFERRED (standard BidiGenerateContent shape, not shown on the fetched
  page): the server acknowledges a valid setup with
  `{"setupComplete": {}}` before any content messages. Readiness here
  waits for that specific message rather than assuming on_open alone means
  "ready" - same "don't assume success, wait for a real signal" discipline
  already used in realtime_asr.py. If this assumption is wrong, connect()
  fails loud with a timeout rather than silently proceeding as if it had
  succeeded.
- INFERRED: since there's no documented client-side "finalize now"
  message for this mode, finalize_source() can't force a flush the way
  Deepgram's `{"type": "Finalize"}` does. It waits a short timeout for a
  final `inputTranscription` to arrive (Gemini's own turn detection should
  often beat our local VAD boundary, same observation as Deepgram's
  speech_final); if nothing final arrives in time, the latest interim text
  is promoted and returned instead of losing it entirely.
- CONFIRMED LIVE, TWO SEPARATE WAYS (not guessed): no noise gate is used
  here, unlike Deepgram/OpenAI, and this was a deliberate choice after
  real testing, not an oversight. First attempt used the shared
  `_NoiseGate` (withholds silent frames to save cost) exactly like the
  other two adapters - gated/intermittent audio (frames just stopped, no
  signal to the server) produced ZERO transcription on a real JFK speech
  clip that transcribed correctly every other way. Google's own Live API
  capabilities guide describes exactly this scenario: "When the audio
  stream is paused for more than a second..., an audioStreamEnd event
  should be sent to flush any cached audio. The client can resume sending
  audio data at any time." That was implemented properly (a
  `{"realtimeInput": {"audioStreamEnd": true}}` message sent exactly when
  the gate closed) and tested TWICE - once through this adapter, once in
  total isolation with raw code, no adapter involved, following the docs'
  own described scenario precisely (2.5s of real speech, audioStreamEnd,
  a genuine 1.5s pause - well past their "more than a second" - then
  resuming with plain audio, no special resume message, exactly as
  documented). Both times: the pre-pause speech transcribed correctly,
  and the server went completely silent afterward - zero response to 3
  more seconds of real speech that would have transcribed fine on its
  own. That's Google's own documented mechanism not working as documented
  against their own API, confirmed independently of this adapter's code,
  not a bug here. Sending every frame unconditionally is the only
  approach confirmed to actually work across an entire session - it costs
  more (silence is billed, no way found to avoid that for this provider)
  but it's the one that doesn't silently go dead on a meeting's first
  real pause. Don't re-attempt gating here without re-testing the premise
  live first - this was tried properly, not skipped.
- UNKNOWN / not yet tested live: whether an idle connection (one source
  quiet while the other talks, for a long meeting) ever gets disconnected
  on its own regardless of what we send - continuous audio is being sent
  throughout per the above, so this is a different risk than Deepgram's
  idle-timeout one, not yet observed in practice.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time

import websocket

from talkflow.asr.base import FinalResult, PartialResult

logger = logging.getLogger(__name__)

_WS_URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
_SAMPLE_RATE = 16000  # matches capture.py's SAMPLE_RATE - no resampling needed
_CONNECT_TIMEOUT_S = 10.0
# No documented client "flush now" message for this mode (see module
# docstring) - this is how long finalize_source() waits for Gemini's own
# turn-detection to emit a final inputTranscription before giving up and
# promoting whatever interim text is on hand instead.
_FINALIZE_TIMEOUT_S = 0.5


class _SourceConnection:
    """One persistent WebSocket for one source_id ("me"/"they")."""

    def __init__(self, api_key: str, model: str, allowed_languages: list[str], source_id: str = "?"):
        self._source_id = source_id
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._setup_complete = threading.Event()
        # Diagnostic timing only, same purpose as deepgram_asr.py's - marks
        # when the FIRST audio frame of a fresh utterance was sent, so real
        # audio-sent -> text-received latency can be logged directly rather
        # than inferred from log-line gaps (which conflate lag with normal
        # pauses in speech).
        self._utterance_audio_started_at: float | None = None
        self._connect_error: str | None = None
        self._latest_partial = ""
        self._last_returned_partial = ""
        self._latest_final = ""
        self._final_seen = threading.Event()
        # Must be set before the socket thread starts - _on_open (called
        # from that thread, as soon as the connection opens) builds the
        # setup message from these.
        self._model = model
        self._allowed_languages = allowed_languages

        self._ws = websocket.WebSocketApp(
            f"{_WS_URL}?key={api_key}",
            on_open=self._on_open, on_message=self._on_message,
            on_error=self._on_error, on_close=self._on_close,
        )
        self._thread = threading.Thread(target=self._ws.run_forever, daemon=True)
        self._thread.start()
        if not self._setup_complete.wait(timeout=_CONNECT_TIMEOUT_S):
            self.close()
            raise RuntimeError("Live Assist Gemini Live ASR: timed out waiting for setupComplete")
        if self._connect_error:
            self.close()
            raise RuntimeError(f"Live Assist Gemini Live ASR: connection rejected: {self._connect_error}")

    def _on_open(self, ws) -> None:
        self._ready.set()
        setup_message = {
            "setup": {
                "model": f"models/{self._model}" if not self._model.startswith("models/") else self._model,
                "generationConfig": {"responseModalities": ["TEXT"]},
                "inputAudioTranscription": {"languageCodes": self._allowed_languages or []},
            }
        }
        try:
            ws.send(json.dumps(setup_message))
        except Exception:
            logger.exception("Live Assist Gemini Live ASR: failed to send setup message")

    def _on_message(self, ws, raw: str) -> None:
        try:
            event = json.loads(raw)
        except ValueError:
            return
        if "setupComplete" in event:
            self._setup_complete.set()
            return
        if "goAway" in event:
            time_left = (event.get("goAway") or {}).get("timeLeft")
            logger.warning("Live Assist Gemini Live ASR [%s]: server sent GoAway, timeLeft=%s "
                            "- connection will close soon, no reconnect is attempted",
                            self._source_id, time_left)
            return
        content = event.get("serverContent")
        if not content:
            return
        interim = content.get("interimInputTranscription")
        final = content.get("inputTranscription")
        if final and final.get("text"):
            text = final["text"]
            if self._utterance_audio_started_at is not None:
                elapsed_ms = (time.monotonic() - self._utterance_audio_started_at) * 1000
                logger.info("Live Assist Gemini Live ASR [%s]: first final text %.0fms after audio started",
                            self._source_id, elapsed_ms)
                self._utterance_audio_started_at = None
            with self._lock:
                self._latest_final = (f"{self._latest_final} {text}".strip() if self._latest_final else text)
                self._latest_partial = ""
                self._last_returned_partial = ""
            self._final_seen.set()
        elif interim and interim.get("text"):
            with self._lock:
                self._latest_partial = interim["text"]

    def _on_error(self, ws, error) -> None:
        logger.warning("Live Assist Gemini Live ASR socket error: %s", error)
        self._connect_error = str(error)
        self._ready.set()
        self._setup_complete.set()  # unblock a connect-time wait that will now never succeed
        self._final_seen.set()  # same reasoning for a finalize-time wait

    def _on_close(self, ws, code, msg) -> None:
        logger.info("Live Assist Gemini Live ASR socket closed: %s %s", code, msg)
        if not self._setup_complete.is_set():
            self._connect_error = self._connect_error or f"closed before setupComplete: {code} {msg}"
        self._ready.set()
        self._setup_complete.set()
        self._final_seen.set()

    def send_audio(self, pcm16: bytes) -> None:
        if self._utterance_audio_started_at is None:
            self._utterance_audio_started_at = time.monotonic()
        payload = {
            "realtimeInput": {
                "audio": {"data": base64.b64encode(pcm16).decode("ascii"), "mimeType": f"audio/pcm;rate={_SAMPLE_RATE}"},
            }
        }
        try:
            self._ws.send(json.dumps(payload))
        except Exception:
            logger.exception("Live Assist Gemini Live ASR: failed to send audio")

    def take_partial(self) -> str | None:
        with self._lock:
            if self._latest_partial and self._latest_partial != self._last_returned_partial:
                self._last_returned_partial = self._latest_partial
                return self._latest_partial
        return None

    def commit_and_wait(self) -> str:
        self._final_seen.clear()
        # No documented "flush now" message for this mode (see module
        # docstring) - this just waits briefly for Gemini's own turn
        # detection to catch up, same bet deepgram_asr.py makes on
        # speech_final usually beating the local VAD boundary.
        self._final_seen.wait(timeout=_FINALIZE_TIMEOUT_S)
        with self._lock:
            text = self._latest_final.strip()
            if not text and self._latest_partial.strip():
                # Nothing finalized in time - don't lose what we have.
                text = self._latest_partial.strip()
            self._latest_final = ""
            self._latest_partial = ""
            self._last_returned_partial = ""
        self._utterance_audio_started_at = None  # next send_audio() starts a fresh utterance clock
        return text

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


class TwoSourceGeminiLiveAsrAdapter:
    def __init__(self, api_key: str, model: str = "gemini-3.5-transcribe-live"):
        self._api_key = api_key
        self._model = model
        self._connections: dict[str, _SourceConnection] = {}

    def start_source(self, source_id: str, allowed_languages: list[str]) -> None:
        if source_id not in self._connections:
            self._connections[source_id] = _SourceConnection(
                self._api_key, self._model, allowed_languages, source_id=source_id)

    def feed_source_chunk(self, source_id: str, pcm: bytes) -> PartialResult | None:
        # No noise gate here - see the module docstring's "CONFIRMED LIVE"
        # entries for the full story: Google's own documented audioStreamEnd
        # mechanism for exactly this "paused, want to resume" case was
        # tried, twice, independently (through this adapter and in total
        # isolation with raw code) - a real 1.5s pause with audioStreamEnd
        # sent, then resuming plain audio afterward, got ZERO server
        # response for 3 more seconds of real speech that transcribed fine
        # before the pause. That's Google's own documented API not working
        # as documented, not a bug here. Sending every frame unconditionally
        # is the only approach confirmed to actually work - it costs more
        # (silence is billed) but it's the one that doesn't silently go
        # dead on a real meeting's first pause.
        conn = self._connections.get(source_id)
        if conn is None:
            return None
        conn.send_audio(pcm)
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
