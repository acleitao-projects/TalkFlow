"""Unit-testable pieces of the WhisperLiveKit ASR adapter: the language-param
selection logic and the connection/Results state machine (using a fake
WebSocketApp - no real network). The real socket's actual latency/accuracy
still needs the live whisper-live.service container (192.168.1.36) and is
only verified by live use, same as the other Live Assist ASR adapters in
this project."""
import json

import pytest
import websocket

from talkflow.features.liveassist import whisperlivekit_asr as wlk_module
from talkflow.features.liveassist.whisperlivekit_asr import (
    TwoSourceWhisperLiveKitAdapter,
    _language_param,
    _SourceConnection,
)


def test_language_param_uses_the_single_allowed_language_directly():
    assert _language_param(["pt-BR"]) == "pt-BR"
    assert _language_param(["en"]) == "en"


def test_language_param_falls_back_to_multi_for_more_than_one_language():
    assert _language_param(["en", "pt-BR"]) == "multi"


class _FakeWebSocketApp:
    """Stands in for websocket.WebSocketApp - run_forever() synchronously
    fires on_open, then delivers whatever raw JSON messages the test
    scripted, with no real network."""

    scripted_events: list[dict] = []
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
            self._on_error(self, "connection refused")
            self._on_close(self, None, "refused")
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
    monkeypatch.setattr(wlk_module.websocket, "WebSocketApp", fake_cls)
    return fake_cls


def test_url_has_no_api_key_header_and_uses_v1_listen_path(monkeypatch):
    """No auth needed - this is our own LAN server with no --api-token set."""
    fake_cls = _patch_fake_ws(monkeypatch, [])
    conn = _SourceConnection(base_url="ws://192.168.1.36:8765", allowed_languages=["en"], source_id="me")
    assert "/v1/listen" in conn._ws.url
    assert conn._ready.is_set()


def test_handshake_rejection_raises_instead_of_assuming_ready(monkeypatch):
    _patch_fake_ws(monkeypatch, fail_before_open=True)
    with pytest.raises(RuntimeError, match="connection rejected"):
        _SourceConnection(base_url="ws://192.168.1.36:8765", allowed_languages=["en"])


def test_no_open_and_no_error_fails_loud_instead_of_assuming_ready(monkeypatch):
    monkeypatch.setattr(wlk_module, "_CONNECT_TIMEOUT_S", 0.2)

    class _NeverOpens(_FakeWebSocketApp):
        def run_forever(self):
            pass

    monkeypatch.setattr(wlk_module.websocket, "WebSocketApp", _NeverOpens)
    with pytest.raises(RuntimeError, match="timed out connecting"):
        _SourceConnection(base_url="ws://192.168.1.36:8765", allowed_languages=["en"])


def test_interim_result_updates_the_partial_text(monkeypatch):
    _patch_fake_ws(monkeypatch, [
        {"type": "Results", "is_final": False, "channel": {"alternatives": [{"transcript": "hello wor"}]}},
    ])
    conn = _SourceConnection(base_url="ws://192.168.1.36:8765", allowed_languages=["en"])
    assert conn.take_partial() == "hello wor"
    assert conn.take_partial() is None


def test_final_results_accumulate_and_take_final_joins_them(monkeypatch):
    _patch_fake_ws(monkeypatch, [
        {"type": "Results", "is_final": True, "channel": {"alternatives": [{"transcript": "hello"}]}},
        {"type": "Results", "is_final": True, "speech_final": True,
         "channel": {"alternatives": [{"transcript": "world"}]}},
    ])
    conn = _SourceConnection(base_url="ws://192.168.1.36:8765", allowed_languages=["en"])
    assert conn.take_final() == "hello world"
    assert conn.take_final() == ""  # state resets after draining


def test_take_final_never_sends_a_finalize_message(monkeypatch):
    """LA-12: the core protocol difference from Deepgram - WhisperLiveKit
    closes the connection (code 4400) if it ever receives {"type":
    "Finalize"}, confirmed by reading its source. take_final() must never
    send anything at all, just drain whatever's already accumulated."""
    _patch_fake_ws(monkeypatch, [
        {"type": "Results", "is_final": True, "channel": {"alternatives": [{"transcript": "ok"}]}},
    ])
    conn = _SourceConnection(base_url="ws://192.168.1.36:8765", allowed_languages=["en"])
    assert conn.take_final() == "ok"
    assert conn._ws.sent == []  # nothing was ever sent to request this


def test_take_final_with_nothing_accumulated_returns_empty_without_blocking(monkeypatch):
    _patch_fake_ws(monkeypatch, [])
    conn = _SourceConnection(base_url="ws://192.168.1.36:8765", allowed_languages=["en"])
    assert conn.take_final() == ""
    assert conn._ws.sent == []


def test_feed_source_chunk_is_gated_by_the_shared_noise_gate(monkeypatch):
    import numpy as np
    _patch_fake_ws(monkeypatch, [])
    adapter = TwoSourceWhisperLiveKitAdapter(base_url="ws://192.168.1.36:8765")
    adapter.start_source("me", ["en"])
    conn = adapter._connections["me"]

    silence = np.zeros(480, dtype=np.int16).tobytes()
    adapter.feed_source_chunk("me", silence)
    assert all(opcode == websocket.ABNF.OPCODE_BINARY for _d, opcode in conn._ws.sent) or not conn._ws.sent

    loud = np.full(480, 2000, dtype=np.int16).tobytes()
    adapter.feed_source_chunk("me", loud)
    audio_sends = [d for d, _o in conn._ws.sent if d == loud]
    assert len(audio_sends) == 1


def test_adapter_finalize_source_never_sends_finalize(monkeypatch):
    _patch_fake_ws(monkeypatch, [
        {"type": "Results", "is_final": True, "channel": {"alternatives": [{"transcript": "ok"}]}},
    ])
    adapter = TwoSourceWhisperLiveKitAdapter(base_url="ws://192.168.1.36:8765")
    adapter.start_source("me", ["en"])
    result = adapter.finalize_source("me")
    assert result.text == "ok"

    conn = adapter._connections["me"]
    finalize_msg = json.dumps({"type": "Finalize"})
    assert not any(data == finalize_msg for data, _opcode in conn._ws.sent)
    adapter.close()
    assert conn._ws.closed is True


def test_base_url_accepts_http_and_normalizes_to_ws():
    adapter = TwoSourceWhisperLiveKitAdapter(base_url="http://192.168.1.36:8765")
    assert adapter._base_url == "ws://192.168.1.36:8765"
