"""Unit-testable pieces of the Realtime ASR adapter: the noise gate (the
cost-saving trick confirmed in Persua's own code), the 16kHz->24kHz
resampler, and the ack-based connection-readiness state machine (using a
fake WebSocketApp - no real network). The real socket's actual behavior
still needs a real OpenAI account and is only verified by live use, same as
the other adapters' live/network paths in this project."""
import json

import numpy as np
import pytest

from talkflow.features.liveassist import realtime_asr as realtime_asr_module
from talkflow.features.liveassist.realtime_asr import (
    _NoiseGate,
    _SourceConnection,
    _resample_to_api_rate,
    _rms,
)


def _tone(amplitude: int, n_samples: int = 480) -> bytes:
    return (np.full(n_samples, amplitude, dtype=np.int16)).tobytes()


def test_noise_gate_blocks_silence_by_default():
    gate = _NoiseGate(rms_threshold=500.0, hangover_frames=10)
    silence = _tone(0)
    assert gate.should_send(silence) is False


def test_noise_gate_opens_on_loud_audio():
    gate = _NoiseGate(rms_threshold=500.0, hangover_frames=10)
    loud = _tone(2000)
    assert gate.should_send(loud) is True


def test_noise_gate_hangover_keeps_sending_briefly_after_speech_stops():
    gate = _NoiseGate(rms_threshold=500.0, hangover_frames=3)
    loud, silence = _tone(2000), _tone(0)
    assert gate.should_send(loud) is True
    # Within the hangover window, trailing silence still gets sent (avoids
    # clipping soft trailing syllables).
    assert gate.should_send(silence) is True
    assert gate.should_send(silence) is True
    assert gate.should_send(silence) is True
    # Past the hangover window, it closes.
    assert gate.should_send(silence) is False


def test_noise_gate_speech_resets_the_hangover_counter():
    gate = _NoiseGate(rms_threshold=500.0, hangover_frames=2)
    loud, silence = _tone(2000), _tone(0)
    gate.should_send(loud)
    gate.should_send(silence)
    gate.should_send(loud)  # speech again resets the clock
    assert gate.should_send(silence) is True
    assert gate.should_send(silence) is True
    assert gate.should_send(silence) is False


def test_resample_doubles_length_ratio_16k_to_24k():
    pcm = _tone(1000, n_samples=1600)  # 100ms @ 16kHz
    resampled = _resample_to_api_rate(pcm)
    out_samples = len(resampled) // 2  # 16-bit samples
    assert out_samples == 2400  # 100ms @ 24kHz


def test_resample_empty_input_is_empty_output():
    assert _resample_to_api_rate(b"") == b""


def test_rms_of_silence_is_zero_and_of_tone_is_amplitude():
    assert _rms(_tone(0)) == 0.0
    assert abs(_rms(_tone(1000)) - 1000.0) < 1e-6


class _FakeWebSocketApp:
    """Stands in for websocket.WebSocketApp - run_forever() synchronously
    fires on_open, then delivers whatever raw JSON messages the test
    scripted (simulating the server's response sequence), with no real
    network. Confirmed live (scripts/_tmp_probe_ack.py against the real
    API) that a successful session.update gets "session.updated" back; a
    rejected one gets "error" instead - this fake lets both paths, plus a
    "nothing comes back" timeout path, be exercised deterministically."""

    scripted_events: list[dict] = []  # set per-test via monkeypatch before construction

    def __init__(self, url, header=None, on_open=None, on_message=None, on_error=None, on_close=None):
        self._on_open = on_open
        self._on_message = on_message
        self._on_error = on_error
        self._on_close = on_close
        self.sent: list[str] = []
        self.closed = False

    def send(self, data):
        self.sent.append(data)

    def run_forever(self):
        self._on_open(self)
        for event in self.scripted_events:
            self._on_message(self, json.dumps(event))

    def close(self):
        self.closed = True


def _patch_fake_ws(monkeypatch, scripted_events):
    fake_cls = type("_FakeWebSocketApp", (_FakeWebSocketApp,), {"scripted_events": scripted_events})
    monkeypatch.setattr(realtime_asr_module.websocket, "WebSocketApp", fake_cls)


def test_session_updated_ack_marks_the_connection_ready(monkeypatch):
    _patch_fake_ws(monkeypatch, [{"type": "session.created"}, {"type": "session.updated"}])
    conn = _SourceConnection(api_key="k", model="gpt-live-transcribe", allowed_languages=["en"])
    assert conn._session_update_acked.is_set()
    assert conn._session_config_error is None


def test_session_error_event_raises_with_that_error_surfaced(monkeypatch):
    _patch_fake_ws(monkeypatch, [{
        "type": "error",
        "error": {"param": "session.audio.input.transcription.languages[0]", "message": "Invalid value: 'xx'"},
    }])
    with pytest.raises(RuntimeError, match="Invalid value"):
        _SourceConnection(api_key="k", model="gpt-live-transcribe", allowed_languages=["xx"])


def test_no_ack_and_no_error_fails_loud_instead_of_assuming_ready(monkeypatch):
    """The core fix: absence of an error is not evidence of success - only
    an explicit ack (or explicit error) may mark the connection ready."""
    monkeypatch.setattr(realtime_asr_module, "_CONNECT_TIMEOUT_S", 0.2)
    _patch_fake_ws(monkeypatch, [{"type": "session.created"}])  # no "session.updated", no "error"
    with pytest.raises(RuntimeError, match="timed out waiting for session.updated"):
        _SourceConnection(api_key="k", model="gpt-live-transcribe", allowed_languages=["en"])
