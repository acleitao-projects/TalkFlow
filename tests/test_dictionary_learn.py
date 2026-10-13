import tempfile
from pathlib import Path

import pytest

from talkflow.features.dictionary import learn as learn_mod
from talkflow.features.dictionary import store as dict_store
from talkflow.history import db as db_module


@pytest.fixture()
def tmp_db(monkeypatch):
    tmp_dir = tempfile.mkdtemp()
    path = Path(tmp_dir) / "test.db"
    monkeypatch.setattr(db_module, "db_path", lambda: path)
    db_module.init_db(path)
    monkeypatch.setattr(dict_store, "connect", lambda: db_module.connect(path))
    return path


def test_suggests_capitalized_new_name():
    original = "please send this to jeff tomorrow"
    edited = "please send this to Geoffrey tomorrow"
    terms = learn_mod.suggest_learnable_terms(original, edited)
    assert "Geoffrey" in terms


def test_suggests_acronym():
    original = "call the a p i team"
    edited = "call the API team"
    terms = learn_mod.suggest_learnable_terms(original, edited)
    assert "API" in terms


def test_skips_common_words_and_urls():
    original = "go to the site"
    edited = "go to https://example.com/path and the Website"
    terms = learn_mod.suggest_learnable_terms(original, edited)
    assert "https" not in terms
    assert not any("example.com" in t for t in terms)


def test_skips_already_known_terms(tmp_db):
    dict_store.add_entry("FigJam", source="manual")
    terms = learn_mod.suggest_learnable_terms("open the doc", "open FigJam")
    assert "FigJam" not in terms


def test_learn_from_correction_persists(tmp_db):
    learned = learn_mod.learn_from_correction("meet with jon", "meet with Jonathan")
    assert "Jonathan" in learned
    assert "Jonathan" in dict_store.all_terms()
