from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from talkflow.history.db import connect

# Per the official Dictionary behavior (Wispr's public help-center docs), these categories
# of corrections are explicitly NOT auto-learned: filenames/paths, URLs/domains,
# partial emails, code symbols, everyday words/names/acronyms. Callers of
# auto-learn should filter before calling add_entry with source="learned".


@dataclass
class DictionaryEntry:
    id: str
    term: str
    created_at: str
    source: str


def list_entries() -> list[DictionaryEntry]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM dictionary_entries ORDER BY created_at DESC").fetchall()
        return [DictionaryEntry(r["id"], r["term"], r["created_at"], r["source"]) for r in rows]
    finally:
        conn.close()


def add_entry(term: str, source: str = "manual") -> DictionaryEntry:
    term = term.strip()[:60]
    entry_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO dictionary_entries (id, term, created_at, source) VALUES (?, ?, ?, ?)",
            (entry_id, term, created_at, source),
        )
        conn.commit()
    finally:
        conn.close()
    return DictionaryEntry(entry_id, term, created_at, source)


def delete_entry(entry_id: str) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM dictionary_entries WHERE id=?", (entry_id,))
        conn.commit()
    finally:
        conn.close()


def all_terms() -> list[str]:
    return [e.term for e in list_entries()]
