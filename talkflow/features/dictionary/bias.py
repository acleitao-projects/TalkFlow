"""Builds an ASR hint string from dictionary entries. speaches/faster-whisper
accepts an `initial_prompt`-style hint via the `prompt` field on the OpenAI
transcription request, which nudges recognition toward these terms without
guaranteeing exact matches (Whisper has no true hotword/biasing API).
"""
from __future__ import annotations

from talkflow.features.dictionary.store import all_terms

MAX_PROMPT_CHARS = 800


def build_initial_prompt() -> str | None:
    terms = all_terms()
    if not terms:
        return None
    prompt = "Vocabulary that may appear: " + ", ".join(terms)
    return prompt[:MAX_PROMPT_CHARS]
