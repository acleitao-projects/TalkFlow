

def test_trim_silence_keeps_speech_and_margin():
    import numpy as np
    from talkflow.pipeline.orchestrator import _trim_silence

    sr = 16000
    silence = np.zeros(sr, dtype=np.int16)                       # 1s
    speech = (np.random.RandomState(0).randn(sr) * 3000).astype(np.int16)  # 1s
    pcm = np.concatenate([silence, speech, silence]).tobytes()
    out = np.frombuffer(_trim_silence(pcm, sr), dtype=np.int16)
    assert len(speech) < len(out) < len(speech) + int(sr * 0.6)  # speech + ~0.25s each side
    assert np.abs(out[int(sr * 0.25) + 100: -int(sr * 0.25) - 100]).max() > 1000
