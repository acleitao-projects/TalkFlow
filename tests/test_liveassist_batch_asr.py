"""TwoSourceBatchAsrAdapter: Live Assist's optional cloud-API ASR path (e.g.
OpenAI's own /v1/audio/transcriptions), for when local Nemotron is too slow
for two concurrent streams on the user's hardware. No real model/network -
mirrors tests/test_asr_adapter.py's MockTransport pattern."""
import httpx

from talkflow.features.liveassist.batch_asr import TwoSourceBatchAsrAdapter


def _fake_client(text_by_call):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        text = text_by_call[len(calls) - 1] if len(calls) <= len(text_by_call) else text_by_call[-1]
        return httpx.Response(200, json={"text": text, "language": "en"})

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_feed_source_chunk_only_buffers_no_partial():
    client, calls = _fake_client(["hello"])
    adapter = TwoSourceBatchAsrAdapter(base_url="https://api.openai.com", model_id="whisper-1", client=client)
    adapter.start_source("me", ["en"])

    assert adapter.feed_source_chunk("me", b"\x00\x01" * 16000) is None
    assert calls == []  # no request fired until finalize


def test_finalize_source_transcribes_the_buffered_audio_and_resets():
    client, calls = _fake_client(["hello there"])
    adapter = TwoSourceBatchAsrAdapter(base_url="https://api.openai.com", model_id="whisper-1", client=client)
    adapter.start_source("me", ["en"])
    adapter.feed_source_chunk("me", b"\x00\x01" * 16000)

    final = adapter.finalize_source("me")

    assert final.text == "hello there"
    assert len(calls) == 1
    # Buffer was consumed - finalizing again with no re-arm returns empty,
    # not a stale re-send of the same audio.
    assert adapter.finalize_source("me").text == ""


def test_two_sources_are_independent_and_never_cross_contaminate():
    client, calls = _fake_client(["me text", "them text"])
    adapter = TwoSourceBatchAsrAdapter(base_url="https://api.openai.com", model_id="whisper-1", client=client)
    adapter.start_source("me", ["en"])
    adapter.start_source("they", ["en"])

    adapter.feed_source_chunk("me", b"\x00\x01" * 16000)
    adapter.feed_source_chunk("they", b"\x01\x02" * 16000)

    me_final = adapter.finalize_source("me")
    they_final = adapter.finalize_source("they")

    assert me_final.text == "me text"
    assert they_final.text == "them text"
    assert len(calls) == 2


def test_finalize_without_any_audio_returns_empty_without_a_request():
    client, calls = _fake_client(["should not be used"])
    adapter = TwoSourceBatchAsrAdapter(base_url="https://api.openai.com", model_id="whisper-1", client=client)
    adapter.start_source("me", ["en"])

    final = adapter.finalize_source("me")

    assert final.text == ""
    assert calls == []
