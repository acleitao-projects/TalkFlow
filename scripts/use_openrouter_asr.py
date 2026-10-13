"""One-shot setup: store your OpenRouter API key in Windows Credential Manager,
verify a real transcription request works (and time it), then make OpenRouter's
Nemotron 3.5 ASR the active ASR stage - the cloud-hosted version of the same
model the local sherpa-onnx adapter runs in-process (see
talkflow/asr/nemotron_sherpa_adapter.py), for machines where Docker/local
inference isn't an option (e.g. a locked-down work laptop with no admin
rights) but you still want the same model's behavior. Uses the same
WhisperHttpAdapter/batch-HTTP shape as the OpenRouter Whisper option this
replaced - OpenRouter's audio endpoint is a plain "/v1/audio/transcriptions"
upload regardless of model, not the true frame-level streaming the local
in-process adapter does. The local Docker/sherpa-onnx entries stay in the
registry as a fallback (switch back from the Model Registry tab).

Usage:
    python scripts/use_openrouter_asr.py (with the project venv)
"""
import getpass
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from talkflow.asr.whisper_http_adapter import WhisperHttpAdapter  # noqa: E402
from talkflow.history.db import init_db  # noqa: E402
from talkflow.registry import secrets as secret_store  # noqa: E402
from talkflow.registry import store as registry_store  # noqa: E402
from talkflow.registry.models import ModelAssignment  # noqa: E402

KEY_REF = "asr_openrouter"
BASE_URL = "https://openrouter.ai/api"  # adapter appends /v1/audio/transcriptions
MODEL_ID = "nvidia/nemotron-3.5-asr-streaming-multilingual-0.6b"


def main() -> None:
    init_db()
    registry_store.ensure_defaults()
    key = getpass.getpass("OpenRouter API key (input hidden): ").strip()
    if not key:
        print("Empty key, nothing changed.")
        return

    # Verify BEFORE touching the registry so a bad key can't break dictation.
    adapter = WhisperHttpAdapter(BASE_URL, MODEL_ID, key, timeout=30.0)
    adapter.start_session(["en"])
    adapter.set_full_audio(bytes(32000), 16000)  # 1s of silence
    try:
        for i in range(2):
            t = time.time()
            adapter.finalize()
            print(f"test request {i + 1}: {time.time() - t:.2f}s")
    except httpx.HTTPStatusError as e:
        print(f"OpenRouter rejected the request ({e.response.status_code}): {e.response.text[:300]}")
        print("Nothing was changed; local ASR is still active.")
        return
    except Exception as e:  # network etc.
        print(f"Request failed: {e}\nNothing was changed; local ASR is still active.")
        return

    secret_store.set_api_key(KEY_REF, key)
    existing = [a for a in registry_store.list_for_stage("asr") if a.provider == "openrouter"]
    registry_store.upsert(ModelAssignment(
        id=existing[0].id if existing else None, stage="asr", name="OpenRouter Nemotron 3.5 ASR",
        provider="openrouter", base_url=BASE_URL, model_id=MODEL_ID,
        api_key_ref=KEY_REF, is_active=True,
    ))
    print("Done: key saved to Credential Manager and OpenRouter is now the active ASR stage.")
    print("Restart TalkFlow to use it.")


if __name__ == "__main__":
    main()
