from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class CleanupResult:
    cleaned_text: str
    model_used: str
    latency_ms: float


@dataclass
class SuggestionResult:
    suggestion_text: str  # "" when has_suggestion is False
    has_suggestion: bool
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
        cleanup_level: str = "light",
    ) -> CleanupResult:
        """Remove disfluencies and apply the formality instruction, preserving
        the input language (never translates). `previous_text` is the already
        inserted start of the same dictation (context only, never re-emitted).
        `cleanup_level`: "none" skips the LLM call and returns the transcript
        verbatim; "light" (default) removes disfluencies only; "medium" also
        tightens wording for clarity/concision."""

    @abstractmethod
    def rewrite(self, text: str, instruction: str) -> CleanupResult:
        """Generic instruction-following rewrite of existing text (used by
        Transforms and Command Mode's edit intent) - no dictation-cleanup
        framing, no language-preservation assumption, just: apply this
        instruction to this text."""

    @abstractmethod
    def suggest(
        self,
        transcript_lines: list[tuple[str, str]],
        assistant_name: str,
        assistant_task: str,
        assistant_style: str,
        language: str,
        custom_system_prompt: str | None = None,
    ) -> "SuggestionResult":
        """LA-09: draft what ME could say/send next, grounded in the given
        (source, text) transcript lines so far, in the assistant's persona.
        Returns has_suggestion=False (not a fabricated reply) when the model
        decides there's genuinely nothing useful yet - see prompts.py's
        NO_SUGGESTION_MARKER. custom_system_prompt, when set, fully replaces
        assistant_task/assistant_style with a hand-written persona prompt -
        see prompts.py's build_suggestion_messages."""
