"""js_api surface exposed to the main window's webview JS via
`pywebview.api.<method>(...)`. Every method returns plain JSON-serializable
data (dicts/lists/primitives) since pywebview marshals return values as JSON.
"""
from __future__ import annotations

from dataclasses import asdict

from talkflow.config import autostart
from talkflow.config.settings import Settings
from talkflow.features.dictionary import store as dictionary_store
from talkflow.features.dictionary.learn import learn_from_correction
from talkflow.features.insights import store as insights_store
from talkflow.features.notetaker import pipeline as notetaker_pipeline
from talkflow.features.scratchpad import store as scratchpad_store
from talkflow.features.snippets import store as snippet_store
from talkflow.features.transforms import store as transform_store
from talkflow.formality import app_assignment, categories as category_store, presets as preset_store
from talkflow.history import repository as history_repo
from talkflow.registry import models as reg_models
from talkflow.registry import secrets as secret_store
from talkflow.registry import store as registry_store


class Api:
    # Set by app.py after construction - lets JS (both the main window and
    # the floating notes popup, which share this same Api instance) reach
    # into those windows without each window needing its own Api wiring.
    overlay = None
    floating_notes = None
    main_window = None

    # --- Settings ---
    def get_settings(self) -> dict:
        return asdict(Settings.load())

    def save_settings(self, data: dict) -> dict:
        s = Settings(**{**asdict(Settings.load()), **data})
        s.save()
        if "launch_at_login" in data:
            autostart.set_enabled(s.launch_at_login)
        return asdict(s)

    def list_input_devices(self) -> list[dict]:
        import sounddevice as sd
        return [
            {"id": i, "name": d["name"]}
            for i, d in enumerate(sd.query_devices())
            if d["max_input_channels"] > 0
        ]

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

    def delete_history_entry(self, session_id: str) -> dict:
        history_repo.delete_session(session_id)
        return {"ok": True}

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

    # --- Style categories (Personal/Work/Email/Other) ---
    def list_categories(self) -> list[dict]:
        return [asdict(c) for c in category_store.list_categories()]

    def set_category_preset(self, category_id: str, preset_id: str) -> dict:
        category_store.set_category_preset(category_id, preset_id)
        return {"ok": True}

    def list_category_apps(self, category_id: str) -> list[str]:
        return category_store.list_category_apps(category_id)

    def add_app_to_category(self, process_name: str, category_id: str) -> dict:
        if not process_name.strip():
            return {"ok": False, "error": "process name required"}
        category_store.add_app_to_category(process_name, category_id)
        return {"ok": True}

    def remove_app_from_category(self, process_name: str) -> dict:
        category_store.remove_app_from_category(process_name)
        return {"ok": True}

    def preview_cleanup(self, formality_name: str, formality_instruction: str, sample_text: str,
                         language: str = "en", cleanup_level: str = "light") -> dict:
        """Runs a real cleanup call against the active cleanup model so Style's
        preset/level preview cards show genuine output, not a fabricated mockup."""
        try:
            assignment = registry_store.get_active("cleanup")
            if assignment is None:
                return {"ok": False, "error": "no active cleanup model"}
            api_key = secret_store.get_api_key(assignment.api_key_ref) if assignment.api_key_ref else None
            from talkflow.llm.openai_compatible_adapter import OpenAiCompatibleAdapter
            adapter = OpenAiCompatibleAdapter(
                base_url=assignment.base_url, model_id=assignment.model_id, api_key=api_key, timeout=20.0)
            result = adapter.cleanup(
                raw_transcript=sample_text, language=language, formality_name=formality_name,
                formality_instruction=formality_instruction, target_process_name=None,
                cleanup_level=cleanup_level,
            )
            return {"ok": True, "text": result.cleaned_text}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

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

    def update_snippet(self, snippet_id: str, trigger: str, expansion: str) -> dict:
        updated = snippet_store.update_snippet(snippet_id, trigger, expansion)
        if updated is None:
            return {"ok": False, "error": "snippet not found"}
        return {"ok": True, **asdict(updated)}

    def delete_snippet(self, snippet_id: str) -> dict:
        snippet_store.delete_snippet(snippet_id)
        return {"ok": True}

    # --- Main window chrome (custom frameless title bar) ---
    def minimize_main_window(self) -> dict:
        if self.main_window:
            self.main_window.minimize()
        return {"ok": True}

    def maximize_main_window(self) -> dict:
        if self.main_window:
            self.main_window.maximize()
        return {"ok": True}

    def restore_main_window(self) -> dict:
        if self.main_window:
            self.main_window.restore()
        return {"ok": True}

    def hide_main_window(self) -> dict:
        if self.main_window:
            self.main_window.hide()
        return {"ok": True}

    def resize_main_window(self, width: int, height: int) -> dict:
        if self.main_window:
            self.main_window.resize(width, height)
        return {"ok": True}

    # --- Insights ---
    def get_insights(self) -> dict:
        return asdict(insights_store.get_summary())

    # --- Scratchpad ---
    def list_scratchpad_notes(self) -> list[dict]:
        return [asdict(n) for n in scratchpad_store.list_notes()]

    def search_scratchpad_notes(self, query: str) -> list[dict]:
        return [asdict(n) for n in scratchpad_store.search_notes(query)]

    def create_scratchpad_note(self, title: str = "", content: str = "") -> dict:
        return asdict(scratchpad_store.create_note(title, content))

    def update_scratchpad_note(self, note_id: str, title: str | None = None, content: str | None = None) -> dict:
        note = scratchpad_store.update_note(note_id, title, content)
        if note is None:
            return {"ok": False, "error": "note not found"}
        return {"ok": True, **asdict(note)}

    def delete_scratchpad_note(self, note_id: str) -> dict:
        scratchpad_store.delete_note(note_id)
        return {"ok": True}

    def hide_floating_notes(self) -> dict:
        if self.floating_notes:
            self.floating_notes.hide()
        return {"ok": True}

    def resize_floating_notes(self, width: int, height: int) -> dict:
        if self.floating_notes:
            self.floating_notes.resize(width, height)
        return {"ok": True}

    def set_scratchpad_in_pill(self, enabled: bool) -> dict:
        s = Settings.load()
        s.scratchpad_in_pill = enabled
        s.save()
        if self.overlay:
            self.overlay.set_flow_bar_enabled(enabled)
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

    def delete_meeting(self, session_id: str) -> dict:
        notetaker_pipeline.delete_meeting(session_id)
        return {"ok": True}

    # --- Model registry ---
    def list_registry(self, stage: str) -> list[dict]:
        return [asdict(a) for a in registry_store.list_for_stage(stage)]

    def upsert_registry(self, assignment: dict, api_key: str | None = None) -> dict:
        # A brand new entry with a real key but no key_ref yet needs one
        # generated, or set_api_key() below has nothing to store it under.
        key_ref = assignment.get("api_key_ref")
        if api_key and not key_ref:
            key_ref = f"{assignment['stage']}_{assignment['provider']}_{assignment['name']}".lower().replace(" ", "_")
        a = reg_models.ModelAssignment(
            id=assignment.get("id"), stage=assignment["stage"], name=assignment["name"],
            provider=assignment["provider"], base_url=assignment["base_url"],
            model_id=assignment["model_id"], api_key_ref=key_ref,
            is_active=bool(assignment.get("is_active", True)),
            extra_json=assignment.get("extra_json", "{}"),
        )
        new_id = registry_store.upsert(a)
        if api_key and a.api_key_ref:
            secret_store.set_api_key(a.api_key_ref, api_key)
        return {"id": new_id}

    def activate_registry(self, stage: str, assignment_id: int) -> dict:
        registry_store.activate(stage, assignment_id)
        return {"ok": True}

    def delete_registry(self, assignment_id: int) -> dict:
        registry_store.delete(assignment_id)
        return {"ok": True}

    def test_registry_entry(self, stage: str, provider: str, base_url: str, model_id: str,
                             api_key: str | None) -> dict:
        """Verify a candidate entry actually works before it's saved/activated -
        same "don't break dictation with a bad key" principle as
        scripts/setup_cloud_only.py, exposed here so the UI can do it too."""
        try:
            if stage == "asr":
                if provider == "nemotron-local":
                    from talkflow.asr.nemotron_sherpa_adapter import NemotronSherpaAdapter
                    NemotronSherpaAdapter(model_dir=base_url)  # raises if the model files are missing/invalid
                else:
                    from talkflow.asr.whisper_http_adapter import WhisperHttpAdapter
                    adapter = WhisperHttpAdapter(base_url, model_id, api_key, timeout=30.0)
                    adapter.start_session(["en"])
                    adapter.set_full_audio(bytes(32000), 16000)
                    adapter.finalize()
            elif stage == "cleanup":
                from talkflow.llm.openai_compatible_adapter import OpenAiCompatibleAdapter
                adapter = OpenAiCompatibleAdapter(base_url=base_url, model_id=model_id, api_key=api_key, timeout=30.0)
                adapter.cleanup(
                    raw_transcript="this is a test", language="en", formality_name="Normal",
                    formality_instruction="Clear, neutral, professional.", target_process_name=None,
                )
            else:
                return {"ok": False, "error": f"unknown stage {stage!r}"}
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)[:500]}
