from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class AsrCapabilities:
    streaming: bool
    supported_languages: list[str] | None  # None = provider-defined, not restricted here


@dataclass
class PartialResult:
    text: str
    detected_language: str | None = None


@dataclass
class FinalResult:
    text: str
    detected_language: str | None = None
    detected_language_confidence: float | None = None


class AsrAdapter(ABC):
    capabilities: AsrCapabilities

    @abstractmethod
    def start_session(self, allowed_languages: list[str], initial_prompt: str | None = None) -> None:
        """Begin a new recording session, constrained to this language allowlist.
        initial_prompt optionally biases recognition toward dictionary terms."""

    @abstractmethod
    def feed_chunk(self, pcm: bytes, sample_rate: int) -> PartialResult | None:
        """Feed a VAD-delimited chunk of 16-bit mono PCM audio; may return a
        rolling re-transcription of everything heard so far, or None if the
        adapter has nothing new to report yet."""

    @abstractmethod
    def finalize(self) -> FinalResult:
        """Called once recording stops; reconciles into the final transcript."""
