import sqlite3
import tempfile
from pathlib import Path

from talkflow.history import db as db_module


def test_column_migration_adds_missing_column_without_losing_data():
    tmp_dir = tempfile.mkdtemp()
    path = Path(tmp_dir) / "legacy.db"

    # Simulate a pre-is_builtin database with one user snippet already in it.
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE snippets (
            id TEXT PRIMARY KEY, trigger TEXT NOT NULL, expansion TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO snippets (id, trigger, expansion, created_at, updated_at) VALUES "
        "('x1', 'my sig', 'Best, Alex', '2026-01-01', '2026-01-01')"
    )
    conn.commit()
    conn.close()

    db_module.init_db(path)

    conn = db_module.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(snippets)").fetchall()}
    assert "is_builtin" in cols

    # Pre-existing user data survived the migration.
    row = conn.execute("SELECT * FROM snippets WHERE id='x1'").fetchone()
    assert row["trigger"] == "my sig"
    assert row["is_builtin"] == 0  # migrated rows default to non-builtin

    # Built-ins were seeded too (table wasn't empty, but seeding checks count
    # at the time init runs - with 1 pre-existing row it's non-zero, so
    # built-ins are intentionally NOT seeded here, matching "don't surprise
    # an existing user with 10 new snippets they didn't add").
    count = conn.execute("SELECT COUNT(*) FROM snippets").fetchone()[0]
    assert count == 1
    conn.close()
