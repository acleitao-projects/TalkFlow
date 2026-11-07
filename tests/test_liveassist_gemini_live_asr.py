"""Unit-testable pieces of the Gemini Live ASR adapter: the setup/
setupComplete handshake and the serverContent transcription state machine
(using a fake WebSocketApp - no real network). The real socket's actual
latency/accuracy still needs a real Gemini API key and is only verified by
live use, same as the other Live Assist ASR adapters in this project."""
import json

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


def test_final_transcriptions_accumulate_and_commit_and_wait_joins_them(monkeypatch):
    monkeypatch.setattr(gemini_live_asr_module, "_FINALIZE_TIMEOUT_S", 0.2)
    _patch_fake_ws(monkeypatch, [
        {"serverContent": {"inputTranscription": {"text": "hello"}}},
        {"serverContent": {"inputTranscription": {"text": "world"}}},
    ])
    conn = _SourceConnection(api_key="k", model="gemini-3.5-transcribe-live", allowed_languages=["en"])
    assert conn.commit_and_wait() == "hello world"
    # State resets after a commit - a second call with nothing new returns empty.
    assert conn.commit_and_wait() == ""


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
    """Regression test for a real bug found live: gating/withholding silent
    frames (the same noise gate Deepgram/OpenAI use) silently produced ZERO
    transcription from Gemini - no error, nothing. Sending every frame,
    silence included, is what actually works - see the module docstring's
    "CONFIRMED LIVE" entry for how this was found."""
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
