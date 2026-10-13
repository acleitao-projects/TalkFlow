"""Generic /v1/chat/completions client. Works against Ollama Cloud (default),
OpenAI, Azure OpenAI, or any other OpenAI-compatible host - base_url/model_id/
api_key come from the model registry, never hardcoded. This stage is always
cloud/network-reachable; there is no offline fallback (see CLAUDE.md).
"""
from __future__ import annotations

import time

import httpx

from talkflow.llm.base import CleanupResult, LlmAdapter
from talkflow.llm.prompts import build_messages, build_rewrite_messages


class OpenAiCompatibleAdapter(LlmAdapter):
    def __init__(self, base_url: str, model_id: str, api_key: str | None,
                 timeout: float = 30.0, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.api_key = api_key
        self.timeout = timeout
        # Long keep-alive: httpx's 5s default meant every dictation paid a new
        # TCP/TLS handshake to the cloud host.
        self._client = client or httpx.Client(
            timeout=timeout,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=300),
        )

    def warm(self) -> None:
        """Open the connection ahead of the first cleanup request."""
        try:
            self._client.head(self.base_url, timeout=5.0)
        except Exception:
            pass

    def _post_chat(self, payload: dict) -> dict:
        """POST /v1/chat/completions, retrying once on a failure that may be
        transient (network error, 404/408/429/5xx - Ollama Cloud has returned
        a one-off 404 for a model that exists). The provider's response body
        goes into the raised error so the log says why it failed."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = f"{self.base_url}/v1/chat/completions"
        last_error = "no response"
        for attempt in range(2):
            try:
                resp = self._client.post(url, headers=headers, json=payload)
            except httpx.TransportError as e:
                last_error = f"{type(e).__name__}: {e}"
                continue
            if resp.is_success:
                return resp.json()
            last_error = f"HTTP {resp.status_code} from {url}: {resp.text[:300]}"
            if resp.status_code not in (404, 408, 429) and resp.status_code < 500:
                break
        raise RuntimeError(last_error)

    def cleanup(
        self,
        raw_transcript: str,
        language: str,
        formality_name: str,
        formality_instruction: str,
        target_process_name: str | None,
        dictionary_terms: list[str] | None = None,
        snippet_candidates: list[tuple[str, str]] | None = None,
        previous_text: str | None = None,
    ) -> CleanupResult:
        messages = build_messages(
            raw_transcript, language, formality_name, formality_instruction,
            target_process_name, dictionary_terms, previous_text,
        )
        # Cleaned text is never much longer than the transcript; the cap stops
        # a model that starts rambling from holding up the paste.
        payload = {"model": self.model_id, "messages": messages, "temperature": 0.2,
                   "max_tokens": max(256, len(raw_transcript) + 128)}
        start = time.monotonic()
        body = self._post_chat(payload)
        if body["choices"][0].get("finish_reason") == "length":
            # Cap was hit (e.g. a reasoning model spent it thinking): never
            # paste a truncated dictation, redo without the cap.
            del payload["max_tokens"]
            body = self._post_chat(payload)
        latency_ms = (time.monotonic() - start) * 1000

        cleaned = body["choices"][0]["message"]["content"].strip()
        return CleanupResult(cleaned_text=cleaned, model_used=self.model_id, latency_ms=latency_ms)

    def rewrite(self, text: str, instruction: str) -> CleanupResult:
        messages = build_rewrite_messages(text, instruction)
        start = time.monotonic()
        body = self._post_chat({"model": self.model_id, "messages": messages, "temperature": 0.2})
        latency_ms = (time.monotonic() - start) * 1000

        rewritten = body["choices"][0]["message"]["content"].strip()
        return CleanupResult(cleaned_text=rewritten, model_used=self.model_id, latency_ms=latency_ms)
