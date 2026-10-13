"""End-to-end orchestrator test using the REAL asr/llm adapter classes
(network calls mocked via httpx.MockTransport) but a FAKE insertion service,
so this never actually sends a real Ctrl+V into whatever window happens to
have focus on the machine running the test.
"""
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from talkflow.asr.whisper_http_adapter import WhisperHttpAdapter
from talkflow.history import db as db_module
from talkflow.insertion.base import InsertionAdapter, InsertionOutcome
from talkflow.llm.openai_compatible_adapter import OpenAiCompatibleAdapter
from talkflow.pipeline.orchestrator import Orchestrator
from talkflow.pipeline.session import SessionState


class FakeInsertion(InsertionAdapter):
    def __init__(self):
        self.inserted_text = None

    def insert(self, text: str) -> InsertionOutcome:
        self.inserted_text = text
        return InsertionOutcome(success=True, method="fake")


@pytest.fixture()
def tmp_db(monkeypatch):
    tmp_dir = tempfile.mkdtemp()
    path = Path(tmp_dir) / "test.db"
    monkeypatch.setattr(db_module, "db_path", lambda: path)
    db_module.init_db(path)
    # Patch the connect() name as imported into every module that uses it directly.
    import talkflow.history.repository as repo
    import talkflow.formality.presets as presets_mod
    import talkflow.formality.app_assignment as assign_mod
    monkeypatch.setattr(repo, "connect", lambda: db_module.connect(path))
    monkeypatch.setattr(presets_mod, "connect", lambda: db_module.connect(path))
    monkeypatch.setattr(assign_mod, "connect", lambda: db_module.connect(path))
    return path


def test_full_pipeline_mocked_network_real_adapters(tmp_db, monkeypatch):
    def asr_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "um so I think uh we should ship it", "language": "en"})

    def llm_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "We should ship it."}}]})

    asr_client = httpx.Client(transport=httpx.MockTransport(asr_handler))
    llm_client = httpx.Client(transport=httpx.MockTransport(llm_handler))

    asr = WhisperHttpAdapter(base_url="http://localhost:8000", model_id="m", client=asr_client)
    llm = OpenAiCompatibleAdapter(base_url="https://ollama.com", model_id="qwen3.5:cloud", api_key="k", client=llm_client)
    insertion = FakeInsertion()

    # Avoid touching the real focused window / real microphone.
    import talkflow.pipeline.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "get_focused_target", lambda: type(
        "T", (), {"process_name": "notepad.exe", "window_title": "Untitled", "hwnd": 1})()
    )

    captured_states = []
    orchestrator = Orchestrator(
        asr=asr, llm=llm, insertion=insertion, allowed_languages=["en"],
        on_state_change=lambda s: captured_states.append(s.state),
    )

    from talkflow.capture.vad import FRAME_BYTES

    class FakeRecorder:
        def __init__(self, device_id=None):
            pass

        def start(self, on_frame):
            on_frame(b"\x00\x01" * (FRAME_BYTES // 2))  # one correctly-sized frame

        def stop(self):
            return (4096).to_bytes(2, "little") * 16000  # audible, so the silence gate passes

        def cancel(self):
            pass

    class AlwaysBoundaryVad:
        def feed_frame(self, frame):
            return True  # force a chunk boundary immediately, regardless of VAD speech detection

    monkeypatch.setattr(orch_mod, "AudioRecorder", FakeRecorder)
    monkeypatch.setattr(orch_mod, "ChunkBoundaryDetector", lambda: AlwaysBoundaryVad())

    orchestrator.start(trigger_mode="hold")
    orchestrator.stop()

    assert insertion.inserted_text == "We should ship it. "
    assert SessionState.COMPLETE in captured_states

    from talkflow.history import repository as repo
    rows = repo.list_recent(5)
    assert len(rows) == 1
    assert rows[0]["raw_transcript"] == "um so I think uh we should ship it"
    assert rows[0]["cleaned_transcript"] == "We should ship it."
    assert rows[0]["target_process_name"] == "notepad.exe"
    assert rows[0]["detected_language"] == "en"
