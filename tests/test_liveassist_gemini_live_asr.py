"""Unit-testable pieces of the Gemini Live ASR adapter: the setup/
setupComplete handshake and the serverContent transcription state machine
(using a fake WebSocketApp - no real network). The real socket's actual
latency/accuracy still needs a real Gemini API key and is only verified by
live use, same as the other Live Assist ASR adapters in this project."""
import json
import time

import pytest
import websocket

from talkflow.features.liveassist import gemini_live_asr as gemini_live_asr_module
from talkflow.features.liveassist.gemini_live_asr import (
    TwoSourceGeminiLiveAsrAdapter,
    _SourceConnection,
)


class _FakeWebSocketApp:
    """Stands in for websocket.WebSocketApp - run_forever() synchronously
    fires on_open (which sends the setup message), then delivers whatever
    raw JSON messages the test scripted (simulating the server's response
    stream), with no real network."""

    scripted_events: list[dict] = []  # set per-test via monkeypatch before construction
    fail_before_open = False
    send_setup_complete = True  # most tests want a normal handshake

    def __init__(self, url, on_open=None, on_message=None, on_error=None, on_close=None):
        self.url = url
        self._on_open = on_open
        self._on_message = on_message
        self._on_error = on_error
        self._on_close = on_close
        self.sent: list = []
        self.closed = False

    def send(self, data, opcode=None):
        self.sent.append((data, opcode))

    def run_forever(self):
        if self.fail_before_open:
            self._on_error(self, "handshake rejected")
            self._on_close(self, 401, "unauthorized")
            return
        self._on_open(self)  # sends the setup message, recorded in self.sent
        if self.send_setup_complete:
            self._on_message(self, json.dumps({"setupComplete": {}}))
        for event in self.scripted_events:
            self._on_message(self, json.dumps(event))

    def close(self):
        self.closed = True


def _patch_fake_ws(monkeypatch, scripted_events=(), fail_before_open=False, send_setup_complete=True):
    fake_cls = type("_FakeWebSocketApp", (_FakeWebSocketApp,), {
        "scripted_events": list(scripted_events), "fail_before_open": fail_before_open,
        "send_setup_complete": send_setup_complete,
    })
    monkeypatch.setattr(gemini_live_asr_module.websocket, "WebSocketApp", fake_cls)
    return fake_cls


def test_setup_message_sent_on_open_with_model_and_languages(monkeypatch):
    _patch_fake_ws(monkeypatch, [])
    conn = _SourceConnection(api_key="k", model="gemini-3.5-transcribe-live", allowed_languages=["en", "pt-BR"])
    setup_calls = [json.loads(data) for data, _opcode in conn._ws.sent if "setup" in json.loads(data)]
    assert len(setup_calls) == 1
    setup = setup_calls[0]["setup"]
    assert setup["model"] == "models/gemini-3.5-transcribe-live"
    assert setup["generationConfig"]["responseModalities"] == ["TEXT"]
    assert setup["inputAudioTranscription"]["languageCodes"] == ["en", "pt-BR"]


def test_setup_complete_marks_the_connection_ready(monkeypatch):
    _patch_fake_ws(monkeypatch, [])
    conn = _SourceConnection(api_key="k", model="gemini-3.5-transcribe-live", allowed_languages=["en"])
    assert conn._setup_complete.is_set()
    assert conn._connect_error is None


def test_handshake_rejection_raises_instead_of_assuming_ready(monkeypatch):
    _patch_fake_ws(monkeypatch, fail_before_open=True)
    with pytest.raises(RuntimeError, match="connection rejected"):
        _SourceConnection(api_key="bad", model="gemini-3.5-transcribe-live", allowed_languages=["en"])


def test_no_setup_complete_and_no_error_fails_loud_instead_of_assuming_ready(monkeypatch):
    """Same core guarantee as realtime_asr.py/deepgram_asr.py: absence of a
    signal is not evidence of success - on_open alone must not be treated
    as "ready" since the setup message could still be rejected."""
    monkeypatch.setattr(gemini_live_asr_module, "_CONNECT_TIMEOUT_S", 0.2)

    class _NeverAcks(_FakeWebSocketApp):
        def run_forever(self):
            self._on_open(self)  # connects, but no setupComplete ever arrives

    monkeypatch.setattr(gemini_live_asr_module.websocket, "WebSocketApp", _NeverAcks)
    with pytest.raises(RuntimeError, match="timed out waiting for setupComplete"):
        _SourceConnection(api_key="k", model="gemini-3.5-transcribe-live", allowed_languages=["en"])


def test_interim_transcription_updates_the_partial_text(monkeypatch):
    _patch_fake_ws(monkeypatch, [
        {"serverContent": {"interimInputTranscription": {"text": "hello wor"}}},
    ])
    conn = _SourceConnection(api_key="k", model="gemini-3.5-transcribe-live", allowed_languages=["en"])
    assert conn.take_partial() == "hello wor"
    assert conn.take_partial() is None  # unchanged text isn't returned twice


def test_a_single_cumulative_final_is_returned_whole(monkeypatch):
    """Gemini's inputTranscription for a turn is cumulative, not a stream
    of separate pieces to join (see the module docstring's CONFIRMED LIVE
    entry) - a single final event's text is returned as-is."""
    monkeypatch.setattr(gemini_live_asr_module, "_FINALIZE_TIMEOUT_S", 0.2)
    _patch_fake_ws(monkeypatch, [
        {"serverContent": {"inputTranscription": {"text": "hello world"}}},
    ])
    conn = _SourceConnection(api_key="k", model="gemini-3.5-transcribe-live", allowed_languages=["en"])
    assert conn.commit_and_wait() == "hello world"
    # State resets after a commit - a second call with nothing new returns empty.
    assert conn.commit_and_wait() == ""


def test_regression_cumulative_finals_across_multiple_commits_do_not_duplicate(monkeypatch):
    """Regression test for a real bug found live: the orchestrator's own
    local VAD boundary fires finalize_source() far more often than
    Gemini's server-side turn boundary during one long, mostly-
    uninterrupted utterance. Each inputTranscription event covers the
    WHOLE turn so far, not just what's new - naively returning it every
    time a commit happens re-sent dozens of already-seen words as "new"
    final text on every boundary, which showed up as runaway repeated
    text in the Live Assist bubble UI. This simulates exactly that: two
    commits during one ongoing turn, with the server's text having grown
    (cumulatively, as real Gemini responses do) between them."""
    monkeypatch.setattr(gemini_live_asr_module, "_FINALIZE_TIMEOUT_S", 0.2)
    _patch_fake_ws(monkeypatch, [])
    conn = _SourceConnection(api_key="k", model="gemini-3.5-transcribe-live", allowed_languages=["en"])

    conn._on_message(conn._ws, json.dumps({"serverContent": {"inputTranscription": {"text": "Vamos tentar"}}}))
    assert conn.commit_and_wait() == "Vamos tentar"

    # The server's NEXT final for the same ongoing turn includes everything
    # already seen, plus new words - exactly the pattern observed live.
    conn._on_message(conn._ws, json.dumps(
        {"serverContent": {"inputTranscription": {"text": "Vamos tentar mais uma vez aqui"}}}))
    assert conn.commit_and_wait() == "mais uma vez aqui"

    conn._on_message(conn._ws, json.dumps(
        {"serverContent": {"inputTranscription": {"text": "Vamos tentar mais uma vez aqui para testar"}}}))
    assert conn.commit_and_wait() == "para testar"


def test_activity_start_resets_the_dedup_baseline_for_a_new_turn(monkeypatch):
    """A real ACTIVITY_START from Gemini means a genuinely new turn is
    starting - the next final's cumulative text must NOT be diffed against
    the previous (now-finished) turn's tail, or its first words would be
    wrongly swallowed as "already seen"."""
    monkeypatch.setattr(gemini_live_asr_module, "_FINALIZE_TIMEOUT_S", 0.2)
    _patch_fake_ws(monkeypatch, [])
    conn = _SourceConnection(api_key="k", model="gemini-3.5-transcribe-live", allowed_languages=["en"])

    conn._on_message(conn._ws, json.dumps({"serverContent": {"inputTranscription": {"text": "first turn done"}}}))
    assert conn.commit_and_wait() == "first turn done"

    conn._on_message(conn._ws, json.dumps({"voiceActivity": {"type": "ACTIVITY_START"}}))
    conn._on_message(conn._ws, json.dumps({"serverContent": {"inputTranscription": {"text": "second turn"}}}))
    assert conn.commit_and_wait() == "second turn"


def test_commit_and_wait_promotes_interim_text_when_nothing_finalized_in_time(monkeypatch):
    """No documented client-side 'flush now' message for this mode (see
    gemini_live_asr.py's docstring) - if Gemini's own turn detection hasn't
    closed the utterance yet, the latest interim text must still be
    returned rather than silently losing it."""
    monkeypatch.setattr(gemini_live_asr_module, "_FINALIZE_TIMEOUT_S", 0.2)
    _patch_fake_ws(monkeypatch, [
        {"serverContent": {"interimInputTranscription": {"text": "still talking"}}},
    ])
    conn = _SourceConnection(api_key="k", model="gemini-3.5-transcribe-live", allowed_languages=["en"])
    assert conn.commit_and_wait() == "still talking"


def test_commit_and_wait_returns_empty_when_nothing_was_said(monkeypatch):
    monkeypatch.setattr(gemini_live_asr_module, "_FINALIZE_TIMEOUT_S", 0.2)
    _patch_fake_ws(monkeypatch, [])
    conn = _SourceConnection(api_key="k", model="gemini-3.5-transcribe-live", allowed_languages=["en"])
    assert conn.commit_and_wait() == ""


def test_go_away_is_logged_and_does_not_affect_connection_state(monkeypatch):
    _patch_fake_ws(monkeypatch, [
        {"goAway": {"timeLeft": "30s"}},
        {"serverContent": {"inputTranscription": {"text": "still here"}}},
    ])
    conn = _SourceConnection(api_key="k", model="gemini-3.5-transcribe-live", allowed_languages=["en"])
    assert conn._setup_complete.is_set()
    # setupComplete fires before the scripted events after it finish
    # processing on the background thread - wait for the real synchronization
    # point (the same one commit_and_wait() itself relies on) instead of
    # racing a bare attribute read against that thread.
    assert conn._final_seen.wait(timeout=2)
    assert conn._latest_final == "still here"


def test_audio_is_sent_as_base64_json_with_correct_mime_type(monkeypatch):
    _patch_fake_ws(monkeypatch, [])
    conn = _SourceConnection(api_key="k", model="gemini-3.5-transcribe-live", allowed_languages=["en"])
    conn.send_audio(b"\x01\x02\x03\x04")
    audio_calls = [json.loads(data) for data, _opcode in conn._ws.sent if "realtimeInput" in json.loads(data)]
    assert len(audio_calls) == 1
    audio = audio_calls[0]["realtimeInput"]["audio"]
    assert audio["mimeType"] == "audio/pcm;rate=16000"
    import base64
    assert base64.b64decode(audio["data"]) == b"\x01\x02\x03\x04"


def test_adapter_finalize_source_returns_accumulated_text(monkeypatch):
    monkeypatch.setattr(gemini_live_asr_module, "_FINALIZE_TIMEOUT_S", 0.2)
    _patch_fake_ws(monkeypatch, [
        {"serverContent": {"inputTranscription": {"text": "ok"}}},
    ])
    adapter = TwoSourceGeminiLiveAsrAdapter(api_key="k")
    adapter.start_source("me", ["en"])
    result = adapter.finalize_source("me")
    assert result.text == "ok"
    conn = adapter._connections["me"]
    adapter.close()
    assert conn._ws.closed is True


def test_feed_source_chunk_sends_every_frame_unconditionally(monkeypatch):
    """Regression test for a real bug found live, confirmed TWICE (see the
    module docstring's "CONFIRMED LIVE, TWO SEPARATE WAYS" entry): gating/
    withholding silent frames (the same noise gate Deepgram/OpenAI use)
    silently produced ZERO transcription from Gemini - no error, nothing -
    and Google's own documented fix for this (audioStreamEnd) was tried
    properly and ALSO confirmed not to work against the real API. Sending
    every frame, silence included, is the only approach confirmed to
    actually work across a whole session - don't re-add gating here
    without re-testing the premise live first."""
    import numpy as np
    _patch_fake_ws(monkeypatch, [])
    adapter = TwoSourceGeminiLiveAsrAdapter(api_key="k")
    adapter.start_source("me", ["en"])
    conn = adapter._connections["me"]

    silence = np.zeros(480, dtype=np.int16).tobytes()
    adapter.feed_source_chunk("me", silence)
    assert any("realtimeInput" in json.loads(d) for d, _o in conn._ws.sent)

    loud = np.full(480, 2000, dtype=np.int16).tobytes()
    adapter.feed_source_chunk("me", loud)
    assert sum("realtimeInput" in json.loads(d) for d, _o in conn._ws.sent) == 2


def _wait_until(predicate, timeout=1.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_hotswap_does_not_warm_a_replacement_before_the_interval_elapses(monkeypatch):
    _patch_fake_ws(monkeypatch, [])
    adapter = TwoSourceGeminiLiveAsrAdapter(api_key="k", hotswap_interval_s=600.0)
    adapter.start_source("me", ["en"])
    adapter.feed_source_chunk("me", b"\x00\x00")
    time.sleep(0.05)
    assert "me" not in adapter._warm_connections
    assert "me" not in adapter._warming


def test_hotswap_warms_a_replacement_once_the_interval_elapses(monkeypatch):
    _patch_fake_ws(monkeypatch, [])
    adapter = TwoSourceGeminiLiveAsrAdapter(api_key="k", hotswap_interval_s=0.01)
    adapter.start_source("me", ["en"])
    time.sleep(0.02)
    adapter.feed_source_chunk("me", b"\x00\x00")  # this call is what notices the interval elapsed
    assert _wait_until(lambda: "me" in adapter._warm_connections)
    assert adapter._warm_connections["me"] is not adapter._connections["me"]


def test_hotswap_switches_at_the_next_finalize_without_losing_pending_text(monkeypatch):
    """The core guarantee: the swap only happens inside finalize_source(),
    right after commit_and_wait() has already cleanly returned whatever
    was pending on the OLD connection - so nothing said before the swap is
    lost, and everything said after goes to the NEW connection."""
    monkeypatch.setattr(gemini_live_asr_module, "_FINALIZE_TIMEOUT_S", 0.2)
    _patch_fake_ws(monkeypatch, [
        {"serverContent": {"inputTranscription": {"text": "pending before swap"}}},
    ])
    adapter = TwoSourceGeminiLiveAsrAdapter(api_key="k", hotswap_interval_s=0.01)
    adapter.start_source("me", ["en"])
    old_conn = adapter._connections["me"]
    time.sleep(0.02)
    adapter.feed_source_chunk("me", b"\x00\x00")  # triggers warming
    assert _wait_until(lambda: "me" in adapter._warm_connections)
    warm_conn = adapter._warm_connections["me"]

    result = adapter.finalize_source("me")
    assert result.text == "pending before swap"  # nothing lost from the old connection
    assert adapter._connections["me"] is warm_conn  # swapped
    assert "me" not in adapter._warm_connections  # consumed, not swapped again next time
    assert old_conn._ws.closed is True  # closed only after the handoff completed

    old_sent_before = sum("realtimeInput" in json.loads(d) for d, _o in old_conn._ws.sent)
    warm_sent_before = sum("realtimeInput" in json.loads(d) for d, _o in warm_conn._ws.sent)
    adapter.feed_source_chunk("me", b"\x01\x02")
    old_sent_after = sum("realtimeInput" in json.loads(d) for d, _o in old_conn._ws.sent)
    warm_sent_after = sum("realtimeInput" in json.loads(d) for d, _o in warm_conn._ws.sent)
    assert old_sent_after == old_sent_before  # the old (closed) connection gets nothing more
    assert warm_sent_after == warm_sent_before + 1  # the new connection gets the new audio


def test_hotswap_does_not_warm_twice_while_one_warm_up_is_already_in_flight(monkeypatch):
    _patch_fake_ws(monkeypatch, [])
    adapter = TwoSourceGeminiLiveAsrAdapter(api_key="k", hotswap_interval_s=0.01)
    adapter.start_source("me", ["en"])
    time.sleep(0.02)
    adapter.feed_source_chunk("me", b"\x00\x00")
    adapter.feed_source_chunk("me", b"\x00\x00")  # should not start a second warm-up thread
    assert _wait_until(lambda: "me" in adapter._warm_connections)
    # Only one replacement was ever created - re-checking after it's ready
    # and already consumed-free must not spin up yet another one.
    first_warm = adapter._warm_connections["me"]
    adapter.feed_source_chunk("me", b"\x00\x00")
    time.sleep(0.05)
    assert adapter._warm_connections["me"] is first_warm
