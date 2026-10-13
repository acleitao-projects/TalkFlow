"""Partial paste: cleaned text is pasted segment by segment at each pause
while recording continues. Everything external is faked (recorder, VAD, ASR,
LLM, insertion), so no audio device, network or real Ctrl+V is involved.
"""
import tempfile
import time
from pathlib import Path

import pytest

from talkflow.asr.base import FinalResult
from talkflow.capture.vad import FRAME_BYTES
from talkflow.config.settings import Settings
from talkflow.history import db as db_module
from talkflow.insertion.base import InsertionOutcome
from talkflow.llm.base import CleanupResult
from talkflow.pipeline.orchestrator import Orchestrator
from talkflow.pipeline.session import SessionState

SPEECH = (4096).to_bytes(2, "little") * (FRAME_BYTES // 2)
QUIET = b"\x00\x00" * (FRAME_BYTES // 2)
PAUSE = (1).to_bytes(2, "little") * (FRAME_BYTES // 2)    # VAD reports a real pause here
FORCED = (2).to_bytes(2, "little") * (FRAME_BYTES // 2)   # VAD's max-interval safety net
SEGMENT_FRAMES = 105  # frames of 30ms, just over MIN_SEGMENT_S


class FakeVad:
    def __init__(self, **kwargs):
        self.last_frame_was_speech = False
        self.last_boundary_was_silence = False
        self.trailing_silence_ms = 1000  # skip the tail grace sleep

    def feed_frame(self, frame):
        self.last_frame_was_speech = frame == SPEECH
        if frame == PAUSE:
            self.last_boundary_was_silence = True
            return True
        if frame == FORCED:
            self.last_boundary_was_silence = False
            return True
        return False


class FakeRecorder:
    instance = None

    def __init__(self, device_id=None):
        self.frames = []
        FakeRecorder.instance = self

    def start(self, on_frame):
        self.on_frame = on_frame

    def push(self, frame, count=1):
        for _ in range(count):
            self.frames.append(frame)
            self.on_frame(frame)

    def stop(self):
        return b"".join(self.frames)

    def cancel(self):
        pass


class FakeAsr:
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []       # (n_bytes, forced language)
        self.finalize_calls = 0

    def start_session(self, allowed_languages, initial_prompt=None):
        pass

    def feed_chunk(self, pcm, sample_rate):
        return None

    def set_full_audio(self, pcm, sample_rate):
        pass

    def finalize(self):
        self.finalize_calls += 1
        return FinalResult(text=self.texts.pop(0), detected_language="pt")

    def transcribe_pcm(self, pcm, sample_rate, language=None):
        self.calls.append((len(pcm), language))
        return FinalResult(text=self.texts.pop(0), detected_language="pt")


class FakeLlm:
    def __init__(self):
        self.calls = []

    def cleanup(self, raw_transcript, language, formality_name, formality_instruction,
                target_process_name, dictionary_terms=None, snippet_candidates=None,
                previous_text=None):
        self.calls.append({"raw": raw_transcript, "language": language, "previous_text": previous_text})
        return CleanupResult(cleaned_text=raw_transcript.capitalize() + ".", model_used="fake", latency_ms=1.0)

    def rewrite(self, text, instruction):
        raise AssertionError("not used")


class FakeInsertion:
    def __init__(self):
        self.inserted = []
        self.begins = 0
        self.ends = 0

    def begin_session(self):
        self.begins += 1

    def end_session(self):
        self.ends += 1

    def insert(self, text):
        self.inserted.append(text)
        return InsertionOutcome(success=True, method="fake")


timing_records = []


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture()
def harness(monkeypatch):
    path = Path(tempfile.mkdtemp()) / "test.db"
    monkeypatch.setattr(db_module, "db_path", lambda: path)
    db_module.init_db(path)
    import talkflow.formality.app_assignment as assign_mod
    import talkflow.formality.presets as presets_mod
    import talkflow.history.repository as repo
    import talkflow.pipeline.orchestrator as orch_mod
    for mod in (repo, presets_mod, assign_mod):
        monkeypatch.setattr(mod, "connect", lambda: db_module.connect(path))
    monkeypatch.setattr(orch_mod, "list_snippets", lambda: [])
    monkeypatch.setattr(orch_mod, "all_terms", lambda: [])
    monkeypatch.setattr(orch_mod, "build_initial_prompt", lambda: None)
    monkeypatch.setattr(orch_mod, "get_focused_target", lambda: type(
        "T", (), {"process_name": "notepad.exe", "window_title": "Untitled", "hwnd": 1})())
    monkeypatch.setattr(orch_mod, "AudioRecorder", FakeRecorder)
    monkeypatch.setattr(orch_mod, "ChunkBoundaryDetector", FakeVad)
    # Capture timing records instead of appending to the real benchmark file.
    timing_records.clear()
    monkeypatch.setattr(orch_mod.timing, "report", timing_records.append)

    def build(texts, partial_paste=True, incremental_asr=False):
        asr, llm, insertion = FakeAsr(texts), FakeLlm(), FakeInsertion()
        states = []
        orch = Orchestrator(asr=asr, llm=llm, insertion=insertion, allowed_languages=["en", "pt-BR"],
                            on_state_change=lambda s: states.append(s.state), partial_paste=partial_paste,
                            incremental_asr=incremental_asr)
        orch.start(trigger_mode="toggle")
        return orch, FakeRecorder.instance, asr, llm, insertion, states

    return build


def test_segments_are_pasted_in_order_while_still_recording(harness):
    orch, rec, asr, llm, insertion, states = harness(["one", "two", "three"])

    rec.push(SPEECH, SEGMENT_FRAMES)
    rec.push(PAUSE)
    assert _wait_for(lambda: insertion.inserted == ["One."])  # pasted before stop()
    assert states[-1] == SessionState.RECORDING

    rec.push(SPEECH, SEGMENT_FRAMES)
    rec.push(PAUSE)
    assert _wait_for(lambda: insertion.inserted == ["One.", " Two."])

    rec.push(SPEECH, 10)
    orch.stop()

    assert insertion.inserted == ["One.", " Two.", " Three."]
    assert states[-1] == SessionState.COMPLETE
    assert (insertion.begins, insertion.ends) == (1, 1)  # clipboard snapshotted/restored once
    assert asr.finalize_calls == 0
    # Only the tail (10 frames, less after trimming) was left for stop().
    assert asr.calls[2][0] <= 10 * FRAME_BYTES
    # Language detected on the first segment is forced on the later ones.
    assert [lang for _n, lang in asr.calls] == [None, "pt", "pt"]
    assert [c["previous_text"] for c in llm.calls] == [None, "One.", "One. Two."]

    from talkflow.history import repository as repo
    rows = repo.list_recent(5)
    assert len(rows) == 1
    assert rows[0]["raw_transcript"] == "one two three"
    assert rows[0]["cleaned_transcript"] == "One. Two. Three."
    assert rows[0]["detected_language"] == "pt"
    assert rows[0]["target_process_name"] == "notepad.exe"


def test_short_segment_and_forced_boundary_do_not_split(harness):
    orch, rec, asr, llm, insertion, _states = harness(["all of it"])

    rec.push(SPEECH, 20)
    rec.push(PAUSE)              # real pause, but under MIN_SEGMENT_S of audio
    rec.push(SPEECH, SEGMENT_FRAMES)
    rec.push(FORCED)             # long enough, but not a real pause
    time.sleep(0.1)
    assert asr.calls == [] and insertion.inserted == []

    orch.stop()
    assert len(asr.calls) == 1
    assert insertion.inserted == ["All of it."]


def test_noise_only_tail_after_a_pasted_segment_is_not_transcribed(harness):
    orch, rec, asr, _llm, insertion, states = harness(["one", "HALLUCINATION"])

    rec.push(SPEECH, SEGMENT_FRAMES)
    rec.push(PAUSE)
    assert _wait_for(lambda: insertion.inserted == ["One."])
    rec.push(QUIET, 30)
    orch.stop()

    assert len(asr.calls) == 1
    assert insertion.inserted == ["One."]
    assert states[-1] == SessionState.COMPLETE


def test_cancel_stops_pasting_and_ends_clipboard_session(harness):
    orch, rec, asr, _llm, insertion, states = harness(["one", "two"])

    rec.push(SPEECH, SEGMENT_FRAMES)
    rec.push(PAUSE)
    assert _wait_for(lambda: insertion.inserted == ["One."])
    rec.push(SPEECH, 10)
    orch.cancel()
    time.sleep(0.1)

    assert insertion.inserted == ["One."]
    assert (insertion.begins, insertion.ends) == (1, 1)
    assert states[-1] == SessionState.CANCELLED


def test_no_speech_at_all_is_cancelled_as_no_speech(harness):
    orch, rec, asr, _llm, insertion, states = harness([])
    rec.push(QUIET, 40)
    orch.stop()
    assert asr.calls == [] and insertion.inserted == []
    assert states[-1] == SessionState.CANCELLED


def test_single_pass_path_when_partial_paste_is_off(harness):
    orch, rec, asr, _llm, insertion, _states = harness(["everything"], partial_paste=False)
    rec.push(SPEECH, SEGMENT_FRAMES)
    rec.push(PAUSE)
    rec.push(SPEECH, SEGMENT_FRAMES)
    time.sleep(0.05)
    assert insertion.inserted == []
    orch.stop()
    assert asr.calls == [] and asr.finalize_calls == 1
    assert insertion.inserted == ["Everything."]


def test_incremental_asr_transcribes_while_recording_but_cleans_and_pastes_once(harness):
    orch, rec, asr, llm, insertion, states = harness(
        ["one", "two", "three"], partial_paste=False, incremental_asr=True)

    rec.push(SPEECH, SEGMENT_FRAMES)
    rec.push(PAUSE)
    rec.push(SPEECH, SEGMENT_FRAMES)
    rec.push(PAUSE)
    assert _wait_for(lambda: len(asr.calls) == 2)   # transcribed before stop()
    assert llm.calls == [] and insertion.inserted == []

    rec.push(SPEECH, 10)
    orch.stop()

    assert len(asr.calls) == 3 and asr.finalize_calls == 0
    # One cleanup call sees the whole dictation, one paste.
    assert [c["raw"] for c in llm.calls] == ["one two three"]
    assert llm.calls[0]["previous_text"] is None
    assert insertion.inserted == ["One two three."]
    assert states[-1] == SessionState.COMPLETE

    from talkflow.history import repository as repo
    row = repo.list_recent(1)[0]
    assert row["raw_transcript"] == "one two three"
    assert row["cleaned_transcript"] == "One two three."
    assert row["insertion_outcome"] == "fake"

    # Timing record: every step up to the paste is accounted for.
    (record,) = timing_records
    assert record["mode"] == "incremental asr, single cleanup"
    assert [seg["after_stop"] for seg in record["segments"]] == [False, False, True]
    assert all(seg["asr_ms"] >= 0 and seg["audio_s"] > 0 for seg in record["segments"])
    assert set(record["after_stop"]) == {"grace_ms", "recorder_stop_ms", "asr_wait_ms", "cleanup_ms", "paste_ms"}
    assert record["stop_to_paste_ms"] >= sum(record["after_stop"].values()) - 5
    assert (record["raw_chars"], record["clean_chars"]) == (13, 14)

    from talkflow.pipeline.timing import format_record
    text = format_record({"at": "2026-10-12T10:00:00", **record})
    assert "STOP KEY -> PASTED" in text and "one two three" not in text  # durations only, no dictated text


def test_partial_paste_allowed_only_without_held_modifiers():
    assert not Settings(hotkey="<caps_lock>", trigger_mode="hold").partial_paste_allowed()  # off by default
    assert Settings(hotkey="<caps_lock>", trigger_mode="hold", partial_paste=True).partial_paste_allowed()
    assert Settings(hotkey="<ctrl>+<shift>+d", trigger_mode="toggle", partial_paste=True).partial_paste_allowed()
    assert not Settings(hotkey="<ctrl>+<shift>+d", trigger_mode="hold", partial_paste=True).partial_paste_allowed()
    assert not Settings(hotkey="<caps_lock>", trigger_mode="toggle", partial_paste=False).partial_paste_allowed()


def test_clipboard_is_snapshotted_and_restored_once_per_session(monkeypatch):
    import talkflow.insertion.clipboard_adapter as mod

    clipboard = {"text": "user's own clipboard"}
    reads = []

    def fake_read():
        reads.append(1)
        return clipboard["text"]

    monkeypatch.setattr(mod.ClipboardAdapter, "_read_clipboard_text", staticmethod(fake_read))
    monkeypatch.setattr(mod.ClipboardAdapter, "_set_clipboard_text",
                        staticmethod(lambda text: clipboard.update(text=text)))
    monkeypatch.setattr(mod, "_RESTORE_DELAY_S", 0.0)
    monkeypatch.setattr(mod, "_PASTE_SETTLE_S", 0.0)

    class NoKeyboard:
        def pressed(self, key):
            import contextlib
            return contextlib.nullcontext()

        def press(self, key):
            pass

        def release(self, key):
            pass

    adapter = mod.ClipboardAdapter()
    adapter._kb = NoKeyboard()

    adapter.begin_session()
    assert adapter.insert("One.").success
    assert adapter.insert(" Two.").success
    time.sleep(0.05)
    assert clipboard["text"] == " Two."     # not restored between pieces
    adapter.end_session()
    assert _wait_for(lambda: clipboard["text"] == "user's own clipboard")
    assert len(reads) == 1

    # Outside a session a single insert still restores on its own.
    assert adapter.insert("solo").success
    assert _wait_for(lambda: clipboard["text"] == "user's own clipboard")
