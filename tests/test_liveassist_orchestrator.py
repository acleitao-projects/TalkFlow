"""LA-02/LA-03: live_assist_* schema + LiveAssistOrchestrator's session
lifecycle. LA-04/LA-05: capture + ASR wiring, tested with fakes (no real
audio devices / sherpa-onnx model - those are LA-04/LA-05's live acceptance,
proven by hand, not unit-testable).
"""
import tempfile
from pathlib import Path

import pytest

from talkflow.asr.base import FinalResult, PartialResult
from talkflow.features.liveassist import store as store_module
from talkflow.features.liveassist.orchestrator import LiveAssistOrchestrator
from talkflow.features.liveassist.session import LiveAssistState
from talkflow.history import db as db_module


class FakeRecorder:
    """Stands in for TwoSourceRecorder - start() just remembers the
    callback so the test can drive frames directly instead of opening
    real audio devices."""

    def __init__(self):
        self.on_frame = None
        self.started = False
        self.stopped = False

    def start(self, on_frame):
        self.on_frame = on_frame
        self.started = True

    def stop(self):
        self.stopped = True


class FakeAsr:
    """Stands in for MultiStreamAsrAdapter - scriptable per source via
    `finals[source_id]` (a list of FinalResult, consumed in order) and
    `partials[source_id]` (one PartialResult returned on the next
    feed_source_chunk call for that source, then cleared)."""

    def __init__(self):
        self.started_sources = []
        self.finalize_calls = []
        self.finals: dict[str, list[FinalResult]] = {"me": [], "they": []}
        self.partials: dict[str, PartialResult | None] = {"me": None, "they": None}
        self.fail_finalize_for: set[str] = set()

    def start_source(self, source_id, allowed_languages):
        self.started_sources.append(source_id)

    def feed_source_chunk(self, source_id, pcm):
        partial = self.partials.get(source_id)
        self.partials[source_id] = None
        return partial

    def finalize_source(self, source_id):
        self.finalize_calls.append(source_id)
        if source_id in self.fail_finalize_for:
            raise RuntimeError(f"simulated finalize failure for {source_id}")
        queue = self.finals.get(source_id) or []
        if queue:
            return queue.pop(0)
        return FinalResult(text="", detected_language=None, detected_language_confidence=None)


@pytest.fixture()
def tmp_db(monkeypatch):
    tmp_dir = tempfile.mkdtemp()
    path = Path(tmp_dir) / "test.db"
    monkeypatch.setattr(db_module, "db_path", lambda: path)
    db_module.init_db(path)
    monkeypatch.setattr(store_module, "connect", lambda: db_module.connect(path))
    return path


def _new_orchestrator(**kwargs):
    kwargs.setdefault("asr", FakeAsr())
    kwargs.setdefault("allowed_languages", ["en"])
    kwargs.setdefault("recorder", FakeRecorder())
    return LiveAssistOrchestrator(**kwargs)


def _session_rows(path):
    conn = db_module.connect(path)
    try:
        return conn.execute(
            "SELECT state, ended_at FROM live_assist_sessions ORDER BY started_at"
        ).fetchall()
    finally:
        conn.close()


def _utterance_rows(path):
    conn = db_module.connect(path)
    try:
        return conn.execute(
            "SELECT source, sequence, text, is_final FROM live_assist_utterances "
            "ORDER BY source, sequence"
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
    orch = _new_orchestrator(on_state_change=lambda s: seen_states.append(s.state))

    orch.start()

    assert orch.state == LiveAssistState.ACTIVE
    assert seen_states == [LiveAssistState.ARMING, LiveAssistState.ACTIVE]
    rows = _session_rows(tmp_db)
    assert len(rows) == 1
    assert rows[0]["state"] == LiveAssistState.ACTIVE.value


def test_start_arms_both_sources_and_recorder(tmp_db):
    asr = FakeAsr()
    recorder = FakeRecorder()
    orch = _new_orchestrator(asr=asr, recorder=recorder)

    orch.start()

    assert set(asr.started_sources) == {"me", "they"}
    assert recorder.started


def test_start_while_already_active_is_a_noop(tmp_db):
    orch = _new_orchestrator()
    orch.start()
    session_id = orch._session.id

    orch.start()  # should not create a second session

    assert orch._session.id == session_id
    assert len(_session_rows(tmp_db)) == 1


def test_pause_resume_toggle_and_persist(tmp_db):
    orch = _new_orchestrator()
    orch.start()

    orch.pause()
    assert orch.state == LiveAssistState.PAUSED

    orch.resume()
    assert orch.state == LiveAssistState.ACTIVE

    rows = _session_rows(tmp_db)
    assert len(rows) == 1
    assert rows[0]["state"] == LiveAssistState.ACTIVE.value


def test_stop_is_idempotent_and_reaches_ended_with_timestamp(tmp_db):
    orch = _new_orchestrator()
    orch.start()

    orch.stop()
    assert orch.state == LiveAssistState.ENDED
    rows = _session_rows(tmp_db)
    assert len(rows) == 1
    assert rows[0]["state"] == LiveAssistState.ENDED.value
    assert rows[0]["ended_at"] is not None

    orch.stop()  # idempotent - no error, no second row
    assert orch.state == LiveAssistState.ENDED
    assert len(_session_rows(tmp_db)) == 1


def test_stop_after_pause_reaches_ended(tmp_db):
    orch = _new_orchestrator()
    orch.start()
    orch.pause()

    orch.stop()

    assert orch.state == LiveAssistState.ENDED
    rows = _session_rows(tmp_db)
    assert rows[0]["state"] == LiveAssistState.ENDED.value


def test_interim_result_is_emitted_but_never_persisted(tmp_db):
    asr = FakeAsr()
    recorder = FakeRecorder()
    seen = []
    orch = _new_orchestrator(asr=asr, recorder=recorder, on_utterance=lambda s, t, f: seen.append((s, t, f)))
    orch.start()

    asr.partials["me"] = PartialResult(text="hello there", detected_language=None)
    recorder.on_frame("me", b"\x00" * 960)

    assert ("me", "hello there", False) in seen
    assert _utterance_rows(tmp_db) == []


def test_final_result_is_persisted_with_correct_source_and_sequence(tmp_db):
    asr = FakeAsr()
    recorder = FakeRecorder()
    orch = _new_orchestrator(asr=asr, recorder=recorder)
    orch.start()

    asr.finals["me"] = [FinalResult(text="hello", detected_language="en", detected_language_confidence=None)]
    orch._finalize_source("me")

    rows = _utterance_rows(tmp_db)
    assert len(rows) == 1
    assert rows[0]["source"] == "me"
    assert rows[0]["sequence"] == 1
    assert rows[0]["text"] == "hello"
    assert rows[0]["is_final"] == 1


def test_sequence_increments_independently_per_source(tmp_db):
    asr = FakeAsr()
    orch = _new_orchestrator(asr=asr)
    orch.start()

    asr.finals["me"] = [
        FinalResult(text="first", detected_language=None, detected_language_confidence=None),
        FinalResult(text="second", detected_language=None, detected_language_confidence=None),
    ]
    asr.finals["they"] = [FinalResult(text="their line", detected_language=None, detected_language_confidence=None)]

    orch._finalize_source("me")
    orch._finalize_source("me")
    orch._finalize_source("they")

    rows = {(r["source"], r["sequence"]): r["text"] for r in _utterance_rows(tmp_db)}
    assert rows[("me", 1)] == "first"
    assert rows[("me", 2)] == "second"
    assert rows[("they", 1)] == "their line"


def test_stop_finalizes_both_sources_and_one_failure_does_not_drop_the_other(tmp_db):
    asr = FakeAsr()
    orch = _new_orchestrator(asr=asr)
    orch.start()

    asr.finals["they"] = [FinalResult(text="still captured", detected_language=None, detected_language_confidence=None)]
    asr.fail_finalize_for.add("me")

    orch.stop()  # must not raise, despite "me"'s finalize failing

    assert orch.state == LiveAssistState.ENDED
    rows = _utterance_rows(tmp_db)
    assert len(rows) == 1
    assert rows[0]["source"] == "they"
    assert rows[0]["text"] == "still captured"
