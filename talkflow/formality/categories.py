"""Style categories (Personal messages / Work messages / Email / Other) - a
coarser layer above app_assignment.py's per-app override: each category
holds one active formality preset, and an app assigned to a category picks
up that preset dynamically (changing the category's preset immediately
re-applies to every app in it, since membership and preset are stored
separately - see history/db.py's style_categories / category_app_assignments
tables)."""
from __future__ import annotations

from dataclasses import dataclass

from talkflow.history.db import connect


@dataclass
class Category:
    id: str
    name: str
    preset_id: str


def list_categories() -> list[Category]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM style_categories ORDER BY rowid").fetchall()
        return [Category(r["id"], r["name"], r["preset_id"]) for r in rows]
    finally:
        conn.close()


def get_category(category_id: str) -> Category | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM style_categories WHERE id=?", (category_id,)).fetchone()
        return Category(row["id"], row["name"], row["preset_id"]) if row else None
    finally:
        conn.close()


def set_category_preset(category_id: str, preset_id: str) -> None:
    conn = connect()
    try:
        conn.execute("UPDATE style_categories SET preset_id=? WHERE id=?", (preset_id, category_id))
        conn.commit()
    finally:
        conn.close()


def list_category_apps(category_id: str) -> list[str]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT process_name FROM category_app_assignments WHERE category_id=? ORDER BY process_name",
            (category_id,),
        ).fetchall()
        return [r["process_name"] for r in rows]
    finally:
        conn.close()


def add_app_to_category(process_name: str, category_id: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO category_app_assignments (process_name, category_id) VALUES (?, ?) "
            "ON CONFLICT(process_name) DO UPDATE SET category_id=excluded.category_id",
            (process_name.strip().lower(), category_id),
        )
        conn.commit()
    finally:
        conn.close()


def remove_app_from_category(process_name: str) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM category_app_assignments WHERE process_name=?", (process_name.lower(),))
        conn.commit()
    finally:
        conn.close()


def get_category_for_app(process_name: str) -> str | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT category_id FROM category_app_assignments WHERE process_name=?",
            (process_name.lower(),),
        ).fetchone()
        return row["category_id"] if row else None
    finally:
        conn.close()
