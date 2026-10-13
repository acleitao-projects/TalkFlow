import httpx

from talkflow.llm.openai_compatible_adapter import OpenAiCompatibleAdapter


def test_cleanup_request_shape_and_response_parsing():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.read()
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "Cleaned text here."}}]
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenAiCompatibleAdapter(
        base_url="https://ollama.com", model_id="qwen3.5:cloud", api_key="secret123", client=client,
    )
    result = adapter.cleanup(
        raw_transcript="um so i think uh we should ship it",
        language="en",
        formality_name="Friendly",
        formality_instruction="Warm, conversational.",
        target_process_name="slack.exe",
    )

    assert result.cleaned_text == "Cleaned text here."
    assert captured["url"] == "https://ollama.com/v1/chat/completions"
    assert captured["auth"] == "Bearer secret123"
    assert b"Friendly" in captured["body"]
    assert b"slack.exe" in captured["body"]


def test_rewrite_request_shape_and_response_parsing():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(200, json={"choices": [{"message": {"content": "Polished output."}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenAiCompatibleAdapter(base_url="https://ollama.com", model_id="qwen3.5:cloud", api_key="k", client=client)
    result = adapter.rewrite("some rough text", "Polish this text.")
    assert result.cleaned_text == "Polished output."
    assert b"Polish this text." in captured["body"]
    assert b"some rough text" in captured["body"]


def test_no_api_key_omits_auth_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenAiCompatibleAdapter(base_url="http://localhost:11434", model_id="llama3", api_key=None, client=client)
    result = adapter.cleanup("hi", "en", "Normal", "neutral", None)
    assert result.cleaned_text == "ok"


def test_cleanup_sends_token_cap_and_previous_text_context():
    import json
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"choices": [{"message": {"content": "and then we left."}}]})

    adapter = OpenAiCompatibleAdapter(base_url="https://ollama.com", model_id="m", api_key="k",
                                      client=httpx.Client(transport=httpx.MockTransport(handler)))
    adapter.cleanup("um and then we left", "en", "Normal", "neutral", None,
                    previous_text="We had lunch")
    assert captured["body"]["max_tokens"] >= 256
    system = captured["body"]["messages"][0]["content"]
    assert "We had lunch" in system and "Do NOT repeat" in system


def test_cleanup_retries_without_cap_when_output_was_truncated():
    import json
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.read()))
        if len(bodies) == 1:
            return httpx.Response(200, json={"choices": [
                {"message": {"content": "Cut off mid"}, "finish_reason": "length"}]})
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "The whole sentence."}, "finish_reason": "stop"}]})

    adapter = OpenAiCompatibleAdapter(base_url="https://ollama.com", model_id="m", api_key="k",
                                      client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = adapter.cleanup("the whole sentence", "en", "Normal", "neutral", None)
    assert result.cleaned_text == "The whole sentence."
    assert "max_tokens" in bodies[0] and "max_tokens" not in bodies[1]


def test_transient_404_is_retried_once_and_persistent_error_reports_the_body():
    import pytest
    calls = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(404, text="404 page not found")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    adapter = OpenAiCompatibleAdapter(base_url="https://ollama.com", model_id="m", api_key="k",
                                      client=httpx.Client(transport=httpx.MockTransport(flaky)))
    assert adapter.cleanup("hi", "en", "Normal", "neutral", None).cleaned_text == "ok"
    assert len(calls) == 2

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model 'm' not found"})

    adapter = OpenAiCompatibleAdapter(base_url="https://ollama.com", model_id="m", api_key="k",
                                      client=httpx.Client(transport=httpx.MockTransport(broken)))
    with pytest.raises(RuntimeError, match="model 'm' not found"):
        adapter.rewrite("text", "instruction")
