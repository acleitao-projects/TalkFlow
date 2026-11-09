import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from talkflow.features.insights import store as insights_store
from talkflow.history import db as db_module


@pytest.fixture()
def tmp_db(monkeypatch):
    tmp_dir = tempfile.mkdtemp()
    path = Path(tmp_dir) / "test.db"
    monkeypatch.setattr(db_module, "db_path", lambda: path)
    db_module.init_db(path)
    monkeypatch.setattr(insights_store, "connect", lambda: db_module.connect(path))
    return path


def _insert_dictation(conn, text, started, ended, process_name=None, language=None):
    conn.execute(
        "INSERT INTO dictation_sessions "
        "(id, started_at, ended_at, state, raw_transcript, cleaned_transcript, "
        "target_process_name, detected_language) "
        "VALUES (?, ?, ?, 'complete', ?, ?, ?, ?)",
        (str(uuid.uuid4()), started.isoformat(), ended.isoformat(), text, text,
         process_name, language),
    )


def _insert_meeting(conn, started, ended, state="ended"):
    session_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO live_assist_sessions (id, started_at, ended_at, state) "
        "VALUES (?, ?, ?, ?)",
        (session_id, started.isoformat(), ended.isoformat() if ended else None, state),
    )
    return session_id


def _insert_utterance(conn, session_id, source, text, sequence=1):
    conn.execute(
        "INSERT INTO live_assist_utterances "
        "(id, session_id, source, sequence, captured_at, text, is_final, revision) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, 1)",
        (str(uuid.uuid4()), session_id, source, sequence,
         datetime.now(timezone.utc).isoformat(), text),
    )


def test_meeting_words_count_toward_total_words_but_not_wpm(tmp_db):
    conn = db_module.connect(tmp_db)
    try:
        now = datetime.now(timezone.utc)
        # One dictation session: 10 words over 30s -> contributes to avg_wpm.
        _insert_dictation(conn, "one two three four five six seven eight nine ten",
                           now, now + timedelta(seconds=30))

        session_id = _insert_meeting(conn, now, now + timedelta(minutes=5))
        _insert_utterance(conn, session_id, "me", "me said four words here")
        _insert_utterance(conn, session_id, "they", "they said six words in this line")
        conn.commit()
    finally:
        conn.close()

    summary = insights_store.get_summary()

    # total_words includes dictation (10) + meeting "me" (5) + meeting "they" (7).
    assert summary.total_words == 10 + 5 + 7
    assert summary.meeting_words_me == 5
    assert summary.meeting_words_they == 7
    assert summary.total_meetings == 1
    assert summary.meeting_minutes == pytest.approx(5.0, abs=0.1)

    # avg_wpm must come only from the dictation session (10 words / 0.5 min = 20).
    assert summary.avg_wpm == pytest.approx(20.0, abs=0.1)


def test_non_ended_meetings_are_excluded(tmp_db):
    conn = db_module.connect(tmp_db)
    try:
        now = datetime.now(timezone.utc)
        session_id = _insert_meeting(conn, now, None, state="active")
        _insert_utterance(conn, session_id, "me", "this should not be counted yet")
        conn.commit()
    finally:
        conn.close()

    summary = insights_store.get_summary()

    assert summary.total_meetings == 0
    assert summary.meeting_words_me == 0
    assert summary.total_words == 0


def test_no_meetings_leaves_summary_unaffected(tmp_db):
    summary = insights_store.get_summary()

    assert summary.total_meetings == 0
    assert summary.meeting_minutes == 0.0
    assert summary.meeting_words_me == 0
    assert summary.meeting_words_they == 0
    assert summary.total_words == 0
