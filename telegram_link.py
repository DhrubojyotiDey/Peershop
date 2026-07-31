#!/usr/bin/env python3
"""
PeerShop — Phase I: Telegram ↔ seller identity binding (stdlib only).

The security layer for the seller Telegram control bot. ONE shared bot serves
every seller; this module decides WHICH seller (if any) an incoming Telegram
user is, so the bot can only ever act on that seller's own store.

Root of trust is the existing web login:
  * A one-time link CODE is minted only inside an authenticated dashboard session
    (create_link_code). To link a Telegram account to seller X you must already be
    able to log into seller X's web account.
  * The seller sends `/start <code>` to the bot; redeem_link_code binds the
    Telegram-authenticated sender id (message.from.id) → seller_id. Codes are
    single-use and short-lived.
  * resolve_seller() is the per-message lookup; it NEVER trusts anything in the
    message text — only the stored binding.

Tables (see schema.sql): seller_telegram (verified binding), telegram_link_codes.
"""

import os
import secrets
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "peershop.db")

DEFAULT_TTL_MINUTES = 10


def create_link_code(seller_id: int, ttl_minutes: int = DEFAULT_TTL_MINUTES,
                     db_path: str = DB_PATH) -> str:
    """Mint a one-time link code for a seller (caller must be an authed session)."""
    code = secrets.token_urlsafe(8)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        conn.execute(
            "INSERT INTO telegram_link_codes (code, seller_id, expires_at) "
            "VALUES (?, ?, datetime('now', ?))",
            (code, seller_id, f"+{int(ttl_minutes)} minutes"))
        conn.commit()
        return code
    finally:
        conn.close()


def redeem_link_code(code: str, telegram_user_id: int,
                     db_path: str = DB_PATH) -> int | None:
    """
    Redeem a code for a Telegram user: if valid (exists, unused, unexpired), bind
    telegram_user_id → seller_id and mark the code used. Returns the seller_id, or
    None if the code is invalid/expired/used. This is the ONLY path that creates a
    binding. Re-linking (same Telegram id → new seller) is allowed via upsert.
    """
    code = (code or "").strip()
    if not code:
        return None
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        row = conn.execute(
            "SELECT seller_id FROM telegram_link_codes "
            "WHERE code = ? AND used_at IS NULL AND expires_at > datetime('now')",
            (code,)).fetchone()
        if row is None:
            return None
        seller_id = row[0]
        conn.execute(
            "INSERT INTO seller_telegram (telegram_user_id, seller_id) VALUES (?, ?) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET seller_id = excluded.seller_id, "
            "linked_at = datetime('now')",
            (telegram_user_id, seller_id))
        conn.execute(
            "UPDATE telegram_link_codes SET used_at = datetime('now') WHERE code = ?",
            (code,))
        conn.commit()
        return seller_id
    finally:
        conn.close()


def resolve_seller(telegram_user_id: int, db_path: str = DB_PATH) -> int | None:
    """The per-message security lookup: bound seller_id for this Telegram user, or None."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT seller_id FROM seller_telegram WHERE telegram_user_id = ?",
            (telegram_user_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_telegram_for_seller(seller_id: int, db_path: str = DB_PATH) -> int | None:
    """The Telegram user id currently linked to a seller (for dashboard state), or None."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT telegram_user_id FROM seller_telegram WHERE seller_id = ? "
            "ORDER BY linked_at DESC LIMIT 1", (seller_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def unlink(seller_id: int, db_path: str = DB_PATH) -> None:
    """Remove all Telegram bindings for a seller (dashboard 'Unlink')."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM seller_telegram WHERE seller_id = ?", (seller_id,))
        conn.commit()
    finally:
        conn.close()


def seller_display(seller_id: int, db_path: str = DB_PATH) -> str:
    """Shop name for confirmation messages."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT name FROM sellers WHERE id = ?",
                          (seller_id,)).fetchone()
        return row[0] if row else f"seller #{seller_id}"
    finally:
        conn.close()
