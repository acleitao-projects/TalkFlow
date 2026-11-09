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
- CONFIRMED LIVE: `inputTranscription` is CUMULATIVE for an ongoing turn,
  not a delta - and the orchestrator's own local VAD boundary (300-600ms
  pause) fires finalize_source() far more often than Gemini's own
  server-side turn boundary does during a long, mostly-uninterrupted
  utterance. Seen directly in real logs: a final arriving a few seconds
  after an earlier one contained that ENTIRE earlier text again plus new
  words - sometimes with minor revision right at the seam (e.g. a period
  becoming a comma as more audio arrived), so it isn't even a byte-exact
  prefix extension every time. The first version of this adapter both (a)
  wrongly re-appended each already-cumulative event onto its own buffer,
  compounding the duplication, and (b) never tracked what had already been
  handed to the orchestrator, so every local VAD boundary during one long
  turn re-sent the whole thing-so-far as a brand "final" utterance - this
  is what showed up as repeated/runaway text in the bubble UI (the bubble
  merge logic just concatenates consecutive same-speaker finals, so
  already-duplicated ASR output got doubly concatenated on render; the fix
  belongs here, not in the overlay). Fixed by (1) REPLACING
  `_latest_final` on each final event instead of appending, and (2)
  `commit_and_wait()` diffing the new cumulative text against
  `_last_committed_final` (word-level longest-common-match via difflib,
  not a strict prefix check, specifically to tolerate that seam revision)
  and returning only the genuinely new tail. A `voiceActivity.
  ACTIVITY_START` event resets the baseline cleanly when Gemini itself
  signals a new turn, independent of the diff heuristic.
"""
from __future__ import annotations

import base64
import difflib
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


def _delta_text(already_committed: str, cumulative: str) -> str:
    """Returns the genuinely new tail of `cumulative` beyond whatever was
    already handed to the orchestrator (`already_committed`) - see the
    module docstring's CONFIRMED LIVE entry on why this exists: Gemini's
    `inputTranscription` for an ongoing turn is cumulative, not a delta,
    and can slightly revise already-finalized wording right at the seam
    (not just append to it), so a strict substring/prefix check isn't
    reliable. Word-level longest-common-match tolerates that: it finds the
    biggest contiguous run of words the two texts agree on and returns
    whatever comes after it in `cumulative` - a few words right at a
    revised seam can occasionally still repeat, but the dominant failure
    mode (the whole turn-so-far re-appearing as a "new" final) is gone."""
    if not already_committed:
        return cumulative
    old_words = already_committed.split()
    new_words = cumulative.split()
    matcher = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False)
    match = matcher.find_longest_match(0, len(old_words), 0, len(new_words))
    if match.size == 0:
        return cumulative  # no real overlap - treat as an entirely new turn
    return " ".join(new_words[match.b + match.size:])


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
        # What's already been handed back via commit_and_wait() for the
        # CURRENT ongoing turn - see _delta_text()'s docstring for why this
        # is needed (Gemini's final text is cumulative, not a delta).
        # Reset on a real ACTIVITY_START, so a genuinely new turn doesn't
        # get diffed against the previous one's tail.
        self._last_committed_final = ""
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
                            "- the scheduled hot-swap (see TwoSourceGeminiLiveAsrAdapter) should "
                            "already be ahead of this in normal use", self._source_id, time_left)
            return
        activity = event.get("voiceActivity")
        if activity:
            if activity.get("type") == "ACTIVITY_START":
                # A genuinely new turn, per Gemini itself - the cumulative
                # final text it sends from here on is for THIS turn, so the
                # dedup baseline must restart here too (see _delta_text()).
                with self._lock:
                    self._last_committed_final = ""
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
                # REPLACE, not append - `text` is already the cumulative
                # text for the whole ongoing turn (see _delta_text()'s
                # docstring). Appending it onto itself was the other half
                # of the duplication bug: each new cumulative event was
                # being glued onto the previous one instead of replacing it.
                self._latest_final = text
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
            cumulative = self._latest_final.strip()
            if not cumulative and self._latest_partial.strip():
                # Nothing finalized in time - don't lose what we have.
                cumulative = self._latest_partial.strip()
            # `cumulative` covers this WHOLE ongoing turn, not just what's
            # new since the last commit (see _delta_text()'s docstring) -
            # every caller of this adapter treats whatever is returned here
            # as new, permanent final text, so only the genuinely new tail
            # must go out, and the baseline must advance to match, or the
            # next commit on this same turn would re-send it all again.
            text = _delta_text(self._last_committed_final, cumulative) if cumulative else ""
            if cumulative:
                self._last_committed_final = cumulative
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
    """hotswap_interval_s: proactively replace each source's connection with
    a freshly-warmed one after this long, well ahead of Gemini's own
    ~10-minute connection lifetime (see the module docstring's GoAway
    entry) - that limit is confirmed real but its warning (`timeLeft`) has
    no documented minimum, so waiting for GoAway and reconnecting reactively
    risks too little notice to warm a replacement in time. The swap itself
    only happens inside finalize_source(), right after commit_and_wait()
    cleanly closes out whatever was pending on the OLD connection - never
    mid-utterance - so no audio or in-progress text is lost: everything the
    old connection had is already returned normally via that same call,
    and the new connection only starts receiving audio from the very next
    send_audio(). Default of 8 minutes leaves 2 minutes of margin under the
    documented ~10-minute limit for the next natural VAD boundary (typically
    well under a second away during real speech) to actually trigger the
    swap - configurable since that margin may need adjusting once this is
    observed over real long sessions."""

    def __init__(self, api_key: str, model: str = "gemini-3.5-transcribe-live", hotswap_interval_s: float = 480.0):
        self._api_key = api_key
        self._model = model
        self._hotswap_interval_s = hotswap_interval_s
        self._connections: dict[str, _SourceConnection] = {}
        self._connection_started_at: dict[str, float] = {}
        self._allowed_languages: dict[str, list[str]] = {}
        self._warm_connections: dict[str, _SourceConnection] = {}
        self._warming: set[str] = set()  # source_ids with a warm-up thread currently in flight
        self._lock = threading.Lock()

    def start_source(self, source_id: str, allowed_languages: list[str]) -> None:
        if source_id not in self._connections:
            self._connections[source_id] = _SourceConnection(
                self._api_key, self._model, allowed_languages, source_id=source_id)
            self._connection_started_at[source_id] = time.monotonic()
            self._allowed_languages[source_id] = allowed_languages

    def _maybe_start_warming_replacement(self, source_id: str) -> None:
        with self._lock:
            if source_id in self._warm_connections or source_id in self._warming:
                return  # already warm and waiting, or a warm-up is already in flight
            started_at = self._connection_started_at.get(source_id)
            if started_at is None or time.monotonic() - started_at < self._hotswap_interval_s:
                return
            self._warming.add(source_id)

        def _warm() -> None:
            try:
                conn = _SourceConnection(
                    self._api_key, self._model, self._allowed_languages[source_id], source_id=source_id)
                with self._lock:
                    self._warm_connections[source_id] = conn
                logger.info("Live Assist Gemini Live ASR [%s]: hot-swap replacement warmed up and ready",
                            source_id)
            except Exception:
                logger.exception("Live Assist Gemini Live ASR [%s]: hot-swap warm-up failed - will retry "
                                  "on the next check", source_id)
            finally:
                with self._lock:
                    self._warming.discard(source_id)

        threading.Thread(target=_warm, name=f"gemini-hotswap-{source_id}", daemon=True).start()

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
        self._maybe_start_warming_replacement(source_id)
        conn.send_audio(pcm)
        text = conn.take_partial()
        return PartialResult(text=text, detected_language=None) if text else None

    def finalize_source(self, source_id: str) -> FinalResult:
        conn = self._connections.get(source_id)
        if conn is None:
            return FinalResult(text="", detected_language=None, detected_language_confidence=None)
        text = conn.commit_and_wait()
        # Safe point to hot-swap: `conn` has just cleanly finished whatever
        # utterance was pending (returned above, in `text`), so nothing is
        # mid-word. If a warmed replacement is ready, switch to it now -
        # every send_audio() from here on goes to the new connection, and
        # the old one is closed only after the handoff.
        with self._lock:
            warm = self._warm_connections.pop(source_id, None)
            if warm is not None:
                old = self._connections[source_id]
                self._connections[source_id] = warm
                self._connection_started_at[source_id] = time.monotonic()
        if warm is not None:
            old.close()
            logger.info("Live Assist Gemini Live ASR [%s]: hot-swapped to a fresh connection "
                        "ahead of the ~10min limit", source_id)
        return FinalResult(text=text, detected_language=None, detected_language_confidence=None)

    def close(self) -> None:
        for conn in self._connections.values():
            conn.close()
        for conn in self._warm_connections.values():
            conn.close()
        self._connections.clear()
        self._connection_started_at.clear()
        self._allowed_languages.clear()
        self._warm_connections.clear()
        self._warming.clear()
