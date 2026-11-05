"""LA-09: SuggestionEngine's supersede logic - a second manual ask must drop
a still-in-flight first request's result instead of showing stale text."""
import tempfile
import threading
from pathlib import Path

import pytest

from talkflow.features.liveassist import store as store_module
from talkflow.features.liveassist.session import LiveAssistSession, LiveAssistState
from talkflow.features.liveassist.suggestion_engine import SuggestionEngine
from talkflow.history import db as db_module
from talkflow.llm.base import SuggestionResult


@pytest.fixture()
def tmp_db(monkeypatch):
    path = Path(tempfile.mkdtemp()) / "test.db"
    monkeypatch.setattr(db_module, "db_path", lambda: path)
    db_module.init_db(path)
    monkeypatch.setattr(store_module, "connect", lambda: db_module.connect(path))
    return path


def _new_session(tmp_db) -> str:
    session = LiveAssistSession(state=LiveAssistState.ACTIVE)
    store_module.save_session(session)
    return session.id


class _GatedFakeLlm:
    """Blocks suggest() on an Event so the test can control exactly when
    each in-flight request's result is produced, deterministically racing
    a second request against a still-running first one."""

    def __init__(self):
        self.calls = []
        self._gates: dict[int, threading.Event] = {}
        self._call_index = -1

    def release(self, call_index: int) -> None:
        self._gates[call_index].set()

    def suggest(self, transcript_lines, assistant_name, assistant_task, assistant_style, language,
                custom_system_prompt=None):
        with threading.Lock():
            self._call_index += 1
            my_index = self._call_index
        self._gates[my_index] = threading.Event()
        self.calls.append(my_index)
        self._gates[my_index].wait(timeout=5)
        return SuggestionResult(suggestion_text=f"suggestion #{my_index}", has_suggestion=True,
                                 model_used="fake", latency_ms=1.0)


def test_second_ask_supersedes_a_still_in_flight_first_request(tmp_db):
    session_id = _new_session(tmp_db)
    llm = _GatedFakeLlm()
    results = []
    done = threading.Event()

    def on_result(text, has_suggestion):
        results.append((text, has_suggestion))
        done.set()

    engine = SuggestionEngine(llm, on_result)
    engine.request(session_id, assistant_id=None, language="en")
    # Wait until the first call has actually started (entered suggest()) before firing the second.
    while len(llm.calls) < 1:
        pass
    engine.request(session_id, assistant_id=None, language="en")
    while len(llm.calls) < 2:
        pass

    # Release the SECOND (newer) request first, then the stale first one.
    llm.release(1)
    done.wait(timeout=5)
    done.clear()
    llm.release(0)
    import time
    time.sleep(0.2)  # give the stale callback a chance to (wrongly) fire

    assert results == [("suggestion #1", True)]  # only the newer request's result was delivered


def test_single_ask_delivers_its_result(tmp_db):
    session_id = _new_session(tmp_db)
    llm = _GatedFakeLlm()
    results = []
    done = threading.Event()

    def on_result(text, has_suggestion):
        results.append((text, has_suggestion))
        done.set()

    engine = SuggestionEngine(llm, on_result)
    engine.request(session_id, assistant_id=None, language="en")
    while len(llm.calls) < 1:
        pass
    llm.release(0)
    assert done.wait(timeout=5)
    assert results == [("suggestion #0", True)]
