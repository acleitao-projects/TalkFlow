from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from talkflow.history.db import connect

MAX_CUSTOM_TRANSFORMS = 8


@dataclass
class CustomTransform:
    id: str
    name: str
    instruction: str
    hotkey: str | None
    created_at: str


def list_custom_transforms() -> list[CustomTransform]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM custom_transforms ORDER BY created_at").fetchall()
        return [CustomTransform(r["id"], r["name"], r["instruction"], r["hotkey"], r["created_at"]) for r in rows]
    finally:
        conn.close()


def create_custom_transform(name: str, instruction: str, hotkey: str | None = None) -> CustomTransform:
    existing = list_custom_transforms()
    if len(existing) >= MAX_CUSTOM_TRANSFORMS:
        raise ValueError(f"Maximum of {MAX_CUSTOM_TRANSFORMS} custom transforms reached")
    transform_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO custom_transforms (id, name, instruction, hotkey, created_at) VALUES (?, ?, ?, ?, ?)",
            (transform_id, name, instruction, hotkey, created_at),
        )
        conn.commit()
    finally:
        conn.close()
    return CustomTransform(transform_id, name, instruction, hotkey, created_at)


def delete_custom_transform(transform_id: str) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM custom_transforms WHERE id=?", (transform_id,))
        conn.commit()
    finally:
        conn.close()
