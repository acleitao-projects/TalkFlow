"""Single sqlite database for all of TalkFlow's local state: dictation
history, formality presets, per-app preset assignments, dictionary entries,
snippets, and the model/provider registry. No ORM - the schema is small and
stable (see CLAUDE.md's Risks section for why this was kept minimal).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from talkflow.config.paths import db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS dictation_sessions (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    state TEXT NOT NULL,
    input_device_id TEXT,
    trigger_mode TEXT,
    detected_language TEXT,
    detected_language_confidence REAL,
    raw_transcript TEXT,
    cleaned_transcript TEXT,
    applied_preset_id TEXT,
    target_process_name TEXT,
    target_window_title TEXT,
    insertion_outcome TEXT,
    error_code TEXT
);

CREATE TABLE IF NOT EXISTS formality_presets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    instruction TEXT NOT NULL,
    is_builtin INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS app_preset_assignments (
    process_name TEXT PRIMARY KEY,
    preset_id TEXT NOT NULL REFERENCES formality_presets(id)
);

-- Style categories (Personal messages / Work messages / Email / Other) -
-- each holds ONE active preset, applied to every app assigned to it. An app
-- can belong to at most one category (category_app_assignments.process_name
-- is the primary key). This sits ABOVE app_preset_assignments in resolution
-- order: see talkflow/formality/app_assignment.py::get_preset_for_app -
-- app_preset_assignments is left in place as a power-user direct override
-- that still wins if both are set for the same app.
CREATE TABLE IF NOT EXISTS style_categories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    preset_id TEXT NOT NULL REFERENCES formality_presets(id)
);

CREATE TABLE IF NOT EXISTS category_app_assignments (
    process_name TEXT PRIMARY KEY,
    category_id TEXT NOT NULL REFERENCES style_categories(id)
);

CREATE TABLE IF NOT EXISTS dictionary_entries (
    id TEXT PRIMARY KEY,
    term TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual'
);

CREATE TABLE IF NOT EXISTS snippets (
    id TEXT PRIMARY KEY,
    trigger TEXT NOT NULL,
    expansion TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    is_builtin INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS model_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage TEXT NOT NULL,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    base_url TEXT NOT NULL,
    model_id TEXT NOT NULL,
    api_key_ref TEXT,
    is_active INTEGER NOT NULL DEFAULT 0,
    extra_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS custom_transforms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    instruction TEXT NOT NULL,
    hotkey TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scratchpad_notes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notetaker_sessions (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    state TEXT NOT NULL,
    transcript TEXT,
    summary TEXT,
    action_items_json TEXT,
    my_thoughts TEXT
);
"""

_BUILTIN_PRESETS = [
    ("friendly", "Friendly", "Warm, conversational, first-person, light contractions, minimal jargon."),
    ("normal", "Normal", "Clear, neutral, professional but not stiff; standard punctuation and grammar."),
    ("formal", "Formal", "Precise, formal register, no contractions, complete sentences, no slang."),
]

# id, display name, default preset id. Apps are matched by OS process name
# (see context_provider.py) - a website running inside a shared browser
# process (Gmail/LinkedIn in chrome.exe) can't be told apart from any other
# tab, so only native desktop apps are seedable here; the UI lets the user
# add more by process name regardless.
_BUILTIN_CATEGORIES = [
    ("personal", "Personal messages", "friendly", ["whatsapp.exe", "telegram.exe", "discord.exe"]),
    ("work", "Work messages", "normal", ["slack.exe", "teams.exe", "ms-teams.exe"]),
    ("email", "Email", "normal", ["outlook.exe", "thunderbird.exe"]),
    ("other", "Other", "normal", []),
]

# Common spoken-abbreviation expansions, seeded ONLY when the snippets table
# is completely empty (first run) - not on every init - so deleting one of
# these doesn't make it silently reappear on the next app launch. Fully
# user-deletable/editable like any other snippet; is_builtin just labels them
# in the UI.
_BUILTIN_SNIPPETS = [
    ("btw", "by the way"),
    ("afaik", "as far as I know"),
    ("imo", "in my opinion"),
    ("imho", "in my humble opinion"),
    ("fyi", "for your information"),
    ("asap", "as soon as possible"),
    ("tbh", "to be honest"),
    ("idk", "I don't know"),
    ("eta", "estimated time of arrival"),
    ("wip", "work in progress"),
]


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# (table, column, "ADD COLUMN" clause) - CREATE TABLE IF NOT EXISTS won't
# retrofit a new column onto an already-existing table (this bit a real dev
# db during development: snippets.is_builtin was added after the schema had
# already been created by an earlier run), so new columns need an explicit
# entry here rather than only in SCHEMA above.
_COLUMN_MIGRATIONS = [
    ("snippets", "is_builtin", "INTEGER NOT NULL DEFAULT 0"),
]


def _apply_column_migrations(conn: sqlite3.Connection) -> None:
    for table, column, clause in _COLUMN_MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {clause}")


def init_db(path: Path | None = None) -> None:
    import uuid
    from datetime import datetime, timezone

    with contextmanager(_conn_ctx)(path) as conn:
        conn.executescript(SCHEMA)
        _apply_column_migrations(conn)
        for preset_id, name, instruction in _BUILTIN_PRESETS:
            conn.execute(
                "INSERT OR IGNORE INTO formality_presets (id, name, instruction, is_builtin) "
                "VALUES (?, ?, ?, 1)",
                (preset_id, name, instruction),
            )

        for cat_id, name, default_preset_id, default_apps in _BUILTIN_CATEGORIES:
            conn.execute(
                "INSERT OR IGNORE INTO style_categories (id, name, preset_id) VALUES (?, ?, ?)",
                (cat_id, name, default_preset_id),
            )
            for process_name in default_apps:
                conn.execute(
                    "INSERT OR IGNORE INTO category_app_assignments (process_name, category_id) VALUES (?, ?)",
                    (process_name, cat_id),
                )

        (snippet_count,) = conn.execute("SELECT COUNT(*) FROM snippets").fetchone()
        if snippet_count == 0:
            now = datetime.now(timezone.utc).isoformat()
            for trigger, expansion in _BUILTIN_SNIPPETS:
                conn.execute(
                    "INSERT INTO snippets (id, trigger, expansion, created_at, updated_at, is_builtin) "
                    "VALUES (?, ?, ?, ?, ?, 1)",
                    (str(uuid.uuid4()), trigger, expansion, now, now),
                )

        conn.commit()


def _conn_ctx(path: Path | None = None):
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()
