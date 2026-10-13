"""ModelAssignment: the configuration for one pipeline stage's provider.

Every stage (asr, cleanup, command, transform, notetaker_summary, ...) has
zero or more assignments; exactly one is active per stage at a time. The
actual secret lives in the OS keyring, referenced here by `api_key_ref`.
"""
from __future__ import annotations

from dataclasses import dataclass

Stage = str  # "asr" | "cleanup" | "command" | "transform" | "notetaker_summary"


@dataclass
class ModelAssignment:
    id: int | None
    stage: Stage
    name: str
    provider: str  # free-form label, e.g. "ollama-cloud", "openai", "local-docker"
    base_url: str
    model_id: str
    api_key_ref: str | None  # keyring lookup key; None for providers that need no key (e.g. local docker)
    is_active: bool
    extra_json: str = "{}"
