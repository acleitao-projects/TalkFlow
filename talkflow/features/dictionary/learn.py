"""Auto-learn dictionary entries from a user-edited History transcript.

Approximates Wispr's documented inclusion/exclusion rules (see
EVIDENCE_LEDGER.md) without true NER: learns word-ish tokens that are NEW in
the edited text vs. the original and look like a name/term (capitalized
mid-sentence, or ALLCAPS acronym, or contains a non-ASCII/foreign character),
while skipping file paths, URLs, emails, code-symbol-heavy tokens, and very
common short words. This is a heuristic, not a claim of parity with Wispr's
actual (undisclosed) learning model - see CLAUDE.md for that caveat.
"""
from __future__ import annotations

import re

from talkflow.features.dictionary.store import add_entry, all_terms

_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9][A-Za-zÀ-ÖØ-öø-ÿ0-9'\-]*")

_COMMON_WORDS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be",
    "to", "of", "in", "on", "at", "for", "with", "this", "that", "it", "i",
    "you", "he", "she", "we", "they", "my", "your", "his", "her", "our",
    "their", "not", "do", "does", "did", "have", "has", "had", "will",
    "would", "can", "could", "should", "so", "if", "as", "by", "from",
}

_SKIP_PATTERNS = [
    re.compile(r"[\\/]"),           # file paths
    re.compile(r"^https?://"),       # URLs
    re.compile(r"^\w+\.\w+$"),       # domains / partial emails / code symbols like foo.bar
    re.compile(r"@"),                 # emails (unless the whole token is a full address, handled below)
]

_FULL_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")


def _is_learnable(token: str) -> bool:
    if len(token) < 3:
        return False
    low = token.lower()
    if low in _COMMON_WORDS:
        return False
    if _FULL_EMAIL_RE.match(token):
        return True  # full email addresses ARE learned per the documented rules
    for pattern in _SKIP_PATTERNS:
        if pattern.search(token):
            return False
    if token.isupper() and len(token) >= 2:
        return True  # acronym, e.g. API
    if token[0].isupper() and not token.isupper():
        return True  # capitalized name/term, e.g. Geoff, FigJam
    if any(ord(c) > 127 for c in token):
        return True  # foreign-language / non-ASCII term
    return False


def suggest_learnable_terms(original_text: str, edited_text: str) -> list[str]:
    original_tokens = {m.group(0) for m in _TOKEN_RE.finditer(original_text or "")}
    edited_tokens = [m.group(0) for m in _TOKEN_RE.finditer(edited_text or "")]
    known = {t.lower() for t in all_terms()}

    new_tokens = []
    seen = set()
    for tok in edited_tokens:
        if tok in original_tokens:
            continue
        low = tok.lower()
        if low in seen or low in known:
            continue
        if _is_learnable(tok):
            new_tokens.append(tok)
            seen.add(low)
    return new_tokens


def learn_from_correction(original_text: str, edited_text: str) -> list[str]:
    """Suggests AND saves the learnable terms (source='learned'); returns what was added."""
    terms = suggest_learnable_terms(original_text, edited_text)
    for term in terms:
        add_entry(term, source="learned")
    return terms
