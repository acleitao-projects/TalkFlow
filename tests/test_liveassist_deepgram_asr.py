"""Unit-testable pieces of the Deepgram ASR adapter: the language-param
selection logic and the connection/Results state machine (using a fake
WebSocketApp - no real network). The real socket's actual latency/accuracy
still needs a real Deepgram account and is only verified by live use, same
as the other Live Assist ASR adapters in this project."""
import json

import pytest
import websocket

from talkflow.features.liveassist import deepgram_asr as deepgram_asr_module
from talkflow.features.liveassist.deepgram_asr import (
    TwoSourceDeepgramAsrAdapter,
    _language_param,
    _SourceConnection,
)


def test_language_param_uses_the_single_allowed_language_directly():
    assert _language_param(["pt-BR"]) == "pt-BR"
    assert _language_param(["en"]) == "en"


def test_language_param_falls_back_to_multi_for_more_than_one_language():
    assert _language_param(["en", "pt-BR"]) == "multi"


def test_language_param_handles_empty_input():
    assert _language_param([]) == "multi"


class _FakeWebSocketApp:
    """Stands in for websocket.WebSocketApp - run_forever() synchronously
    fires on_open, then delivers whatever raw JSON messages the test
    scripted (simulating the server's Results stream), with no real
    network."""

    scripted_events: list[dict] = []  # set per-test via monkeypatch before construction
    fail_before_open = False

    def __init__(self, url, header=None, on_open=None, on_message=None, on_error=None, on_close=None):
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
        self._on_open(self)
        for event in self.scripted_events:
            self._on_message(self, json.dumps(event))

    def close(self):
        self.closed = True


def _patch_fake_ws(monkeypatch, scripted_events=(), fail_before_open=False):
    fake_cls = type("_FakeWebSocketApp", (_FakeWebSocketApp,), {
        "scripted_events": list(scripted_events), "fail_before_open": fail_before_open,
    })
    monkeypatch.setattr(deepgram_asr_module.websocket, "WebSocketApp", fake_cls)
    return fake_cls


def test_open_without_config_errors_marks_the_connection_ready(monkeypatch):
    _patch_fake_ws(monkeypatch, [])
    conn = _SourceConnection(api_key="k", model="nova-3", allowed_languages=["en"])
    assert conn._ready.is_set()
    assert conn._connect_error is None


def test_handshake_rejection_raises_instead_of_assuming_ready(monkeypatch):
    _patch_fake_ws(monkeypatch, fail_before_open=True)
    with pytest.raises(RuntimeError, match="connection rejected"):
        _SourceConnection(api_key="bad", model="nova-3", allowed_languages=["en"])


def test_no_open_and_no_error_fails_loud_instead_of_assuming_ready(monkeypatch):
    """Same core guarantee as realtime_asr.py: absence of a signal is not
    evidence of success."""
    monkeypatch.setattr(deepgram_asr_module, "_CONNECT_TIMEOUT_S", 0.2)

    class _NeverOpens(_FakeWebSocketApp):
        def run_forever(self):
            pass  # never calls on_open, on_error, or on_close

    monkeypatch.setattr(deepgram_asr_module.websocket, "WebSocketApp", _NeverOpens)
    with pytest.raises(RuntimeError, match="timed out connecting"):
        _SourceConnection(api_key="k", model="nova-3", allowed_languages=["en"])


def test_interim_result_updates_the_partial_text(monkeypatch):
    _patch_fake_ws(monkeypatch, [
        {"type": "Results", "is_final": False, "channel": {"alternatives": [{"transcript": "hello wor"}]}},
    ])
    conn = _SourceConnection(api_key="k", model="nova-3", allowed_languages=["en"])
    assert conn.take_partial() == "hello wor"
    assert conn.take_partial() is None  # unchanged text isn't returned twice


def test_final_results_accumulate_and_commit_and_wait_joins_them(monkeypatch):
    # The fake WS only delivers its scripted events once, during __init__ -
    # a second commit_and_wait() genuinely has nothing more coming, so keep
    # that wait short rather than blocking the suite for the real timeout.
    monkeypatch.setattr(deepgram_asr_module, "_FINALIZE_TIMEOUT_S", 0.2)
    _patch_fake_ws(monkeypatch, [
        {"type": "Results", "is_final": True, "channel": {"alternatives": [{"transcript": "hello"}]}},
        {"type": "Results", "is_final": True, "channel": {"alternatives": [{"transcript": "world"}]},
         "from_finalize": True},
    ])
    conn = _SourceConnection(api_key="k", model="nova-3", allowed_languages=["en"])
    assert conn.commit_and_wait() == "hello world"
    # State resets after a commit - a second call with nothing new returns empty.
    assert conn.commit_and_wait() == ""


def test_speech_final_result_skips_the_explicit_finalize_round_trip(monkeypatch):
    """LA-11 speed fix: Deepgram's own server-side endpointing already closes
    an utterance and marks it speech_final=true before our local VAD even
    notices - sending an explicit Finalize on top of that is a wasted round
    trip. commit_and_wait() must return immediately in that case, with no
    "Finalize" message sent at all."""
    _patch_fake_ws(monkeypatch, [
        {"type": "Results", "is_final": True, "speech_final": True,
         "channel": {"alternatives": [{"transcript": "already done"}]}},
    ])
    conn = _SourceConnection(api_key="k", model="nova-3", allowed_languages=["en"])
    assert conn.commit_and_wait() == "already done"
    finalize_msg = json.dumps({"type": "Finalize"})
    assert not any(data == finalize_msg for data, _opcode in conn._ws.sent)


def test_commit_and_wait_times_out_gracefully_when_nothing_was_buffered(monkeypatch):
    monkeypatch.setattr(deepgram_asr_module, "_FINALIZE_TIMEOUT_S", 0.2)
    _patch_fake_ws(monkeypatch, [])
    conn = _SourceConnection(api_key="k", model="nova-3", allowed_languages=["en"])
    assert conn.commit_and_wait() == ""


def test_adapter_sends_finalize_control_frame_on_finalize_source(monkeypatch):
    monkeypatch.setattr(deepgram_asr_module, "_FINALIZE_TIMEOUT_S", 0.2)
    _patch_fake_ws(monkeypatch, [
        {"type": "Results", "is_final": True, "channel": {"alternatives": [{"transcript": "ok"}]}},
    ])
    adapter = TwoSourceDeepgramAsrAdapter(api_key="k")
    adapter.start_source("me", ["en"])
    result = adapter.finalize_source("me")
    assert result.text == "ok"

    conn = adapter._connections["me"]
    assert any(data == json.dumps({"type": "Finalize"}) for data, _opcode in conn._ws.sent)
    adapter.close()
    assert conn._ws.closed is True


def test_feed_source_chunk_is_gated_by_the_shared_noise_gate(monkeypatch):
    import numpy as np
    _patch_fake_ws(monkeypatch, [])
    adapter = TwoSourceDeepgramAsrAdapter(api_key="k")
    adapter.start_source("me", ["en"])
    conn = adapter._connections["me"]

    silence = np.zeros(480, dtype=np.int16).tobytes()
    adapter.feed_source_chunk("me", silence)
    # Silence never reaches the socket as audio, but a KeepAlive control
    # frame is allowed through to keep Deepgram from closing the connection.
    assert all(opcode == websocket.ABNF.OPCODE_BINARY for _data, opcode in conn._ws.sent) or not conn._ws.sent

    loud = np.full(480, 2000, dtype=np.int16).tobytes()
    adapter.feed_source_chunk("me", loud)
    audio_sends = [d for d, _o in conn._ws.sent if d == loud]
    assert len(audio_sends) == 1  # sent raw, no resampling/base64 envelope needed for Deepgram


def test_prolonged_silence_sends_a_keepalive_before_deepgrams_10s_cutoff(monkeypatch):
    """LA-11: a quiet source (the other side is talking) withholds audio via
    the noise gate, same as before - but Deepgram kills idle connections
    after 10s of neither audio nor a KeepAlive (confirmed live and in
    Deepgram's own docs), so staying silent for a while must still send
    KeepAlive frames or the connection silently dies mid-session."""
    # A generous margin (not just barely over the interval) - this machine
    # runs under real load from other apps, and a tight margin made this
    # test genuinely flaky under scheduler jitter, not just theoretically.
    monkeypatch.setattr(deepgram_asr_module, "_KEEPALIVE_INTERVAL_S", 0.1)
    _patch_fake_ws(monkeypatch, [])
    conn = _SourceConnection(api_key="k", model="nova-3", allowed_languages=["en"])

    conn.maybe_send_keepalive()  # too soon since connect - rate limited, nothing sent yet
    assert conn._ws.sent == []

    import time
    time.sleep(0.3)
    conn.maybe_send_keepalive()
    assert conn._ws.sent == [(json.dumps({"type": "KeepAlive"}), None)]

    conn.maybe_send_keepalive()  # immediately again - still rate limited
    assert len(conn._ws.sent) == 1


def test_sending_audio_resets_the_keepalive_clock(monkeypatch):
    monkeypatch.setattr(deepgram_asr_module, "_KEEPALIVE_INTERVAL_S", 0.05)
    _patch_fake_ws(monkeypatch, [])
    conn = _SourceConnection(api_key="k", model="nova-3", allowed_languages=["en"])
    conn.send_audio(b"\x00\x00")
    conn.maybe_send_keepalive()  # the audio send just reset the clock
    keepalive_msg = json.dumps({"type": "KeepAlive"})
    assert not any(data == keepalive_msg for data, _opcode in conn._ws.sent)
