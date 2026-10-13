import tempfile
from pathlib import Path

import pytest

from talkflow.history import db as db_module
from talkflow.registry.models import ModelAssignment


@pytest.fixture()
def tmp_db(monkeypatch):
    tmp_dir = tempfile.mkdtemp()
    path = Path(tmp_dir) / "test.db"
    monkeypatch.setattr(db_module, "db_path", lambda: path)
    db_module.init_db(path)
    return path


def test_init_db_seeds_builtin_presets(tmp_db):
    conn = db_module.connect(tmp_db)
    rows = conn.execute("SELECT name FROM formality_presets WHERE is_builtin=1").fetchall()
    names = {r["name"] for r in rows}
    assert names == {"Friendly", "Normal", "Formal"}
    conn.close()


def test_registry_upsert_and_single_active_per_stage(tmp_db, monkeypatch):
    from talkflow.registry import store

    monkeypatch.setattr(store, "connect", lambda: db_module.connect(tmp_db))

    id1 = store.upsert(ModelAssignment(
        id=None, stage="asr", name="A", provider="p", base_url="http://a", model_id="m1",
        api_key_ref=None, is_active=True,
    ))
    id2 = store.upsert(ModelAssignment(
        id=None, stage="asr", name="B", provider="p", base_url="http://b", model_id="m2",
        api_key_ref=None, is_active=True,
    ))

    active = store.get_active("asr")
    assert active.id == id2
    assert active.name == "B"

    all_rows = store.list_for_stage("asr")
    assert len(all_rows) == 2
    inactive = [r for r in all_rows if r.id == id1][0]
    assert inactive.is_active is False
