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
from talkflow.features.liveassist.orchestrator import _HARD_CAP_FRAMES, LiveAssistOrchestrator
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
        self.close_calls = 0

    def close(self):
        self.close_calls += 1

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


def test_start_rebuilds_the_recorder_each_session_when_none_was_injected(tmp_db, monkeypatch):
    """Confirmed live: a changed live_assist_system_audio_process_name
    setting never took effect until the whole app was restarted, because
    the production TwoSourceRecorder() was built once at orchestrator
    construction and reused forever. start() must build a fresh one each
    session instead - but only in production (recorder=None); tests that
    inject a FakeRecorder must keep using that exact instance."""
    built = []

    class _FakeTwoSourceRecorder:
        def __init__(self):
            built.append(self)
            self.started = False

        def start(self, on_frame):
            self.started = True

        def stop(self):
            pass

    import talkflow.features.liveassist.orchestrator as orch_module
    monkeypatch.setattr(orch_module, "TwoSourceRecorder", _FakeTwoSourceRecorder)

    orch = LiveAssistOrchestrator(asr=FakeAsr(), allowed_languages=["en"], recorder=None)
    assert built == []  # not built until the first start() - construction touches no devices

    orch.start()
    first_session_recorder = orch._recorder
    assert first_session_recorder is built[0]
    orch.stop()

    orch.start()
    second_session_recorder = orch._recorder
    assert second_session_recorder is built[1]
    assert second_session_recorder is not first_session_recorder


def test_hard_cap_finalizes_a_source_the_vad_never_flags_as_speech(tmp_db):
    """Confirmed live: a source whose audio never once trips webrtcvad's
    is_speech() (plausible for system/music audio, or long quiet stretches)
    never reaches ChunkBoundaryDetector's own safety net either - both of
    its boundary conditions require _heard_speech_since_boundary, which
    silence never sets - so its sherpa-onnx stream never finalized and grew
    unbounded for the rest of the session (22GB+ RAM, climbing CPU). This
    hard cap must finalize it regardless."""
    asr = FakeAsr()
    recorder = FakeRecorder()
    orch = _new_orchestrator(asr=asr, recorder=recorder)
    orch.start()

    silent_frame = b"\x00" * 960  # 30ms of digital silence - never "speech"
    for _ in range(_HARD_CAP_FRAMES):
        recorder.on_frame("me", silent_frame)

    assert "me" in asr.finalize_calls
    assert orch._frames_since_finalize["me"] == 0  # reset after the forced finalize


def test_paused_session_drops_frames_and_does_not_forward_or_bill(tmp_db):
    """Confirmed as a real bug by review: pause() only changed session state -
    _on_frame() kept calling feed_source_chunk/finalize_source the whole time
    a session was "paused", so a paused session kept streaming audio to the
    ASR adapter (and, for a billed provider, kept being charged)."""
    asr = FakeAsr()
    recorder = FakeRecorder()
    orch = _new_orchestrator(asr=asr, recorder=recorder)
    orch.start()
    orch.pause()

    asr.partials["me"] = PartialResult(text="should never be consumed", detected_language=None)
    recorder.on_frame("me", b"\x00" * 960)

    assert asr.finalize_calls == []
    # feed_source_chunk was never called while paused, so the scripted
    # partial is still sitting there unconsumed.
    assert asr.partials["me"] is not None


def test_resume_restores_frame_forwarding(tmp_db):
    asr = FakeAsr()
    recorder = FakeRecorder()
    orch = _new_orchestrator(asr=asr, recorder=recorder)
    orch.start()
    orch.pause()
    recorder.on_frame("me", b"\x00" * 960)  # dropped while paused

    orch.resume()
    asr.finals["me"] = [FinalResult(text="hello", detected_language=None, detected_language_confidence=None)]
    for _ in range(_HARD_CAP_FRAMES):
        recorder.on_frame("me", b"\x00" * 960)

    assert "me" in asr.finalize_calls


def test_start_resets_vad_and_sequence_state_between_sessions(tmp_db):
    asr = FakeAsr()
    recorder = FakeRecorder()
    orch = _new_orchestrator(asr=asr, recorder=recorder)
    orch.start()
    asr.finals["me"] = [FinalResult(text="first session", detected_language=None, detected_language_confidence=None)]
    orch._finalize_source("me")
    assert orch._sequence["me"] == 1
    orch.stop()

    orch.start()  # second session on the SAME orchestrator instance

    assert orch._sequence["me"] == 0
    assert orch._frames_since_finalize["me"] == 0


def test_start_surfaces_as_error_when_recorder_construction_fails(tmp_db):
    """capture.py's _resolve_output_loopback fails loud on an explicit,
    invalid device override rather than silently substituting a different
    device - the orchestrator must turn that into a visible ERROR state,
    not an unhandled exception out of whatever thread called start()."""
    class _FailingRecorder:
        def __init__(self):
            raise ValueError("configured output device not found")

    # Bypass the injected-recorder path (which would skip construction) by
    # monkeypatching the module-level TwoSourceRecorder the orchestrator
    # falls back to building itself when none was injected.
    import talkflow.features.liveassist.orchestrator as orch_module
    orig = orch_module.TwoSourceRecorder
    orch_module.TwoSourceRecorder = _FailingRecorder
    try:
        orch = LiveAssistOrchestrator(asr=FakeAsr(), allowed_languages=["en"], recorder=None)
        orch.start()
    finally:
        orch_module.TwoSourceRecorder = orig

    assert orch.state == LiveAssistState.ERROR


def test_stop_closes_asr_adapter_even_when_a_finalize_call_raises(tmp_db):
    asr = FakeAsr()
    asr.fail_finalize_for.add("me")
    orch = _new_orchestrator(asr=asr)
    orch.start()

    orch.stop()

    assert asr.close_calls == 1


def test_start_rebuilds_asr_via_factory_each_session_when_none_injected(tmp_db, monkeypatch):
    """Mirrors test_start_rebuilds_the_recorder_each_session_when_none_was_injected
    - the orchestrator itself is cached long-lived in app.py, so clearing a
    module-level adapter cache alone can't reach it; the orchestrator must
    pull a fresh adapter from a factory every session instead."""
    built = []

    def factory():
        asr = FakeAsr()
        built.append(asr)
        return asr

    orch = LiveAssistOrchestrator(asr_factory=factory, allowed_languages=["en"], recorder=FakeRecorder())
    assert built == []  # not built until the first start() - construction touches no devices/network

    orch.start()
    first = orch.asr
    assert first is built[0]
    orch.stop()
    assert first.close_calls == 1

    orch.start()
    second = orch.asr
    assert second is built[1]
    assert second is not first
