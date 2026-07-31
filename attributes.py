#!/usr/bin/env python3
"""
PeerShop — Phase D: per-category product attributes + zero-AI seller prefill.

Two lightweight, stdlib-only concerns (no Gemini, no Groq, no cost):

  * category_attributes declares which attribute NAMES a category offers (e.g.
    stationery/paper products -> 'ruling'; boutique/kurtis -> 'size','color').
    Values are FREEFORM text (decision 2) — sanitized, never governed/queued.

  * seller_attribute_history (decision 3) is a STRUCTURED per-seller record of
    values previously used. When a seller lists another product in the same
    category, prefill_attributes() suggests their own most-frequently-used value
    for each attribute via a plain SQL lookup — NOT an AI feature, NOT a growing
    freeform memory file. Rows are wiped on account close (ON DELETE CASCADE).

Storage: a product's chosen values live as a small JSON object in
products.attributes, keyed by the category's declared attribute names.
"""

import json
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "peershop.db")

_MAX_VALUE_LEN = 100          # freeform, but bounded — basic input sanitization


def _sanitize(value) -> str:
    """Collapse whitespace and cap length. The only check on freeform values."""
    return " ".join(str(value).split())[:_MAX_VALUE_LEN]


def get_category_attributes(category_id: int, db_path: str = DB_PATH) -> list[dict]:
    """The attribute NAMES a category offers, with any (non-enforced) suggestions."""
    conn = sqlite3.connect(db_path)
    try:
        out = []
        for name, sv in conn.execute(
                "SELECT name, suggested_values FROM category_attributes "
                "WHERE category_id = ? ORDER BY name;", (category_id,)):
            try:
                suggested = json.loads(sv) if sv else []
            except (json.JSONDecodeError, TypeError):
                suggested = []
            out.append({"name": name, "suggested_values": suggested})
        return out
    finally:
        conn.close()


def prefill_attributes(seller_id: int, category_id: int,
                       db_path: str = DB_PATH) -> dict:
    """
    {attribute_name: this seller's most-used value} for the category's declared
    attributes — a plain SQL lookup, zero AI. Omits attributes the seller has no
    history for. This is a form default; the seller can always change it.
    """
    conn = sqlite3.connect(db_path)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM category_attributes WHERE category_id = ?;",
            (category_id,))]
        out = {}
        for name in names:
            row = conn.execute(
                "SELECT attribute_value FROM seller_attribute_history "
                "WHERE seller_id = ? AND category_id = ? AND attribute_name = ? "
                "ORDER BY times_used DESC, last_used_at DESC LIMIT 1;",
                (seller_id, category_id, name)).fetchone()
            if row:
                out[name] = row[0]
        return out
    finally:
        conn.close()


def _record_history(conn, seller_id: int, category_id: int, values: dict) -> None:
    """Upsert each (seller, category, name, value): bump times_used or insert."""
    for name, value in values.items():
        cur = conn.execute(
            "UPDATE seller_attribute_history "
            "SET times_used = times_used + 1, last_used_at = datetime('now') "
            "WHERE seller_id = ? AND category_id = ? AND attribute_name = ? "
            "AND attribute_value = ?",
            (seller_id, category_id, name, value))
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO seller_attribute_history "
                "(seller_id, category_id, attribute_name, attribute_value) "
                "VALUES (?, ?, ?, ?)",
                (seller_id, category_id, name, value))


def set_product_attributes(product_id: int, values: dict,
                           record_history: bool = True,
                           db_path: str = DB_PATH) -> dict:
    """
    Store a product's freeform attribute values (JSON in products.attributes) and,
    by default, record them in the seller's attribute history for future prefill.

    Only keys declared for the product's category are kept (the form is generated
    from those); blank values are dropped; kept values are sanitized. Requires the
    product to have a category. Returns the stored {name: value} dict.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        prod = conn.execute(
            "SELECT seller_id, category_id FROM products WHERE id = ?",
            (product_id,)).fetchone()
        if prod is None:
            raise ValueError(f"no product with id={product_id}")
        seller_id, category_id = prod
        if category_id is None:
            raise ValueError("set a category before attributes")

        declared = {r[0] for r in conn.execute(
            "SELECT name FROM category_attributes WHERE category_id = ?",
            (category_id,))}
        clean = {}
        for key, val in (values or {}).items():
            if key not in declared or val is None or not str(val).strip():
                continue
            clean[key] = _sanitize(val)

        conn.execute("UPDATE products SET attributes = ? WHERE id = ?",
                     (json.dumps(clean), product_id))
        if record_history and clean:
            _record_history(conn, seller_id, category_id, clean)
        conn.commit()
        return clean
    finally:
        conn.close()


def get_product_attributes(product_id: int, db_path: str = DB_PATH) -> dict:
    """The stored attribute values for a product (parsed from JSON)."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT attributes FROM products WHERE id = ?",
                          (product_id,)).fetchone()
        if row is None:
            raise ValueError(f"no product with id={product_id}")
        try:
            return json.loads(row[0]) if row[0] else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    finally:
        conn.close()
