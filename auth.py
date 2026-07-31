#!/usr/bin/env python3
"""
PeerShop — Phase H: seller web authentication (stdlib only).

Password storage + server-side sessions for the seller website login/dashboard.
No third-party crypto: PBKDF2-HMAC-SHA256 via hashlib, random via secrets,
constant-time compare via hmac.

  set_credentials()      create/update a seller's login + hashed password
  verify_login()         check login+password -> seller_id | None
  create_session()       issue a session (token + CSRF), 7-day expiry
  validate_session()     token -> (seller_id, csrf) | None  (rejects expired)
  revoke_session()       logout
  prune_expired_sessions()

The web transport (serve.py) is plain HTTP on the LAN — passwords travel in
cleartext, so this is a LAN/dev posture, NOT production. Hashing still protects
the stored passwords at rest.
"""

import hashlib
import hmac
import os
import secrets
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "peershop.db")

_ITERATIONS = 200_000
_MIN_LOGIN = 3
_MIN_PASSWORD = 8
SESSION_TTL_DAYS = 7


class AuthError(Exception):
    """Signup/login validation failure (safe to show the user)."""


# ── password hashing ─────────────────────────────────────────────────────────

def _pbkdf2(password: str, salt_hex: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt_hex), _ITERATIONS)
    return dk.hex()


def _norm_login(login: str) -> str:
    return (login or "").strip().lower()


def validate_credentials(login: str, password: str) -> None:
    """Raise AuthError if the login/password don't meet basic rules."""
    if len(_norm_login(login)) < _MIN_LOGIN:
        raise AuthError(f"login must be at least {_MIN_LOGIN} characters")
    if " " in _norm_login(login):
        raise AuthError("login cannot contain spaces")
    if len(password or "") < _MIN_PASSWORD:
        raise AuthError(f"password must be at least {_MIN_PASSWORD} characters")


def is_login_available(login: str, db_path: str = DB_PATH) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT 1 FROM seller_credentials WHERE login = ?",
                            (_norm_login(login),)).fetchone() is None
    finally:
        conn.close()


def set_credentials(seller_id: int, login: str, password: str,
                    actor: str = "seller", db_path: str = DB_PATH) -> None:
    """
    Create/replace a seller's web credentials. Raises AuthError on bad input.

    Every set is audited in credential_events ('created' the first time, 'changed'
    thereafter) with `actor` (e.g. 'seller' at signup / change, 'admin' on a reset)
    so the admin Users page can show password-change history. The password itself is
    only ever stored hashed. The audit write is best-effort — it never blocks the
    credential change (or fails if the audit table is absent on an old DB).
    """
    validate_credentials(login, password)
    login = _norm_login(login)
    salt_hex = secrets.token_hex(16)
    pw_hash = _pbkdf2(password, salt_hex)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        if conn.execute("SELECT 1 FROM sellers WHERE id = ?",
                        (seller_id,)).fetchone() is None:
            raise AuthError(f"no seller with id {seller_id}")
        taken = conn.execute(
            "SELECT 1 FROM seller_credentials WHERE login = ? AND seller_id <> ?",
            (login, seller_id)).fetchone()
        if taken:
            raise AuthError(f"login '{login}' is already taken")
        existed = conn.execute("SELECT 1 FROM seller_credentials WHERE seller_id = ?",
                               (seller_id,)).fetchone() is not None
        conn.execute(
            "INSERT INTO seller_credentials (seller_id, login, pw_hash, pw_salt) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(seller_id) DO UPDATE SET login=excluded.login, "
            "pw_hash=excluded.pw_hash, pw_salt=excluded.pw_salt",
            (seller_id, login, pw_hash, salt_hex))
        try:
            conn.execute(
                "INSERT INTO credential_events (seller_id, event, actor) VALUES (?, ?, ?)",
                (seller_id, "changed" if existed else "created", actor))
        except sqlite3.OperationalError:
            pass                         # audit table missing (old DB) → don't block
        conn.commit()
    finally:
        conn.close()


def get_login(seller_id: int, db_path: str = DB_PATH) -> str | None:
    """The seller's current login username (needed to reset a password in place)."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT login FROM seller_credentials WHERE seller_id = ?",
                           (seller_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def credential_history(seller_id: int, db_path: str = DB_PATH) -> list[dict]:
    """This seller's password create/change events, newest first."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT event, actor, created_at FROM credential_events "
            "WHERE seller_id = ? ORDER BY created_at DESC, id DESC",
            (seller_id,)).fetchall()
        return [{"event": r[0], "actor": r[1], "at": r[2]} for r in rows]
    finally:
        conn.close()


def all_credential_events(limit: int = 100, db_path: str = DB_PATH) -> list[dict]:
    """Password-change activity across ALL sellers (admin audit log), newest first."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ce.seller_id, s.name, ce.event, ce.actor, ce.created_at "
            "FROM credential_events ce JOIN sellers s ON s.id = ce.seller_id "
            "ORDER BY ce.created_at DESC, ce.id DESC LIMIT ?", (limit,)).fetchall()
        return [{"seller_id": r[0], "seller": r[1], "event": r[2],
                 "actor": r[3], "at": r[4]} for r in rows]
    finally:
        conn.close()


def verify_login(login: str, password: str, db_path: str = DB_PATH) -> int | None:
    """Return the seller_id on a correct login+password, else None."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT seller_id, pw_hash, pw_salt FROM seller_credentials WHERE login = ?",
            (_norm_login(login),)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    seller_id, pw_hash, pw_salt = row
    calc = _pbkdf2(password or "", pw_salt)
    return seller_id if hmac.compare_digest(calc, pw_hash) else None


# ── sessions ─────────────────────────────────────────────────────────────────

def create_session(seller_id: int, db_path: str = DB_PATH,
                   ttl_days: int = SESSION_TTL_DAYS) -> tuple[str, str]:
    """Issue a new session. Returns (token, csrf_token)."""
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(16)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        conn.execute(
            "INSERT INTO seller_sessions (token, seller_id, csrf, expires_at) "
            "VALUES (?, ?, ?, datetime('now', ?))",
            (token, seller_id, csrf, f"+{int(ttl_days)} days"))
        conn.commit()
    finally:
        conn.close()
    return token, csrf


def validate_session(token: str, db_path: str = DB_PATH) -> tuple[int, str] | None:
    """Return (seller_id, csrf) for a live (unexpired) session, else None."""
    if not token:
        return None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT seller_id, csrf FROM seller_sessions "
            "WHERE token = ? AND expires_at > datetime('now')", (token,)).fetchone()
    finally:
        conn.close()
    return (row[0], row[1]) if row else None


def revoke_session(token: str, db_path: str = DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM seller_sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


def prune_expired_sessions(db_path: str = DB_PATH) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM seller_sessions WHERE expires_at <= datetime('now')")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
