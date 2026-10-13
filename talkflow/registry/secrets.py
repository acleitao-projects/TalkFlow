"""API key storage via the Windows Credential Manager (through `keyring`).

Never store provider API keys in plaintext files or in the sqlite db - only
an opaque reference (the keyring "username" under the TalkFlow service name)
is persisted alongside a ModelAssignment row.
"""
from __future__ import annotations

import keyring

SERVICE_NAME = "TalkFlow"


def set_api_key(key_ref: str, api_key: str) -> None:
    keyring.set_password(SERVICE_NAME, key_ref, api_key)


def get_api_key(key_ref: str) -> str | None:
    return keyring.get_password(SERVICE_NAME, key_ref)


def delete_api_key(key_ref: str) -> None:
    try:
        keyring.delete_password(SERVICE_NAME, key_ref)
    except keyring.errors.PasswordDeleteError:
        pass
