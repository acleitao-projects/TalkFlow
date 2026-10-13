from __future__ import annotations

import json

from talkflow.history.db import connect
from talkflow.registry.models import ModelAssignment


def upsert(assignment: ModelAssignment) -> int:
    conn = connect()
    try:
        if assignment.id is None:
            cur = conn.execute(
                "INSERT INTO model_assignments "
                "(stage, name, provider, base_url, model_id, api_key_ref, is_active, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (assignment.stage, assignment.name, assignment.provider, assignment.base_url,
                 assignment.model_id, assignment.api_key_ref, int(assignment.is_active),
                 assignment.extra_json),
            )
            new_id = cur.lastrowid
        else:
            conn.execute(
                "UPDATE model_assignments SET stage=?, name=?, provider=?, base_url=?, "
                "model_id=?, api_key_ref=?, is_active=?, extra_json=? WHERE id=?",
                (assignment.stage, assignment.name, assignment.provider, assignment.base_url,
                 assignment.model_id, assignment.api_key_ref, int(assignment.is_active),
                 assignment.extra_json, assignment.id),
            )
            new_id = assignment.id
        if assignment.is_active:
            conn.execute(
                "UPDATE model_assignments SET is_active=0 WHERE stage=? AND id<>?",
                (assignment.stage, new_id),
            )
        conn.commit()
        return new_id
    finally:
        conn.close()


def get_active(stage: str) -> ModelAssignment | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM model_assignments WHERE stage=? AND is_active=1 LIMIT 1", (stage,)
        ).fetchone()
        return _row_to_assignment(row) if row else None
    finally:
        conn.close()


def list_for_stage(stage: str) -> list[ModelAssignment]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM model_assignments WHERE stage=?", (stage,)).fetchall()
        return [_row_to_assignment(r) for r in rows]
    finally:
        conn.close()


def _row_to_assignment(row) -> ModelAssignment:
    return ModelAssignment(
        id=row["id"], stage=row["stage"], name=row["name"], provider=row["provider"],
        base_url=row["base_url"], model_id=row["model_id"], api_key_ref=row["api_key_ref"],
        is_active=bool(row["is_active"]), extra_json=row["extra_json"],
    )


def ensure_defaults() -> None:
    """Seed default registry rows if none exist yet for a stage: local Docker
    ASR (speaches) and Ollama Cloud for cleanup. Safe to call repeatedly."""
    if not list_for_stage("asr"):
        upsert(ModelAssignment(
            id=None, stage="asr", name="Local (Docker/speaches)", provider="local-docker",
            base_url="http://localhost:8000", model_id="Systran/faster-whisper-base",
            api_key_ref=None, is_active=True,
        ))
    if not list_for_stage("cleanup"):
        # Ollama Cloud's OpenAI-compatible base is https://ollama.com/v1, and
        # the adapter appends "/v1/chat/completions" itself, so base_url is
        # "https://ollama.com" (no /v1 suffix here). Cloud model names use a
        # ":cloud" suffix (e.g. "qwen3.5:cloud") and the catalog changes over
        # time - PLACEHOLDER model_id below; set the real one for your
        # account via the registry UI/CLI before first use.
        upsert(ModelAssignment(
            id=None, stage="cleanup", name="Ollama Cloud", provider="ollama-cloud",
            base_url="https://ollama.com", model_id="REPLACE_WITH_YOUR_OLLAMA_CLOUD_MODEL:cloud",
            api_key_ref="cleanup_ollama_cloud", is_active=True,
        ))
