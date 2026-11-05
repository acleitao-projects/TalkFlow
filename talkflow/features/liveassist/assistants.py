"""LA-08: Assistant/persona config - who the Live Assist suggestion engine
is acting as and what it's helping with, independent of any focused app
(unlike dictation's per-app formality presets). Same CRUD shape as
talkflow/formality/presets.py; the one builtin "General Assistant" row is
seeded by talkflow/history/db.py::init_db() following the same
seed-only-when-empty guard as _BUILTIN_PRESETS/_BUILTIN_SNIPPETS.

custom_system_prompt (optional): lets an assistant fully replace the
generic task/response_style template (see llm/prompts.py's
build_suggestion_messages) with a rich, hand-written system prompt -
needed to port the kind of detailed ROLE/TASK/STYLE/EXAMPLES persona
prompts the reference app Perssua supports, which a plain two-field
task+style pair can't express.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from talkflow.history.db import connect


@dataclass
class Assistant:
    id: str
    name: str
    task: str
    response_style: str
    created_at: str
    updated_at: str
    is_builtin: bool
    custom_system_prompt: str | None = None


def list_assistants() -> list[Assistant]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM live_assist_assistants ORDER BY is_builtin DESC, name"
        ).fetchall()
        return [_row_to_assistant(r) for r in rows]
    finally:
        conn.close()


def get_assistant(assistant_id: str) -> Assistant | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM live_assist_assistants WHERE id=?", (assistant_id,)).fetchone()
        return _row_to_assistant(row) if row else None
    finally:
        conn.close()


def create_assistant(name: str, task: str, response_style: str, custom_system_prompt: str | None = None) -> Assistant:
    assistant_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO live_assist_assistants "
            "(id, name, task, response_style, created_at, updated_at, is_builtin, custom_system_prompt) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (assistant_id, name, task, response_style, now, now, custom_system_prompt or None),
        )
        conn.commit()
    finally:
        conn.close()
    return Assistant(assistant_id, name, task, response_style, now, now, False, custom_system_prompt or None)


def update_assistant(
    assistant_id: str, name: str, task: str, response_style: str, custom_system_prompt: str | None = None,
) -> Assistant:
    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    try:
        conn.execute(
            "UPDATE live_assist_assistants SET name=?, task=?, response_style=?, updated_at=?, "
            "custom_system_prompt=? WHERE id=?",
            (name, task, response_style, now, custom_system_prompt or None, assistant_id),
        )
        conn.commit()
    finally:
        conn.close()
    assistant = get_assistant(assistant_id)
    if assistant is None:
        raise ValueError(f"No assistant with id {assistant_id!r}")
    return assistant


def delete_assistant(assistant_id: str) -> None:
    conn = connect()
    try:
        row = conn.execute("SELECT is_builtin FROM live_assist_assistants WHERE id=?", (assistant_id,)).fetchone()
        if row and row["is_builtin"]:
            raise ValueError("Cannot delete the built-in assistant")
        conn.execute("DELETE FROM live_assist_assistants WHERE id=?", (assistant_id,))
        conn.commit()
    finally:
        conn.close()


def _row_to_assistant(row) -> Assistant:
    keys = row.keys()
    return Assistant(
        id=row["id"], name=row["name"], task=row["task"], response_style=row["response_style"],
        created_at=row["created_at"], updated_at=row["updated_at"], is_builtin=bool(row["is_builtin"]),
        custom_system_prompt=row["custom_system_prompt"] if "custom_system_prompt" in keys else None,
    )
