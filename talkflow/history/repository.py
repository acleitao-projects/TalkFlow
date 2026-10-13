from __future__ import annotations

from talkflow.history.db import connect
from talkflow.pipeline.session import DictationSession


def save_session(session: DictationSession) -> None:
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO dictation_sessions
                (id, started_at, ended_at, state, input_device_id, trigger_mode,
                 detected_language, detected_language_confidence,
                 raw_transcript, cleaned_transcript, applied_preset_id,
                 target_process_name, target_window_title, insertion_outcome, error_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                ended_at=excluded.ended_at, state=excluded.state,
                detected_language=excluded.detected_language,
                detected_language_confidence=excluded.detected_language_confidence,
                raw_transcript=excluded.raw_transcript,
                cleaned_transcript=excluded.cleaned_transcript,
                applied_preset_id=excluded.applied_preset_id,
                target_process_name=excluded.target_process_name,
                target_window_title=excluded.target_window_title,
                insertion_outcome=excluded.insertion_outcome,
                error_code=excluded.error_code
            """,
            (
                session.id,
                session.started_at.isoformat(),
                session.ended_at.isoformat() if session.ended_at else None,
                session.state,
                session.input_device_id,
                session.trigger_mode,
                session.detected_language,
                session.detected_language_confidence,
                session.raw_transcript,
                session.cleaned_transcript,
                session.applied_preset_id,
                session.target_process_name,
                session.target_window_title,
                session.insertion_outcome,
                session.error_code,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_cleaned_transcript(session_id: str, new_text: str) -> str | None:
    """Updates a session's cleaned_transcript (e.g. the user corrected it in
    the History tab) and returns the PREVIOUS value, so callers can diff
    old-vs-new to feed the dictionary auto-learn heuristic."""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT cleaned_transcript FROM dictation_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        previous = row["cleaned_transcript"]
        conn.execute(
            "UPDATE dictation_sessions SET cleaned_transcript=? WHERE id=?",
            (new_text, session_id),
        )
        conn.commit()
        return previous
    finally:
        conn.close()


def list_recent(limit: int = 50) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM dictation_sessions ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def search(query: str, limit: int = 50) -> list[dict]:
    conn = connect()
    try:
        like = f"%{query}%"
        rows = conn.execute(
            """
            SELECT * FROM dictation_sessions
            WHERE raw_transcript LIKE ? OR cleaned_transcript LIKE ?
            ORDER BY started_at DESC LIMIT ?
            """,
            (like, like, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
