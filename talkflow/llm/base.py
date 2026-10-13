from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class CleanupResult:
    cleaned_text: str
    model_used: str
    latency_ms: float


class LlmAdapter(ABC):
    @abstractmethod
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
        """Remove disfluencies and apply the formality instruction, preserving
        the input language (never translates). `previous_text` is the already
        inserted start of the same dictation (context only, never re-emitted)."""

    @abstractmethod
    def rewrite(self, text: str, instruction: str) -> CleanupResult:
        """Generic instruction-following rewrite of existing text (used by
        Transforms and Command Mode's edit intent) - no dictation-cleanup
        framing, no language-preservation assumption, just: apply this
        instruction to this text."""
