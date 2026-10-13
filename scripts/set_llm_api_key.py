"""Store the cleanup-stage (Ollama Cloud / OpenAI-compatible) API key in the
Windows Credential Manager via `keyring` - never in a plaintext file.

Usage:
    .venv\\Scripts\\python.exe scripts\\set_llm_api_key.py

You'll be prompted for the key (input is not echoed). This can also be done
from the main window's Model Registry tab, which calls the same keyring
storage under the hood.
"""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from talkflow.registry import secrets as secret_store  # noqa: E402
from talkflow.registry import store as registry_store  # noqa: E402
from talkflow.history.db import init_db  # noqa: E402


def main() -> None:
    init_db()
    registry_store.ensure_defaults()
    assignment = registry_store.get_active("cleanup")
    if not assignment or not assignment.api_key_ref:
        print("No active cleanup-stage registry entry with a key reference found.")
        return
    print(f"Setting API key for cleanup provider '{assignment.provider}' "
          f"(keyring ref: {assignment.api_key_ref})")
    key = getpass.getpass("API key: ").strip()
    if not key:
        print("Empty key, not saving.")
        return
    secret_store.set_api_key(assignment.api_key_ref, key)
    print("Saved to Windows Credential Manager.")


if __name__ == "__main__":
    main()
