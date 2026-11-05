"""Set up Live Assist's optional OpenAI transcription override in one step,
so you don't have to fill in the Model Registry's ASR fields by hand.

Creates/activates a "live_assist_asr" registry row pointed at OpenAI's real
Whisper API (base_url/model_id below) and stores your API key in Windows
Credential Manager via `keyring` - never in a plaintext file. This only
affects Live Assist's transcription; the "asr" stage (plain dictation) and
the "cleanup" stage (Ollama) are untouched.

Usage:
    .venv\\Scripts\\python.exe scripts\\set_live_assist_openai_key.py

You'll be prompted for the key (input is not echoed). This can also be done
from the main window's Model Registry tab, under "Live Assist ASR".
"""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from talkflow.history.db import init_db  # noqa: E402
from talkflow.registry import secrets as secret_store  # noqa: E402
from talkflow.registry import store as registry_store  # noqa: E402
from talkflow.registry.models import ModelAssignment  # noqa: E402

_KEY_REF = "live_assist_asr_openai"


def main() -> None:
    init_db()
    key = getpass.getpass("OpenAI API key: ").strip()
    if not key:
        print("Empty key, not saving.")
        return
    registry_store.upsert(ModelAssignment(
        id=None, stage="live_assist_asr", name="OpenAI Whisper", provider="openai",
        base_url="https://api.openai.com", model_id="whisper-1",
        api_key_ref=_KEY_REF, is_active=True,
    ))
    secret_store.set_api_key(_KEY_REF, key)
    print("Saved and activated. Live Assist will use OpenAI Whisper for transcription;"
          " dictation and cleanup are unchanged. Restart TalkFlow to pick it up.")


if __name__ == "__main__":
    main()
