import httpx

from talkflow.asr.whisper_http_adapter import WhisperHttpAdapter


def _fake_server(language="en"):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "hello world", "language": language})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_feed_chunk_requires_minimum_audio():
    adapter = WhisperHttpAdapter(base_url="http://localhost:8000", model_id="m", client=_fake_server())
    adapter.start_session(["en"])
    # Less than 1 second of 16kHz 16-bit mono audio -> no transcription yet.
    result = adapter.feed_chunk(b"\x00\x00" * 100, sample_rate=16000)
    assert result is None


def test_feed_chunk_only_buffers_and_finalize_transcribes_everything():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"text": "hello world", "language": "en"})

    adapter = WhisperHttpAdapter(
        base_url="http://localhost:8000", model_id="m",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    adapter.start_session(["en"])
    assert adapter.feed_chunk(b"\x00\x01" * 16000, sample_rate=16000) is None
    assert calls == []  # no rolling request while recording
    final = adapter.finalize()
    assert final.text == "hello world" and final.detected_language == "en"
    assert len(calls) == 1


def test_finalize_empty_buffer():
    adapter = WhisperHttpAdapter(base_url="http://localhost:8000", model_id="m", client=_fake_server())
    adapter.start_session(["en"])
    final = adapter.finalize()
    assert final.text == ""


def test_finalize_forces_language_when_single_allowlist_mismatch():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8", errors="ignore")
        calls.append(body)
        # First call (auto-detect) returns a language NOT in the allowlist;
        # second call (forced) returns the allowlisted language.
        if len(calls) == 1:
            return httpx.Response(200, json={"text": "bonjour", "language": "fr"})
        return httpx.Response(200, json={"text": "hello", "language": "en"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = WhisperHttpAdapter(base_url="http://localhost:8000", model_id="m", client=client)
    adapter.start_session(["en"])
    adapter.feed_chunk(b"\x00\x01" * 16000, sample_rate=16000)
    final = adapter.finalize()
    assert final.detected_language == "en"
    assert final.text == "hello"
    assert len(calls) >= 2


def test_transcribe_pcm_is_stateless_and_can_force_language():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read().decode("utf-8", errors="ignore"))
        return httpx.Response(200, json={"text": "ola mundo", "language": "pt"})

    adapter = WhisperHttpAdapter(base_url="http://localhost:8000", model_id="m",
                                 client=httpx.Client(transport=httpx.MockTransport(handler)))
    adapter.start_session(["en", "pt-BR"])
    adapter.feed_chunk(b"\x00\x01" * 100, sample_rate=16000)
    result = adapter.transcribe_pcm(b"\x00\x01" * 16000, 16000, language="pt")
    assert result.text == "ola mundo" and result.detected_language == "pt"
    assert 'name="language"' in bodies[0]
    assert len(adapter._buffer) == 200  # session buffer untouched


def test_verbose_json_rejection_is_remembered():
    formats = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8", errors="ignore")
        verbose = "verbose_json" in body
        formats.append("verbose_json" if verbose else "json")
        if verbose:
            return httpx.Response(400, json={"error": "unsupported response_format"})
        return httpx.Response(200, json={"text": "hello"})

    adapter = WhisperHttpAdapter(base_url="https://host", model_id="m",
                                 client=httpx.Client(transport=httpx.MockTransport(handler)))
    adapter.start_session(["en", "pt-BR"])
    adapter.transcribe_pcm(b"\x00\x01" * 16000, 16000)
    adapter.transcribe_pcm(b"\x00\x01" * 16000, 16000)
    assert formats == ["verbose_json", "json", "json"]  # second clip uploaded once


def test_detection_outside_multi_language_allowlist_is_redone_in_preferred_language():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8", errors="ignore")
        bodies.append(body)
        if 'name="language"' not in body:
            return httpx.Response(200, json={"text": "Evet, çok kötü.", "language": "tr"})
        return httpx.Response(200, json={"text": "Eu fico tomando 404.", "language": "pt"})

    adapter = WhisperHttpAdapter(base_url="https://host", model_id="m",
                                 client=httpx.Client(transport=httpx.MockTransport(handler)))
    adapter.start_session(["en", "pt-BR"])
    adapter.preferred_language = "pt"
    result = adapter.transcribe_pcm(b"\x00\x01" * 16000, 16000)
    assert result.text == "Eu fico tomando 404." and result.detected_language == "pt"
    assert len(bodies) == 2 and "\r\n\r\npt\r\n" in bodies[1]

    # No known preference: fall back to the first allowlisted language.
    bodies.clear()
    adapter.preferred_language = None
    assert adapter.transcribe_pcm(b"\x00\x01" * 16000, 16000).detected_language == "en"


def test_allowlisted_regional_code_matches_base_detection():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"text": "ola", "language": "portuguese"})

    adapter = WhisperHttpAdapter(base_url="https://host", model_id="m",
                                 client=httpx.Client(transport=httpx.MockTransport(handler)))
    adapter.start_session(["pt-BR"])
    result = adapter.transcribe_pcm(b"\x00\x01" * 16000, 16000)
    assert result.detected_language == "pt" and len(calls) == 1  # no pointless second pass
