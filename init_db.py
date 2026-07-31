#!/usr/bin/env python3
"""
PeerShop — Phase 1 database initializer.

Creates (or updates, idempotently) peershop.db from schema.sql.
Everything here is standard-library only: sqlite3 ships with Python.

Usage:
    python3 init_db.py            # create/refresh ./peershop.db
    python3 init_db.py --show     # create, then print schema + seeded categories
"""

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "peershop.db")
SCHEMA_PATH = os.path.join(HERE, "schema.sql")


def init_db(db_path: str = DB_PATH, schema_path: str = SCHEMA_PATH) -> str:
    """Run schema.sql against db_path. Idempotent (CREATE ... IF NOT EXISTS)."""
    with open(schema_path) as f:
        schema_sql = f.read()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(schema_sql)
        conn.commit()
    finally:
        conn.close()

    return db_path


def show(db_path: str = DB_PATH) -> None:
    """Print the tables (with row counts) and the seeded departments."""
    conn = sqlite3.connect(db_path)
    try:
        print(f"── {db_path} ──\n")

        print("TABLES (row count):")
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"
        ):
            n = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            print(f"  • {name} ({n})")

        print("\nSEEDED TAXONOMY (department → category → subcategories):")
        for dep_id, dep in conn.execute(
            "SELECT id, name FROM departments ORDER BY id;"
        ).fetchall():
            print(f"  {dep}")
            for cat_id, cat in conn.execute(
                "SELECT id, name FROM categories WHERE department_id=? ORDER BY name;",
                (dep_id,),
            ).fetchall():
                subs = [
                    s for (s,) in conn.execute(
                        "SELECT name FROM subcategories WHERE category_id=? ORDER BY name;",
                        (cat_id,),
                    )
                ]
                attrs = [
                    a for (a,) in conn.execute(
                        "SELECT name FROM category_attributes WHERE category_id=? ORDER BY name;",
                        (cat_id,),
                    )
                ]
                line = f"    • {cat}"
                if subs:
                    line += f"  → {', '.join(subs)}"
                if attrs:
                    line += f"   [attrs: {', '.join(attrs)}]"
                print(line)
    finally:
        conn.close()


if __name__ == "__main__":
    path = init_db()
    print(f"Initialized {path}")
    if "--show" in sys.argv:
        print()
        show(path)
