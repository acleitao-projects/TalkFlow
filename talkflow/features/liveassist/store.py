"""Persistence for LiveAssistSession, modelled directly on
talkflow/history/repository.py::save_session's upsert pattern - every state
transition is persisted the same durable way, not just a final success-only
save.
"""
from __future__ import annotations

from talkflow.features.liveassist.session import LiveAssistSession
from talkflow.history.db import connect


def save_session(session: LiveAssistSession) -> None:
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO live_assist_sessions
                (id, started_at, ended_at, state, assistant_id, language, goal)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                ended_at=excluded.ended_at, state=excluded.state,
                assistant_id=excluded.assistant_id, language=excluded.language,
                goal=excluded.goal
            """,
            (
                session.id,
                session.started_at.isoformat(),
                session.ended_at.isoformat() if session.ended_at else None,
                session.state,
                session.assistant_id,
                session.language,
                session.goal,
            ),
        )
        conn.commit()
    finally:
        conn.close()
