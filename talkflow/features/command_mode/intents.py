"""Minimal intent classification for Command Mode's spoken instruction. Three
intents per Wispr's documented behavior: edit existing/focused text, draft
new text (treated as "edit" with no prior selection - the LLM drafts from
scratch given the instruction as context), and web search.
"""
from __future__ import annotations

import re
from enum import Enum

_SEARCH_PATTERNS = [
    r"^\s*web search\b", r"^\s*search\b(\s+for)?", r"^\s*look up\b", r"^\s*google\b",
    r"^\s*find (me )?(information|info) (on|about)\b",
]


class Intent(str, Enum):
    EDIT = "edit"
    SEARCH = "search"


def classify(instruction: str) -> Intent:
    text = instruction.strip().lower()
    for pattern in _SEARCH_PATTERNS:
        if re.match(pattern, text):
            return Intent.SEARCH
    return Intent.EDIT


def extract_search_query(instruction: str) -> str:
    text = instruction.strip()
    for pattern in _SEARCH_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
    return text or instruction.strip()
