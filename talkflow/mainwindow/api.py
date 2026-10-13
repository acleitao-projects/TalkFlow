"""js_api surface exposed to the main window's webview JS via
`pywebview.api.<method>(...)`. Every method returns plain JSON-serializable
data (dicts/lists/primitives) since pywebview marshals return values as JSON.
"""
from __future__ import annotations

from dataclasses import asdict

from talkflow.config.settings import Settings
from talkflow.features.dictionary import store as dictionary_store
from talkflow.features.dictionary.learn import learn_from_correction
from talkflow.features.notetaker import pipeline as notetaker_pipeline
from talkflow.features.snippets import store as snippet_store
from talkflow.features.transforms import store as transform_store
from talkflow.formality import app_assignment, presets as preset_store
from talkflow.history import repository as history_repo
from talkflow.registry import models as reg_models
from talkflow.registry import secrets as secret_store
from talkflow.registry import store as registry_store


class Api:
    # --- Settings ---
    def get_settings(self) -> dict:
        return asdict(Settings.load())

    def save_settings(self, data: dict) -> dict:
        s = Settings(**{**asdict(Settings.load()), **data})
        s.save()
        return asdict(s)

    # --- History ---
    def list_history(self, limit: int = 50) -> list[dict]:
        return history_repo.list_recent(limit)

    def search_history(self, query: str, limit: int = 50) -> list[dict]:
        return history_repo.search(query, limit)

    def correct_history_entry(self, session_id: str, new_text: str) -> dict:
        """Edit a History row's cleaned transcript and auto-learn any new
        name/term-looking tokens into the Dictionary (heuristic - see
        talkflow/features/dictionary/learn.py)."""
        previous = history_repo.update_cleaned_transcript(session_id, new_text)
        if previous is None:
            return {"ok": False, "error": "session not found"}
        learned = learn_from_correction(previous, new_text)
        return {"ok": True, "learned_terms": learned}

    # --- Formality presets ---
    def list_presets(self) -> list[dict]:
        return [asdict(p) for p in preset_store.list_presets()]

    def create_preset(self, name: str, instruction: str) -> dict:
        return asdict(preset_store.create_preset(name, instruction))

    def delete_preset(self, preset_id: str) -> dict:
        try:
            preset_store.delete_preset(preset_id)
            return {"ok": True}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def list_app_assignments(self) -> dict:
        return app_assignment.list_assignments()

    def set_app_assignment(self, process_name: str, preset_id: str) -> dict:
        app_assignment.set_preset_for_app(process_name, preset_id)
        return {"ok": True}

    # --- Dictionary ---
    def list_dictionary(self) -> list[dict]:
        return [asdict(e) for e in dictionary_store.list_entries()]

    def add_dictionary_entry(self, term: str, source: str = "manual") -> dict:
        return asdict(dictionary_store.add_entry(term, source))

    def delete_dictionary_entry(self, entry_id: str) -> dict:
        dictionary_store.delete_entry(entry_id)
        return {"ok": True}

    # --- Snippets ---
    def list_snippets(self) -> list[dict]:
        return [asdict(s) for s in snippet_store.list_snippets()]

    def add_snippet(self, trigger: str, expansion: str) -> dict:
        return asdict(snippet_store.add_snippet(trigger, expansion))

    def delete_snippet(self, snippet_id: str) -> dict:
        snippet_store.delete_snippet(snippet_id)
        return {"ok": True}

    # --- Transforms ---
    def list_custom_transforms(self) -> list[dict]:
        return [asdict(t) for t in transform_store.list_custom_transforms()]

    def create_custom_transform(self, name: str, instruction: str, hotkey: str | None = None) -> dict:
        try:
            t = transform_store.create_custom_transform(name, instruction, hotkey or None)
            return {"ok": True, **asdict(t)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def delete_custom_transform(self, transform_id: str) -> dict:
        transform_store.delete_custom_transform(transform_id)
        return {"ok": True}

    # --- Notetaker ---
    def list_meetings(self, limit: int = 20) -> list[dict]:
        return notetaker_pipeline.list_meetings(limit)

    # --- Model registry ---
    def list_registry(self, stage: str) -> list[dict]:
        return [asdict(a) for a in registry_store.list_for_stage(stage)]

    def upsert_registry(self, assignment: dict, api_key: str | None = None) -> dict:
        a = reg_models.ModelAssignment(
            id=assignment.get("id"), stage=assignment["stage"], name=assignment["name"],
            provider=assignment["provider"], base_url=assignment["base_url"],
            model_id=assignment["model_id"], api_key_ref=assignment.get("api_key_ref"),
            is_active=bool(assignment.get("is_active", True)),
            extra_json=assignment.get("extra_json", "{}"),
        )
        new_id = registry_store.upsert(a)
        if api_key and a.api_key_ref:
            secret_store.set_api_key(a.api_key_ref, api_key)
        return {"id": new_id}
