from __future__ import annotations

import uuid
from dataclasses import dataclass

from talkflow.history.db import connect


@dataclass
class FormalityPreset:
    id: str
    name: str
    instruction: str
    is_builtin: bool


def list_presets() -> list[FormalityPreset]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM formality_presets ORDER BY is_builtin DESC, name").fetchall()
        return [FormalityPreset(r["id"], r["name"], r["instruction"], bool(r["is_builtin"])) for r in rows]
    finally:
        conn.close()


def get_preset(preset_id: str) -> FormalityPreset | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM formality_presets WHERE id=?", (preset_id,)).fetchone()
        return FormalityPreset(row["id"], row["name"], row["instruction"], bool(row["is_builtin"])) if row else None
    finally:
        conn.close()


def create_preset(name: str, instruction: str) -> FormalityPreset:
    preset_id = str(uuid.uuid4())
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO formality_presets (id, name, instruction, is_builtin) VALUES (?, ?, ?, 0)",
            (preset_id, name, instruction),
        )
        conn.commit()
    finally:
        conn.close()
    return FormalityPreset(preset_id, name, instruction, False)


def delete_preset(preset_id: str) -> None:
    conn = connect()
    try:
        row = conn.execute("SELECT is_builtin FROM formality_presets WHERE id=?", (preset_id,)).fetchone()
        if row and row["is_builtin"]:
            raise ValueError("Cannot delete a built-in preset")
        conn.execute("DELETE FROM formality_presets WHERE id=?", (preset_id,))
        conn.commit()
    finally:
        conn.close()
