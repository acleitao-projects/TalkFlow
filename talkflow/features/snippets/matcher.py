"""Longest-match-wins snippet expansion, run on the raw transcript BEFORE the
LLM cleanup stage sees it (so cleanup operates on already-expanded text,
matching Wispr's "fires inline, as part of the same dictation pass" behavior).
"""
from __future__ import annotations

from talkflow.features.snippets.store import Snippet


def expand(text: str, snippets: list[Snippet]) -> str:
    if not snippets or not text:
        return text
    # Longest trigger first, so an overlapping shorter trigger never wins.
    ordered = sorted(snippets, key=lambda s: len(s.trigger), reverse=True)

    out = []
    i = 0
    lower_text = text.lower()
    n = len(text)
    while i < n:
        matched = None
        for s in ordered:
            t = s.trigger.lower()
            if t and lower_text.startswith(t, i):
                matched = s
                break
        if matched:
            out.append(matched.expansion)
            i += len(matched.trigger)
        else:
            out.append(text[i])
            i += 1
    return "".join(out)
