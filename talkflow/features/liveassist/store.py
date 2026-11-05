"""Persistence for LiveAssistSession, modelled directly on
talkflow/history/repository.py::save_session's upsert pattern - every state
transition is persisted the same durable way, not just a final success-only
save.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

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


def save_utterance(session_id: str, source: str, sequence: int, text: str) -> None:
    """A committed FINAL utterance only - interim text is an in-memory
    projection, never written here (same raw/cleaned-never-collapse
    discipline this app already applies to dictation). Each call is a plain
    insert, not an upsert: sequence numbers are per-source and monotonically
    increasing, so there is nothing to conflict with yet - revision/
    update-in-place is for a later story."""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO live_assist_utterances "
            "(id, session_id, source, sequence, captured_at, text, is_final, revision) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, 1)",
            (str(uuid.uuid4()), session_id, source, sequence,
             datetime.now(timezone.utc).isoformat(), text),
        )
        conn.commit()
    finally:
        conn.close()


def list_utterances(session_id: str) -> list[tuple[str, str]]:
    """Ordered (source, text) pairs for a session - what LA-09's suggestion
    engine grounds its draft in. Reads from the DB rather than requiring the
    orchestrator to keep its own duplicate in-memory transcript list."""
    conn = connect()
    try:
        rows = conn.execute(
            # captured_at, not sequence: sequence only increments within one
            # source, so ordering by it would group all of "me"'s lines
            # before "they"'s whenever their sequence numbers tie, instead
            # of the actual chronological back-and-forth of a conversation.
            "SELECT source, text FROM live_assist_utterances WHERE session_id=? ORDER BY captured_at",
            (session_id,),
        ).fetchall()
        return [(r["source"], r["text"]) for r in rows]
    finally:
        conn.close()


def save_suggestion(session_id: str, trigger: str, text: str, kind: str = "response") -> str:
    """LA-09: persists a generated suggestion for history (LA-12) - never the
    spoken transcript itself, so LA-12's "suggestions never rendered as if
    they were spoken content" acceptance has a real structural distinction
    (live_assist_suggestions vs live_assist_utterances) to rely on, not just
    UI styling. `trigger` is "manual" (LA-09) or "auto" (LA-13, later)."""
    suggestion_id = str(uuid.uuid4())
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO live_assist_suggestions (id, session_id, trigger, text, kind, created_at, disposition) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (suggestion_id, session_id, trigger, text, kind, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return suggestion_id
