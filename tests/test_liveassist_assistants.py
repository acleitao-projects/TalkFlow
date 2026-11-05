"""LA-08: assistant/persona CRUD - same shape as talkflow/formality/presets.py,
tested the same way (tmp_db fixture, real sqlite, no mocking)."""
import tempfile
from pathlib import Path

import pytest

from talkflow.features.liveassist import assistants
from talkflow.history import db as db_module


@pytest.fixture()
def tmp_db(monkeypatch):
    path = Path(tempfile.mkdtemp()) / "test.db"
    monkeypatch.setattr(db_module, "db_path", lambda: path)
    db_module.init_db(path)
    monkeypatch.setattr(assistants, "connect", lambda: db_module.connect(path))
    return path


def test_builtin_general_assistant_is_seeded(tmp_db):
    rows = assistants.list_assistants()
    assert len(rows) == 1
    assert rows[0].name == "General Assistant"
    assert rows[0].is_builtin is True


def test_create_update_delete_roundtrip(tmp_db):
    a = assistants.create_assistant("Sales Coach", "Help close deals", "Confident, concise")
    assert a.is_builtin is False
    assert assistants.get_assistant(a.id).name == "Sales Coach"

    updated = assistants.update_assistant(a.id, "Sales Coach v2", "Help close deals faster", "Punchy")
    assert updated.name == "Sales Coach v2"
    assert updated.task == "Help close deals faster"

    assistants.delete_assistant(a.id)
    assert assistants.get_assistant(a.id) is None


def test_cannot_delete_builtin_assistant(tmp_db):
    builtin = assistants.list_assistants()[0]
    with pytest.raises(ValueError):
        assistants.delete_assistant(builtin.id)
    assert assistants.get_assistant(builtin.id) is not None


def test_custom_system_prompt_round_trips_through_create_and_update(tmp_db):
    prompt = "<SYSTEM_PROMPT><ROLE>You are a PM candidate.</ROLE></SYSTEM_PROMPT>"
    a = assistants.create_assistant("Product Manager Interview", "task", "style", prompt)
    assert a.custom_system_prompt == prompt
    assert assistants.get_assistant(a.id).custom_system_prompt == prompt

    cleared = assistants.update_assistant(a.id, "Product Manager Interview", "task", "style", None)
    assert cleared.custom_system_prompt is None


def test_builtin_assistant_has_no_custom_prompt_by_default(tmp_db):
    builtin = assistants.list_assistants()[0]
    assert builtin.custom_system_prompt is None
