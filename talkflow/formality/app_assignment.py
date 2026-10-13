from __future__ import annotations

from talkflow.history.db import connect

DEFAULT_PRESET_ID = "normal"


def get_preset_for_app(process_name: str | None) -> str:
    if not process_name:
        return DEFAULT_PRESET_ID
    conn = connect()
    try:
        row = conn.execute(
            "SELECT preset_id FROM app_preset_assignments WHERE process_name=?",
            (process_name.lower(),),
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
