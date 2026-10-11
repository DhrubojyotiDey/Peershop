#!/usr/bin/env python3
"""
PeerShop — debounce worker (Task 5) + bundle→product wiring (Task 6).

Watches bot_raw_ingest for forwarded-burst rows staged by telegram_bot.py's
_handle_forwarded_message (Task 4). Groups each seller's 'unprocessed' rows
into one bundle once that seller's burst goes quiet (no new row for
QUIET_WINDOW_SECONDS -- a true quiet-window debounce, not a fixed timer from
the first message), then turns the finalized bundle into ONE pending product
via intake.py's AI-draft pipeline (ingest_photo_noninteractive) and
seller_actions.post_product() underneath it -- status='pending' ALWAYS; the
existing human-admin review gate (review.py) is completely unchanged.

v1 scope (per the self-service bot onboarding plan): ONE bundle = ONE product.
The bundle's FIRST photo (by row id) is the primary image; all raw_text rows
are concatenated and fed to the AI description step as a caption hint (richer
multi-product-per-burst splitting is a documented fast-follow, not v1).

A bundle with NO photo at all cannot become a product (products.image_path is
NOT NULL) -- it's marked 'needs_photo', never silently dropped and never
force-posted with a placeholder image. A bundle whose seller has no department
assigned yet (onboarding doesn't collect one) is marked 'needs_department' --
also never silently dropped.

Runs as its own small supervised loop, separate from telegram_bot.py's own
poll loop, so a slow/stuck Gemini call here can never block message delivery.

CLI:
    ./.venv/bin/python debounce_worker.py --once     # one scan+bundle+process tick
    ./.venv/bin/python debounce_worker.py             # loop forever (TICK_SECONDS)
"""

import os
import sqlite3
import sys
import time
import uuid

import intake
import seller_actions
import telegram_api
import telegram_link

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "peershop.db")

QUIET_WINDOW_SECONDS = int(os.environ.get("PEERSHOP_DEBOUNCE_QUIET_WINDOW", "30"))
TICK_SECONDS = int(os.environ.get("PEERSHOP_DEBOUNCE_TICK", "10"))

# Optional: a Telegram user id to push-notify whenever a bundle auto-lists a
# new 'pending' product. Best-effort only -- the product is already safely
# 'pending' in the DB regardless of whether this notification succeeds, is
# configured, or even exists; review.py's queue is the source of truth.
ADMIN_TELEGRAM_ID = os.environ.get("PEERSHOP_ADMIN_TELEGRAM_ID")


# ── Task 5: quiet-window bundling ───────────────────────────────────────────

def scan_and_bundle(db_path: str = DB_PATH,
                     quiet_window: int = QUIET_WINDOW_SECONDS) -> dict:
    """
    For each seller with 'unprocessed' bot_raw_ingest rows, bundle them (shared
    bundle_id, status -> 'bundled') iff that seller's MOST RECENT unprocessed
    row is already >= quiet_window seconds old (nothing new has arrived since
    -- the burst is presumed finished). Returns {'bundled': N}, N = number of
    NEW bundles created this tick (not number of rows).
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        sellers = conn.execute(
            "SELECT DISTINCT seller_id FROM bot_raw_ingest "
            "WHERE status = 'unprocessed'").fetchall()
        bundled = 0
        for (seller_id,) in sellers:
            age_row = conn.execute(
                "SELECT strftime('%s','now') - strftime('%s', MAX(created_at)) "
                "FROM bot_raw_ingest WHERE seller_id = ? AND status = 'unprocessed'",
                (seller_id,)).fetchone()
            age = age_row[0] if age_row else None
            if age is None or age < quiet_window:
                continue   # still receiving this seller's burst
            bundle_id = uuid.uuid4().hex
            conn.execute(
                "UPDATE bot_raw_ingest SET status = 'bundled', bundle_id = ? "
                "WHERE seller_id = ? AND status = 'unprocessed'",
                (bundle_id, seller_id))
            bundled += 1
        conn.commit()
        return {"bundled": bundled}
    finally:
        conn.close()


# ── Task 6: bundle → AI draft → pending product ─────────────────────────────

def _mark_rows(row_ids: list, status: str, db_path: str) -> None:
    if not row_ids:
        return
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany("UPDATE bot_raw_ingest SET status = ? WHERE id = ?",
                          [(status, rid) for rid in row_ids])
        conn.commit()
    finally:
        conn.close()


def _notify_admin_new_pending(product: dict, seller_id: int, db_path: str) -> None:
    """Best-effort push notification. Never raises -- telegram_api.send_message
    already swallows its own failures; a missing/unset ADMIN_TELEGRAM_ID is a
    normal, silent no-op (the product is still safely 'pending' either way)."""
    if not ADMIN_TELEGRAM_ID:
        return
    text = (f"\U0001F195 New auto-listed product pending review\n"
            f"Seller: {telegram_link.seller_display(seller_id, db_path=db_path)}\n"
            f"\"{product.get('title')}\"\n"
            f"Open the admin review queue to approve or reject.")
    telegram_api.send_message(ADMIN_TELEGRAM_ID, text)


def process_bundle(bundle_id: str, db_path: str = DB_PATH) -> dict:
    """
    Turn one finalized ('bundled') bundle into a pending product, or flag it
    if it genuinely can't become one yet. Never raises for expected business
    conditions (no photo / no department) -- only truly unexpected pipeline
    errors (AI call failure etc.) land as 'failed', also never silently lost.
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, seller_id, media_path, raw_text FROM bot_raw_ingest "
            "WHERE bundle_id = ? ORDER BY id", (bundle_id,)).fetchall()
    finally:
        conn.close()

    if not rows:
        return {"bundle_id": bundle_id, "result": "empty"}

    seller_id = rows[0][1]
    row_ids = [r[0] for r in rows]
    photo_path = next((r[2] for r in rows if r[2]), None)
    texts = [r[3] for r in rows if r[3]]
    hint = "\n".join(texts) if texts else None

    if not photo_path:
        _mark_rows(row_ids, "needs_photo", db_path)
        return {"bundle_id": bundle_id, "result": "needs_photo", "seller_id": seller_id}

    try:
        product = intake.ingest_photo_noninteractive(
            seller_id, photo_path, raw_text_hint=hint, db_path=db_path)
    except seller_actions.SellerActionError as e:
        if "department" in str(e).lower():
            _mark_rows(row_ids, "needs_department", db_path)
            return {"bundle_id": bundle_id, "result": "needs_department",
                     "seller_id": seller_id, "error": str(e)}
        _mark_rows(row_ids, "failed", db_path)
        return {"bundle_id": bundle_id, "result": "failed",
                 "seller_id": seller_id, "error": str(e)}
    except Exception as e:
        _mark_rows(row_ids, "failed", db_path)
        print(f"bundle {bundle_id} (seller {seller_id}) failed: {e}", file=sys.stderr)
        return {"bundle_id": bundle_id, "result": "failed",
                 "seller_id": seller_id, "error": str(e)}

    _mark_rows(row_ids, "processed", db_path)
    _notify_admin_new_pending(product, seller_id, db_path)
    return {"bundle_id": bundle_id, "result": "processed", "seller_id": seller_id,
            "product_id": product["product_id"]}


def tick(db_path: str = DB_PATH) -> dict:
    """One full cycle: bundle any newly-quiet bursts, then process every
    currently-'bundled' bundle (including ones left over from a prior tick,
    e.g. if the process died mid-processing)."""
    bundle_result = scan_and_bundle(db_path)
    conn = sqlite3.connect(db_path)
    try:
        bundle_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT bundle_id FROM bot_raw_ingest "
            "WHERE status = 'bundled'").fetchall()]
    finally:
        conn.close()
    processed = [process_bundle(bid, db_path) for bid in bundle_ids]
    return {"bundled": bundle_result["bundled"], "processed": processed}


if __name__ == "__main__":
    import json
    if "--once" in sys.argv[1:]:
        print(json.dumps(tick(), indent=2))
    else:
        print(f"debounce_worker: quiet_window={QUIET_WINDOW_SECONDS}s "
              f"tick={TICK_SECONDS}s", file=sys.stderr)
        while True:
            try:
                tick()
            except Exception as e:
                print(f"tick failed: {e}", file=sys.stderr)
            time.sleep(TICK_SECONDS)
