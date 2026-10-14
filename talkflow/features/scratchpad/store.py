"""Scratchpad: freeform notes the user can dictate into or jot down and come
back to later (modelled on Wispr Flow's Scratchpad) - separate from dictation
History, which logs what was inserted elsewhere. Multiple notes, full text
search over title+content.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from talkflow.history.db import connect


@dataclass
class ScratchpadNote:
    id: str
    title: str
    content: str
    created_at: str
    updated_at: str


def list_notes(limit: int = 200) -> list[ScratchpadNote]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM scratchpad_notes ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_note(r) for r in rows]
    finally:
        conn.close()


def search_notes(query: str, limit: int = 50) -> list[ScratchpadNote]:
    conn = connect()
    try:
        like = f"%{query}%"
        rows = conn.execute(
            "SELECT * FROM scratchpad_notes WHERE title LIKE ? OR content LIKE ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (like, like, limit),
        ).fetchall()
        return [_row_to_note(r) for r in rows]
    finally:
        conn.close()


def get_note(note_id: str) -> ScratchpadNote | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM scratchpad_notes WHERE id=?", (note_id,)).fetchone()
        return _row_to_note(row) if row else None
    finally:
        conn.close()


def create_note(title: str = "", content: str = "") -> ScratchpadNote:
    note_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    title = title.strip() or "Untitled"
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO scratchpad_notes (id, title, content, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (note_id, title, content, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return ScratchpadNote(note_id, title, content, now, now)


def update_note(note_id: str, title: str | None = None, content: str | None = None) -> ScratchpadNote | None:
    existing = get_note(note_id)
    if existing is None:
        return None
    new_title = (title if title is not None else existing.title).strip() or "Untitled"
    new_content = content if content is not None else existing.content
    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    try:
        conn.execute(
            "UPDATE scratchpad_notes SET title=?, content=?, updated_at=? WHERE id=?",
            (new_title, new_content, now, note_id),
        )
        conn.commit()
    finally:
        conn.close()
    return ScratchpadNote(note_id, new_title, new_content, existing.created_at, now)


def delete_note(note_id: str) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM scratchpad_notes WHERE id=?", (note_id,))
        conn.commit()
    finally:
        conn.close()


def _row_to_note(row) -> ScratchpadNote:
    return ScratchpadNote(row["id"], row["title"], row["content"], row["created_at"], row["updated_at"])
