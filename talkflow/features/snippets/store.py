from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from talkflow.history.db import connect

MAX_TRIGGER_CHARS = 60
MAX_EXPANSION_CHARS = 4000


@dataclass
class Snippet:
    id: str
    trigger: str
    expansion: str
    created_at: str
    updated_at: str
    is_builtin: bool = False


def list_snippets() -> list[Snippet]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM snippets ORDER BY is_builtin DESC, trigger").fetchall()
        return [
            Snippet(r["id"], r["trigger"], r["expansion"], r["created_at"], r["updated_at"], bool(r["is_builtin"]))
            for r in rows
        ]
    finally:
        conn.close()


def add_snippet(trigger: str, expansion: str) -> Snippet:
    trigger = trigger.strip()[:MAX_TRIGGER_CHARS]
    expansion = expansion[:MAX_EXPANSION_CHARS]
    now = datetime.now(timezone.utc).isoformat()
    snippet_id = str(uuid.uuid4())
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO snippets (id, trigger, expansion, created_at, updated_at, is_builtin) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (snippet_id, trigger, expansion, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return Snippet(snippet_id, trigger, expansion, now, now, is_builtin=False)


def update_snippet(snippet_id: str, trigger: str, expansion: str) -> Snippet | None:
    trigger = trigger.strip()[:MAX_TRIGGER_CHARS]
    expansion = expansion[:MAX_EXPANSION_CHARS]
    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM snippets WHERE id=?", (snippet_id,)).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE snippets SET trigger=?, expansion=?, updated_at=? WHERE id=?",
            (trigger, expansion, now, snippet_id),
        )
        conn.commit()
        return Snippet(snippet_id, trigger, expansion, row["created_at"], now, bool(row["is_builtin"]))
    finally:
        conn.close()


def delete_snippet(snippet_id: str) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM snippets WHERE id=?", (snippet_id,))
        conn.commit()
    finally:
        conn.close()
