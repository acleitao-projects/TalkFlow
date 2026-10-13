import tempfile
from pathlib import Path

import pytest

from talkflow.features.transforms import store as transform_store
from talkflow.history import db as db_module


@pytest.fixture()
def tmp_db(monkeypatch):
    tmp_dir = tempfile.mkdtemp()
    path = Path(tmp_dir) / "test.db"
    monkeypatch.setattr(db_module, "db_path", lambda: path)
    db_module.init_db(path)
    monkeypatch.setattr(transform_store, "connect", lambda: db_module.connect(path))
    return path


def test_create_list_delete(tmp_db):
    t = transform_store.create_custom_transform("Shorten", "Make this much shorter.", "<f4>")
    assert t.name == "Shorten"
    all_t = transform_store.list_custom_transforms()
    assert len(all_t) == 1
    transform_store.delete_custom_transform(t.id)
    assert transform_store.list_custom_transforms() == []


def test_max_custom_transforms_enforced(tmp_db):
    for i in range(transform_store.MAX_CUSTOM_TRANSFORMS):
        transform_store.create_custom_transform(f"T{i}", "instr")
    with pytest.raises(ValueError):
        transform_store.create_custom_transform("Overflow", "instr")
