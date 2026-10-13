"""Switch which ASR registry entry is active (e.g. to compare the local Docker
Whisper container against a cloud host). Only flips the active flag - no
entry is created, edited or deleted. Restart TalkFlow afterwards.

Usage:
    .venv\\Scripts\\python.exe scripts\\switch_asr.py                # list entries
    .venv\\Scripts\\python.exe scripts\\switch_asr.py local-docker   # activate by provider
    .venv\\Scripts\\python.exe scripts\\switch_asr.py openrouter
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from talkflow.history.db import init_db  # noqa: E402
from talkflow.registry import store as registry_store  # noqa: E402


def main() -> None:
    init_db()
    registry_store.ensure_defaults()
    entries = registry_store.list_for_stage("asr")
    if len(sys.argv) > 1:
        matches = [a for a in entries if a.provider == sys.argv[1]]
        if not matches:
            print(f"No ASR entry with provider {sys.argv[1]!r}.")
        else:
            matches[0].is_active = True
            registry_store.upsert(matches[0])
            print(f"Active ASR is now {matches[0].provider} ({matches[0].model_id}). Restart TalkFlow.")
        entries = registry_store.list_for_stage("asr")
    for a in entries:
        print(f"{'*' if a.is_active else ' '} {a.provider:14} {a.base_url:28} {a.model_id}")


if __name__ == "__main__":
    main()
