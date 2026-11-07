"""Set up Live Assist's optional Gemini Live transcription override in one
step, so you don't have to fill in the Model Registry's ASR fields by hand.

Creates/activates a "live_assist_asr" registry row pointed at Google's
Gemini Live API (Live Transcribe feature, model "gemini-3.5-transcribe-
live") and stores your API key in Windows Credential Manager via `keyring`
- never in a plaintext file or in chat. This only affects Live Assist's
transcription; the "asr" stage (plain dictation) and the "cleanup" stage
(Ollama) are untouched.

Get a key from Google AI Studio (https://aistudio.google.com/apikey) -
there's a free tier, but Google's own terms say free-tier traffic may be
used to improve their products, so treat it as fine for testing/personal
use, not as a privacy-equivalent swap for a paid key if that matters to you.

Usage:
    .venv\\Scripts\\python.exe scripts\\set_live_assist_gemini_key.py

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

_KEY_REF = "live_assist_asr_gemini_live"


def main() -> None:
    init_db()
    key = getpass.getpass("Gemini API key: ").strip()
    if not key:
        print("Empty key, not saving.")
        return
    registry_store.upsert(ModelAssignment(
        id=None, stage="live_assist_asr", name="Gemini Live Transcribe", provider="gemini-live",
        base_url="", model_id="gemini-3.5-transcribe-live",
        api_key_ref=_KEY_REF, is_active=True,
    ))
    secret_store.set_api_key(_KEY_REF, key)
    print("Saved and activated. Live Assist will use Gemini Live Transcribe for transcription;"
          " dictation and cleanup are unchanged. Takes effect on the next Live Assist session -"
          " no restart needed.")


if __name__ == "__main__":
    main()
