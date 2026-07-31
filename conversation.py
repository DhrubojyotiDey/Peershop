#!/usr/bin/env python3
"""
PeerShop — Phase G: daily conversational window + turn-cap logic.

The Telegram bot gives each seller a bounded DAILY conversational allowance for
natural-language chat. This module owns ONLY the window/turn bookkeeping — no LLM,
no Telegram. The bot (built next) layers Gemini intent-parsing and the slash-
command fallback on top of these primitives.

The model (all configurable):
  * A seller gets ONE window per reset period (default: daily, expiring at the
    next LOCAL midnight — set via `window_end_sql`).
  * A window permits up to `turns_cap` LLM turns (default 15) and lives until
    `window_end`.
  * "Active" window  = unexpired AND turns_used < turns_cap  → use conversational path.
  * No active window:
      - an unexpired-but-capped window exists → allowance USED for the period
        ("exhausted") → bot must fall back to slash commands.
      - no unexpired window at all (none yet this period, or the last expired)
        → open a FRESH window.

Conversation messages are EPHEMERAL, scoped to one window (never global/cross-
window/cross-seller), and safe to prune once the window expires.

Everything is measured in the DB's LOCAL time so "midnight" matches the seller.

CLI:
    python3 conversation.py --status <seller_id>
"""

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "peershop.db")

DEFAULT_TURNS_CAP = 15
# Default expiry: the next LOCAL midnight → "resets daily at midnight".
NEXT_MIDNIGHT_SQL = "datetime(date('now', 'localtime'), '+1 day')"
_NOW_SQL = "datetime('now', 'localtime')"

_COLS = "id, seller_id, window_start, window_end, turns_used, turns_cap, pending_action"


def _win(row) -> dict | None:
    if row is None:
        return None
    return {"id": row[0], "seller_id": row[1], "window_start": row[2],
            "window_end": row[3], "turns_used": row[4], "turns_cap": row[5],
            "pending_action": row[6]}


def _active(conn, seller_id: int):
    """Most recent unexpired, uncapped window row (or None)."""
    return conn.execute(
        f"SELECT {_COLS} FROM conversation_windows "
        f"WHERE seller_id = ? AND window_end > {_NOW_SQL} AND turns_used < turns_cap "
        "ORDER BY id DESC LIMIT 1", (seller_id,)).fetchone()


def _has_unexpired(conn, seller_id: int) -> bool:
    """Any window still within its period (capped or not) blocks a new grant."""
    return conn.execute(
        f"SELECT 1 FROM conversation_windows "
        f"WHERE seller_id = ? AND window_end > {_NOW_SQL} LIMIT 1",
        (seller_id,)).fetchone() is not None


def get_active_window(seller_id: int, db_path: str = DB_PATH) -> dict | None:
    conn = sqlite3.connect(db_path)
    try:
        return _win(_active(conn, seller_id))
    finally:
        conn.close()


def get_or_open_window(seller_id: int, turns_cap: int = DEFAULT_TURNS_CAP,
                       window_end_sql: str = NEXT_MIDNIGHT_SQL,
                       db_path: str = DB_PATH) -> dict:
    """
    Resolve this seller's conversational state for an incoming message.

    Returns {"window": <dict>|None, "opened": bool, "reason": str}:
      * reason="active"    → reuse an in-progress window (opened=False).
      * reason="opened"    → a fresh window was granted (opened=True).
      * reason="exhausted" → allowance used for this period; window=None,
                             opened=False → caller falls back to slash commands.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        active = _active(conn, seller_id)
        if active is not None:
            return {"window": _win(active), "opened": False, "reason": "active"}
        if _has_unexpired(conn, seller_id):
            # Unexpired but capped → today's allowance is spent.
            return {"window": None, "opened": False, "reason": "exhausted"}
        pid = conn.execute(
            "INSERT INTO conversation_windows (seller_id, window_end, turns_cap) "
            f"VALUES (?, {window_end_sql}, ?)", (seller_id, turns_cap)).lastrowid
        conn.commit()
        row = conn.execute(f"SELECT {_COLS} FROM conversation_windows WHERE id = ?",
                           (pid,)).fetchone()
        return {"window": _win(row), "opened": True, "reason": "opened"}
    finally:
        conn.close()


def set_pending_action(window_id: int, action: str | None,
                       db_path: str = DB_PATH) -> None:
    """
    Store (or clear with None) a JSON string describing a destructive action that
    is awaiting the seller's explicit yes/no. One slot per window; cleared once
    the confirmation is resolved.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE conversation_windows SET pending_action = ? WHERE id = ?",
            (action, window_id))
        conn.commit()
    finally:
        conn.close()


def record_message(window_id: int, role: str, content: str,
                   db_path: str = DB_PATH) -> None:
    """Append one ephemeral message ('seller' | 'bot') to a window."""
    if role not in ("seller", "bot"):
        raise ValueError(f"bad role: {role!r}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        conn.execute(
            "INSERT INTO conversation_messages (window_id, role, content) "
            "VALUES (?, ?, ?)", (window_id, role, content))
        conn.commit()
    finally:
        conn.close()


def use_turn(window_id: int, db_path: str = DB_PATH) -> int:
    """Consume one turn (increment turns_used). Returns the new turns_used."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE conversation_windows SET turns_used = turns_used + 1 WHERE id = ?",
            (window_id,))
        conn.commit()
        return conn.execute(
            "SELECT turns_used FROM conversation_windows WHERE id = ?",
            (window_id,)).fetchone()[0]
    finally:
        conn.close()


def record_turn(window_id: int, seller_text: str, bot_text: str,
                db_path: str = DB_PATH) -> int:
    """Convenience: record the seller message + bot reply and consume one turn."""
    record_message(window_id, "seller", seller_text, db_path)
    record_message(window_id, "bot", bot_text, db_path)
    return use_turn(window_id, db_path)


def window_history(window_id: int, limit: int | None = None,
                   db_path: str = DB_PATH) -> list[dict]:
    """
    This window's messages in order, for the LLM's bounded multi-turn context.
    Scoped to ONE window only — never global/cross-window/cross-seller. `limit`
    keeps the most recent N (still in chronological order).
    """
    conn = sqlite3.connect(db_path)
    try:
        if limit:
            rows = conn.execute(
                "SELECT role, content FROM conversation_messages "
                "WHERE window_id = ? ORDER BY id DESC LIMIT ?",
                (window_id, limit)).fetchall()
            rows = rows[::-1]
        else:
            rows = conn.execute(
                "SELECT role, content FROM conversation_messages "
                "WHERE window_id = ? ORDER BY id", (window_id,)).fetchall()
        return [{"role": r[0], "content": r[1]} for r in rows]
    finally:
        conn.close()


def prune_expired_messages(db_path: str = DB_PATH) -> int:
    """
    Delete ephemeral messages belonging to expired windows (window rows are kept
    as lightweight allowance records). Returns the number of messages removed.
    Safe to run on any schedule.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        cur = conn.execute(
            "DELETE FROM conversation_messages WHERE window_id IN "
            f"(SELECT id FROM conversation_windows WHERE window_end <= {_NOW_SQL})")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--status":
        sid = int(sys.argv[2])
        active = get_active_window(sid)
        print(f"seller {sid}: active window = {active}")
        if active is None:
            print("  → next message would:",
                  "open a fresh window" if not _has_unexpired(
                      sqlite3.connect(DB_PATH), sid) else "hit 'exhausted' (allowance used)")
    else:
        print("Usage: python3 conversation.py --status <seller_id>")
        raise SystemExit(1)
