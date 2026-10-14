"""Frame-level streaming path: an ASR adapter with capabilities.frame_level
True (the real one is NemotronSherpaAdapter) should get every captured frame
fed to it directly - no VAD batching, no segment worker, no transcribe_pcm()
- and its finalize() result should drive cleanup+paste exactly like the
default (non-partial) path. Everything external is faked; no gRPC/network
is involved.
"""
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from talkflow.asr.base import AsrCapabilities, FinalResult, PartialResult
from talkflow.capture.vad import FRAME_BYTES
from talkflow.history import db as db_module
from talkflow.insertion.base import InsertionOutcome
from talkflow.llm.base import CleanupResult
from talkflow.pipeline.orchestrator import Orchestrator

FRAME = (4096).to_bytes(2, "little") * (FRAME_BYTES // 2)


class FakeFrameLevelAsr:
    capabilities = AsrCapabilities(streaming=True, supported_languages=None, frame_level=True)

    def __init__(self):
        self.fed_frames = []
        self.finalize_calls = 0
        self.started_with = None
        self._partial_text = ""

    def start_session(self, allowed_languages, initial_prompt=None):
        self.started_with = list(allowed_languages)

    def feed_chunk(self, pcm, sample_rate):
        self.fed_frames.append(pcm)
        if len(self.fed_frames) >= 2:
            self._partial_text = "hello wor"
            return PartialResult(text=self._partial_text, detected_language="en")
        return None

    def finalize(self):
        self.finalize_calls += 1
        return FinalResult(text="hello world", detected_language="en")


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


class FakeLlm:
    def __init__(self):
        self.calls = []

    def cleanup(self, raw_transcript, language, formality_name, formality_instruction,
                target_process_name, dictionary_terms=None, snippet_candidates=None,
                previous_text=None, cleanup_level="light"):
        self.calls.append(raw_transcript)
        return CleanupResult(cleaned_text=raw_transcript.capitalize() + ".", model_used="fake", latency_ms=1.0)

    def rewrite(self, text, instruction):
        raise AssertionError("not used")


class FakeInsertion:
    def __init__(self):
        self.inserted = []

    def insert(self, text):
        self.inserted.append(text)
        return InsertionOutcome(success=True, method="fake")


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

    def build():
        asr, llm, insertion = FakeFrameLevelAsr(), FakeLlm(), FakeInsertion()
        states = []
        orch = Orchestrator(asr=asr, llm=llm, insertion=insertion, allowed_languages=["en"],
                            on_state_change=lambda s: states.append(s.state), incremental_asr=True)
        orch.start(trigger_mode="toggle")
        return orch, FakeRecorder.instance, asr, llm, insertion, states

    return build


def test_every_frame_is_fed_directly_no_vad_batching(harness):
    orch, rec, asr, llm, insertion, states = harness()
    rec.push(FRAME, count=5)
    assert len(asr.fed_frames) == 5
    assert asr.started_with == ["en"]


def test_stop_calls_finalize_and_cleans_once(harness):
    orch, rec, asr, llm, insertion, states = harness()
    rec.push(FRAME, count=5)  # > 3200 bytes so _is_silent()'s length floor doesn't short-circuit stop()
    orch.stop()
    assert asr.finalize_calls == 1
    assert llm.calls == ["hello world"]
    assert insertion.inserted == ["Hello world. "]


def test_cancel_still_ends_the_stream(harness):
    orch, rec, asr, llm, insertion, states = harness()
    rec.push(FRAME, count=2)
    orch.cancel()
    assert _wait_for(lambda: asr.finalize_calls == 1)
    assert insertion.inserted == []
