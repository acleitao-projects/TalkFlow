from talkflow.capture.vad import ChunkBoundaryDetector, FRAME_BYTES

SILENCE_FRAME = b"\x00\x00" * (FRAME_BYTES // 2)


def _speech_frame():
    # A loud alternating signal - not realistic speech, but reliably trips
    # webrtcvad's energy-based detector in aggressive-enough modes is not
    # guaranteed, so this test focuses on the boundary *timing* logic using
    # the safety-net max-frames path instead of relying on VAD's classification.
    import struct
    n_samples = FRAME_BYTES // 2
    return b"".join(struct.pack("<h", 20000 if i % 2 == 0 else -20000) for i in range(n_samples))


def test_no_boundary_on_pure_silence():
    det = ChunkBoundaryDetector(silence_ms_for_boundary=300, max_ms_between_boundaries=3000)
    boundaries = [det.feed_frame(SILENCE_FRAME) for _ in range(50)]
    assert not any(boundaries)  # never heard speech, so no boundary should fire


def test_safety_net_boundary_fires_eventually_after_speech():
    det = ChunkBoundaryDetector(silence_ms_for_boundary=10_000, max_ms_between_boundaries=300)
    det._heard_speech_since_boundary = True  # simulate having detected speech
    boundaries = [det.feed_frame(SILENCE_FRAME) for _ in range(20)]
    assert any(boundaries)


class _ScriptedVad:
    """Stands in for webrtcvad: a frame counts as speech iff it isn't all zeros."""
    def is_speech(self, frame, sample_rate):
        return frame != SILENCE_FRAME


def test_boundary_reports_whether_it_was_a_real_pause():
    det = ChunkBoundaryDetector(silence_ms_for_boundary=90, max_ms_between_boundaries=300)
    det._vad = _ScriptedVad()

    # Continuous speech: only the max-interval safety net can fire.
    fired = [det.feed_frame(_speech_frame()) for _ in range(10)]
    assert fired[-1] and not det.last_boundary_was_silence
    assert det.last_frame_was_speech and det.trailing_silence_ms == 0

    # Speech followed by 90ms of silence: a real pause.
    det.feed_frame(_speech_frame())
    fired = [det.feed_frame(SILENCE_FRAME) for _ in range(3)]
    assert fired == [False, False, True] and det.last_boundary_was_silence
    assert not det.last_frame_was_speech

    # trailing_silence_ms keeps counting across the boundary.
    det.feed_frame(SILENCE_FRAME)
    assert det.trailing_silence_ms == 120
