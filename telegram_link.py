#!/usr/bin/env python3
"""
PeerShop — Phase I: Telegram ↔ seller identity binding (stdlib only).

The security layer for the seller Telegram control bot. ONE shared bot serves
every seller; this module decides WHICH seller (if any) an incoming Telegram
user is, so the bot can only ever act on that seller's own store.

Two ways a Telegram user becomes bound to a seller:
  * LINKING an existing account: root of trust is the existing web login. A
    one-time link CODE is minted only inside an authenticated dashboard session
    (create_link_code). To link a Telegram account to seller X you must already
    be able to log into seller X's web account. The seller sends `/start <code>`
    to the bot; redeem_link_code binds the Telegram-authenticated sender id
    (message.from.id) → seller_id. Codes are single-use and short-lived.
  * SELF-SERVICE new shop creation (no pre-existing account): create_shop /
    create_shop_with_login. Used when the bot's conversational onboarding flow
    (bot.py's _handle_onboarding) finishes collecting a brand-new user's shop
    details. Always creates seller_type='bot_seller'. There is no "root of
    trust" requirement here by design — anyone can message the bot and get a
    shop; the human-admin pending→live review gate (review.py) is the
    abuse/spam backstop, not signup friction.

resolve_seller() is the per-message lookup either way; it NEVER trusts anything
in the message text — only the stored binding.

Tables (see schema.sql): seller_telegram (verified binding), telegram_link_codes
(link-existing-account flow only — create_shop bypasses codes entirely).
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


def create_shop(telegram_user_id: int, name: str, contact: str,
                 pincode: str | None = None, owner_name: str | None = None,
                 db_path: str = DB_PATH) -> int:
    """
    Self-service shop creation FROM the bot -- the only path that creates a NEW
    seller row without requiring a pre-existing web login (create_link_code's
    root of trust is circular for a brand-new signup: you can't log in to an
    account that doesn't exist yet). Always seller_type='bot_seller' -- this is
    how the rest of the system (admin dashboards, reports) distinguishes
    bot-onboarded shops from manually-admin-created ones.

    Binds the CREATING Telegram user to the new seller immediately, same upsert
    semantics as redeem_link_code -- if this telegram_user_id was previously
    bound to a different seller, it is rebound to this new one. Calling this
    again always creates ANOTHER new shop; it is not an idempotent "get or
    create" (re-linking an EXISTING shop still goes through redeem_link_code).
    """
    name = (name or "").strip()
    contact = (contact or "").strip()
    if not name:
        raise ValueError("shop name is required")
    if not contact:
        raise ValueError("contact is required")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        cur = conn.execute(
            "INSERT INTO sellers (name, owner_name, contact, pincode, seller_type) "
            "VALUES (?, ?, ?, ?, 'bot_seller')",
            (name, owner_name, contact, pincode))
        seller_id = cur.lastrowid
        conn.execute(
            "INSERT INTO seller_telegram (telegram_user_id, seller_id) VALUES (?, ?) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET seller_id = excluded.seller_id, "
            "linked_at = datetime('now')",
            (telegram_user_id, seller_id))
        conn.commit()
        return seller_id
    finally:
        conn.close()


def create_shop_with_login(telegram_user_id: int, name: str, contact: str,
                            pincode: str | None = None, owner_name: str | None = None,
                            db_path: str = DB_PATH) -> tuple[int, str, str]:
    """
    create_shop() plus an auto-generated web dashboard login, so a bot-onboarded
    seller can ALSO use the existing web dashboard (review their own listings,
    change price, etc.) without the bot needing to reimplement any of that.
    The password is returned ONCE for the bot to send in a DM -- auth.py hashes
    it immediately via set_credentials; nothing here stores it in plaintext.
    """
    import auth
    seller_id = create_shop(telegram_user_id, name, contact, pincode, owner_name, db_path)
    login = f"shop{seller_id}"
    password = secrets.token_urlsafe(9)
    auth.set_credentials(seller_id, login, password, actor="seller", db_path=db_path)
    return seller_id, login, password


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
