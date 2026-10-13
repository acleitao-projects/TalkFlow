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
    """Seed default registry rows if none exist yet for a stage: local
    Nemotron 3.5 streaming ASR (nemo-speech.cpp, see docker-compose.nemotron.yml)
    and local Ollama/Gemma 3 1B for cleanup. Safe to call repeatedly."""
    if not list_for_stage("asr"):
        upsert(ModelAssignment(
            id=None, stage="asr", name="Local (Docker/Nemotron 3.5 streaming)", provider="nemotron-local",
            base_url="127.0.0.1:50051", model_id="nemotron-3.5-asr-streaming-0.6b",
            api_key_ref=None, is_active=True,
        ))
    if not list_for_stage("cleanup"):
        # Local Ollama (docker-compose.nemotron.yml's ollama-local service),
        # no API key needed. Any OpenAI-compatible host still works by
        # changing base_url/model_id/api_key here - this is just the default.
        upsert(ModelAssignment(
            id=None, stage="cleanup", name="Local (Docker/Ollama, Gemma 3 1B)", provider="ollama-local",
            base_url="http://localhost:11434", model_id="gemma3:1b",
            api_key_ref=None, is_active=True,
        ))
