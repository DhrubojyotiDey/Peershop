#!/usr/bin/env python3
"""
PeerShop — admin account-removal (one-click, destructive, admin-only).

Purpose: lets an admin fully wipe a seller account from the database in one
action. Built primarily so a SINGLE Telegram account can be reused to test
the self-service onboarding flow repeatedly (sign up, test, wipe, sign up
again) without needing multiple real Telegram accounts -- see the
self-service bot onboarding plan
(.hermes/plans/2026-10-10_000000-peershop-self-service-bot-onboarding.md).

Scope of a wipe:
  * The `sellers` row itself, and every table with
    `seller_id ... REFERENCES sellers(id) ON DELETE CASCADE` in schema.sql
    (credentials, sessions, Telegram link, products + their photos/chat logs/
    nudges, departments, attribute history, conversation windows, raw ingest
    staging, etc.) -- a single `DELETE FROM sellers` cascades all of this
    automatically, PROVIDED `PRAGMA foreign_keys = ON` is set on the
    connection (SQLite does not enforce FKs by default).
  * bot_onboarding_sessions -- NOT a seller_id-keyed table (it's keyed by
    telegram_user_id, since onboarding happens BEFORE a seller exists), so it
    cannot cascade via a FK. Explicitly cleared here for every telegram_user_id
    that was bound to this seller, so that re-onboarding the same Telegram
    account afterward starts from a genuinely clean slate.
  * Media files on disk referenced by the deleted rows (product photos,
    seller photos, raw-ingest staged photos) -- DB deletion alone leaves
    orphaned files; best-effort unlink after the DB transaction succeeds.

This is irreversible and has no undo. It is NOT exposed to sellers themselves
-- only wired into the admin dashboard (review.py), behind the existing
admin-password gate.
"""
from __future__ import annotations

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "peershop.db")


def _collect_media_paths(conn: sqlite3.Connection, seller_id: int) -> list[str]:
    """Every on-disk file path tied to this seller, gathered BEFORE the cascade
    delete removes the rows that reference them."""
    paths: list[str] = []

    for row in conn.execute(
            "SELECT image_path FROM products WHERE seller_id = ? AND image_path IS NOT NULL",
            (seller_id,)):
        paths.append(row[0])

    for row in conn.execute(
            "SELECT pp.image_path FROM product_photos pp "
            "JOIN products p ON p.id = pp.product_id WHERE p.seller_id = ?",
            (seller_id,)):
        paths.append(row[0])

    for row in conn.execute(
            "SELECT image_path FROM seller_photos WHERE seller_id = ?", (seller_id,)):
        paths.append(row[0])

    for row in conn.execute(
            "SELECT media_path FROM bot_raw_ingest WHERE seller_id = ? AND media_path IS NOT NULL",
            (seller_id,)):
        paths.append(row[0])

    return paths


def delete_seller(seller_id: int, db_path: str = DB_PATH) -> dict:
    """
    Irreversibly wipe one seller account and everything that belongs to it.
    Returns {"ok": True, "name": <shop name>, "media_removed": N} on success,
    or {"ok": False, "message": "..."} if the seller doesn't exist (a clean
    no-op, not an exception -- callers like the admin HTTP handler shouldn't
    need a try/except for the common "already gone" case).
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        row = conn.execute("SELECT name FROM sellers WHERE id = ?", (seller_id,)).fetchone()
        if row is None:
            return {"ok": False, "message": f"no seller with id {seller_id}"}
        name = row[0]

        telegram_ids = [r[0] for r in conn.execute(
            "SELECT telegram_user_id FROM seller_telegram WHERE seller_id = ?", (seller_id,))]
        media_paths = _collect_media_paths(conn, seller_id)

        # The cascade: one DELETE, every seller_id-FK'd table follows (see
        # schema.sql -- every such FK is declared ON DELETE CASCADE).
        conn.execute("DELETE FROM sellers WHERE id = ?", (seller_id,))

        # Not FK-linked to sellers (keyed by telegram_user_id instead) -- clear
        # explicitly so re-onboarding the same Telegram account starts fresh.
        for tid in telegram_ids:
            conn.execute("DELETE FROM bot_onboarding_sessions WHERE telegram_user_id = ?", (tid,))

        conn.commit()
    finally:
        conn.close()

    media_removed = 0
    for path in media_paths:
        try:
            os.unlink(path)
            media_removed += 1
        except OSError:
            pass  # already gone, or never really existed on disk -- not fatal

    return {"ok": True, "name": name, "media_removed": media_removed}
