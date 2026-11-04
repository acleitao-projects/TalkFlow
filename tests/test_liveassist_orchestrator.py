"""LA-02/LA-03: live_assist_* schema + LiveAssistOrchestrator's session
lifecycle skeleton (fake/no-op capture - no real ASR/audio involved).
"""
import tempfile
from pathlib import Path

import pytest

from talkflow.features.liveassist import store as store_module
from talkflow.features.liveassist.orchestrator import LiveAssistOrchestrator
from talkflow.features.liveassist.session import LiveAssistState
from talkflow.history import db as db_module


@pytest.fixture()
def tmp_db(monkeypatch):
    tmp_dir = tempfile.mkdtemp()
    path = Path(tmp_dir) / "test.db"
    monkeypatch.setattr(db_module, "db_path", lambda: path)
    db_module.init_db(path)
    monkeypatch.setattr(store_module, "connect", lambda: db_module.connect(path))
    return path


def _rows(path):
    conn = db_module.connect(path)
    try:
        return conn.execute(
            "SELECT state, ended_at FROM live_assist_sessions ORDER BY started_at"
        ).fetchall()
    finally:
        conn.close()


def test_schema_seeds_one_builtin_assistant_and_is_idempotent(tmp_db):
    conn = db_module.connect(tmp_db)
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM live_assist_assistants").fetchone()
        assert count == 1
        (name,) = conn.execute("SELECT name FROM live_assist_assistants").fetchone()
        assert name == "General Assistant"
    finally:
        conn.close()

    # Re-running init_db() against the same (now-existing) file must not
    # reseed or duplicate the builtin row.
    db_module.init_db(tmp_db)
    conn = db_module.connect(tmp_db)
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM live_assist_assistants").fetchone()
        assert count == 1
    finally:
        conn.close()


def test_start_transitions_arming_then_active_and_persists_both(tmp_db):
    seen_states = []
    orch = LiveAssistOrchestrator(on_state_change=lambda s: seen_states.append(s.state))

    orch.start()

    assert orch.state == LiveAssistState.ACTIVE
    assert seen_states == [LiveAssistState.ARMING, LiveAssistState.ACTIVE]
    rows = _rows(tmp_db)
    assert len(rows) == 1
    assert rows[0]["state"] == LiveAssistState.ACTIVE.value


def test_start_while_already_active_is_a_noop(tmp_db):
    orch = LiveAssistOrchestrator()
    orch.start()
    session_id = orch._session.id

    orch.start()  # should not create a second session

    assert orch._session.id == session_id
    assert len(_rows(tmp_db)) == 1


def test_pause_resume_toggle_and_persist(tmp_db):
    orch = LiveAssistOrchestrator()
    orch.start()

    orch.pause()
    assert orch.state == LiveAssistState.PAUSED

    orch.resume()
    assert orch.state == LiveAssistState.ACTIVE

    # Only ever one session row, state column reflects the latest transition.
    rows = _rows(tmp_db)
    assert len(rows) == 1
    assert rows[0]["state"] == LiveAssistState.ACTIVE.value


def test_stop_is_idempotent_and_reaches_ended_with_timestamp(tmp_db):
    orch = LiveAssistOrchestrator()
    orch.start()

    orch.stop()
    assert orch.state == LiveAssistState.ENDED
    rows = _rows(tmp_db)
    assert len(rows) == 1
    assert rows[0]["state"] == LiveAssistState.ENDED.value
    assert rows[0]["ended_at"] is not None

    orch.stop()  # idempotent - no error, no second row
    assert orch.state == LiveAssistState.ENDED
    assert len(_rows(tmp_db)) == 1


def test_stop_after_pause_reaches_ended(tmp_db):
    orch = LiveAssistOrchestrator()
    orch.start()
    orch.pause()

    orch.stop()

    assert orch.state == LiveAssistState.ENDED
    rows = _rows(tmp_db)
    assert rows[0]["state"] == LiveAssistState.ENDED.value
