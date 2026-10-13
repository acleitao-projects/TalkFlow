"""One-shot setup for a machine with no Docker and no local-inference option
(e.g. a locked-down work laptop with no admin rights): points BOTH pipeline
stages at cloud providers and verifies each with a real request before
committing anything to the registry.

- ASR: OpenRouter's Nemotron 3.5 ASR (same model the local sherpa-onnx
  adapter runs in-process elsewhere - see talkflow/asr/nemotron_sherpa_adapter.py -
  just hosted, not local). Equivalent to running scripts/use_openrouter_asr.py.
- Cleanup: Ollama Cloud (or any other OpenAI-compatible host, if you edit
  BASE_URL/the prompted model id below).

On a FRESH install (new machine, new talkflow.db), ensure_defaults() seeds
BOTH stages to local providers - scripts/set_llm_api_key.py only works if a
cloud cleanup assignment already exists and is active, which it won't on a
brand new database. This script creates that assignment, not just its key.

Usage:
    .venv\\Scripts\\python.exe scripts\\setup_cloud_only.py
"""
import getpass
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from talkflow.asr.whisper_http_adapter import WhisperHttpAdapter  # noqa: E402
from talkflow.history.db import init_db  # noqa: E402
from talkflow.llm.openai_compatible_adapter import OpenAiCompatibleAdapter  # noqa: E402
from talkflow.registry import secrets as secret_store  # noqa: E402
from talkflow.registry import store as registry_store  # noqa: E402
from talkflow.registry.models import ModelAssignment  # noqa: E402

ASR_KEY_REF = "asr_openrouter"
ASR_BASE_URL = "https://openrouter.ai/api"
ASR_MODEL_ID = "nvidia/nemotron-3.5-asr-streaming-multilingual-0.6b"

CLEANUP_KEY_REF = "cleanup_ollama_cloud"
CLEANUP_BASE_URL = "https://ollama.com"


def setup_asr() -> bool:
    print("\n--- ASR stage: OpenRouter Nemotron 3.5 ---")
    key = getpass.getpass("OpenRouter API key (input hidden, blank to skip ASR setup): ").strip()
    if not key:
        print("Skipped.")
        return False

    adapter = WhisperHttpAdapter(ASR_BASE_URL, ASR_MODEL_ID, key, timeout=30.0)
    adapter.start_session(["en"])
    adapter.set_full_audio(bytes(32000), 16000)  # 1s of silence
    try:
        t = time.time()
        adapter.finalize()
        print(f"Test request ok ({time.time() - t:.2f}s).")
    except httpx.HTTPStatusError as e:
        print(f"OpenRouter rejected the request ({e.response.status_code}): {e.response.text[:300]}")
        print("Nothing changed for ASR.")
        return False
    except Exception as e:
        print(f"Request failed: {e}\nNothing changed for ASR.")
        return False

    secret_store.set_api_key(ASR_KEY_REF, key)
    existing = [a for a in registry_store.list_for_stage("asr") if a.provider == "openrouter"]
    registry_store.upsert(ModelAssignment(
        id=existing[0].id if existing else None, stage="asr", name="OpenRouter Nemotron 3.5 ASR",
        provider="openrouter", base_url=ASR_BASE_URL, model_id=ASR_MODEL_ID,
        api_key_ref=ASR_KEY_REF, is_active=True,
    ))
    print("ASR stage set to OpenRouter Nemotron 3.5.")
    return True


def setup_cleanup() -> bool:
    print("\n--- Cleanup stage: Ollama Cloud ---")
    key = getpass.getpass("Ollama Cloud API key (input hidden, blank to skip cleanup setup): ").strip()
    if not key:
        print("Skipped.")
        return False
    model_id = input("Ollama Cloud model id (e.g. gemma4:31b - catalog changes over time, "
                      "check your account): ").strip()
    if not model_id:
        print("Empty model id, nothing changed for cleanup.")
        return False

    adapter = OpenAiCompatibleAdapter(base_url=CLEANUP_BASE_URL, model_id=model_id, api_key=key, timeout=30.0)
    try:
        t = time.time()
        adapter.cleanup(
            raw_transcript="this is a test", language="en", formality_name="Normal",
            formality_instruction="Clear, neutral, professional.", target_process_name=None,
        )
        print(f"Test request ok ({time.time() - t:.2f}s).")
    except Exception as e:
        print(f"Request failed: {e}\nNothing changed for cleanup.")
        return False

    secret_store.set_api_key(CLEANUP_KEY_REF, key)
    existing = [a for a in registry_store.list_for_stage("cleanup") if a.provider == "ollama-cloud"]
    registry_store.upsert(ModelAssignment(
        id=existing[0].id if existing else None, stage="cleanup", name="Ollama Cloud",
        provider="ollama-cloud", base_url=CLEANUP_BASE_URL, model_id=model_id,
        api_key_ref=CLEANUP_KEY_REF, is_active=True,
    ))
    print(f"Cleanup stage set to Ollama Cloud ({model_id}).")
    return True


def main() -> None:
    init_db()
    registry_store.ensure_defaults()
    asr_ok = setup_asr()
    cleanup_ok = setup_cleanup()
    print()
    if asr_ok or cleanup_ok:
        print("Restart TalkFlow to use the new configuration.")
    else:
        print("Nothing changed.")


if __name__ == "__main__":
    main()
