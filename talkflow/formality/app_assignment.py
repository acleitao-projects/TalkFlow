from __future__ import annotations

from talkflow.history.db import connect

DEFAULT_PRESET_ID = "normal"
FALLBACK_CATEGORY_ID = "other"


def get_preset_for_app(process_name: str | None) -> str:
    """Resolution order: (1) a direct per-app override in
    app_preset_assignments, if one was ever set (legacy/advanced, no longer
    surfaced in the UI but still honored) - (2) the Style category the app is
    assigned to (talkflow/formality/categories.py) - (3) the "Other" category's
    preset, the final fallback for anything not otherwise assigned."""
    if not process_name:
        return DEFAULT_PRESET_ID
    name = process_name.lower()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT preset_id FROM app_preset_assignments WHERE process_name=?", (name,)
        ).fetchone()
        if row:
            return row["preset_id"]

        row = conn.execute(
            """
            SELECT sc.preset_id FROM category_app_assignments caa
            JOIN style_categories sc ON sc.id = caa.category_id
            WHERE caa.process_name=?
            """,
            (name,),
        ).fetchone()
        if row:
            return row["preset_id"]

        row = conn.execute(
            "SELECT preset_id FROM style_categories WHERE id=?", (FALLBACK_CATEGORY_ID,)
        ).fetchone()
        return row["preset_id"] if row else DEFAULT_PRESET_ID
    finally:
        conn.close()


def set_preset_for_app(process_name: str, preset_id: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO app_preset_assignments (process_name, preset_id) VALUES (?, ?) "
            "ON CONFLICT(process_name) DO UPDATE SET preset_id=excluded.preset_id",
            (process_name.lower(), preset_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_assignments() -> dict[str, str]:
    conn = connect()
    try:
        rows = conn.execute("SELECT process_name, preset_id FROM app_preset_assignments").fetchall()
        return {r["process_name"]: r["preset_id"] for r in rows}
    finally:
        conn.close()
