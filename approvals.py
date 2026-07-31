#!/usr/bin/env python3
"""
PeerShop — admin approval layer (the gate between 'pending' and 'live').

THE single decision path for promoting/rejecting a listing, so every caller uses
the same guards and governance:

  * review.py            — the human admin's web queue (port 8001)
  * this module's CLI    — a human on the shell, OR **Hermes** (the always-on
                           agent) approving listings when no human admin is around
  * import approvals     — Hermes can also call pending_queue()/approve()/reject()
                           directly in Python

Invariants enforced here, once, for every caller:
  * A product moves out of 'pending' ONLY from a still-'pending' row (a stale tab
    or re-post can't flip a live/rejected item) — same guard as before.
  * GOVERNANCE: a product cannot go live while UNCATEGORIZED. approve() may take a
    category (routed through the governed categorize.assign_category — an unknown
    name is queued, never auto-created); if the product is still uncategorized
    afterwards, approval is refused with reason 'needs_category'.
  * On any decision, the seller is notified over Telegram IF they've linked their
    account (best-effort; a delivery failure never blocks the decision).

Stdlib only (uses telegram_api — deliberately NOT telegram_bot, which would pull
in google.genai via bot.py and break the stdlib-only core).

CLI:
    python3 approvals.py --list
    python3 approvals.py --approve <id> [--category NAME]
    python3 approvals.py --reject  <id> [--reason "..."]
"""

import argparse
import os
import sqlite3
import sys

import categorize
import telegram_api
import telegram_link

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "peershop.db")


def _conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ── read ──────────────────────────────────────────────────────────────────────

def pending_queue(db_path: str = DB_PATH) -> list[dict]:
    """Everything awaiting a decision, oldest first, with all the context a human
    OR Hermes needs to judge it (department, price, category, seller, photos)."""
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT p.id, p.title, p.description, p.price, p.category_id, "
            "       p.image_path, d.name AS department, c.name AS category, "
            "       s.id AS seller_id, s.name AS seller "
            "FROM products p "
            "JOIN sellers s ON s.id = p.seller_id "
            "JOIN departments d ON d.id = p.department_id "
            "LEFT JOIN categories c ON c.id = p.category_id "
            "WHERE p.status = 'pending' ORDER BY p.created_at, p.id").fetchall()
        out = []
        for r in rows:
            photos = [pr[0] for pr in conn.execute(
                "SELECT image_path FROM product_photos WHERE product_id = ? "
                "ORDER BY position, id", (r["id"],))] or [r["image_path"]]
            d = dict(r)
            d["photos"] = photos
            out.append(d)
        return out


def live_listings(term: str | None = None, db_path: str = DB_PATH) -> list[dict]:
    """Every LIVE product across the site (newest first) — the admin takedown view.
    Same shape/context as pending_queue so the review UI can render it uniformly.

    Optional `term` narrows the result IN SQL (only matches are fetched, not the
    whole table then Python-filtered — the optimisation):
      * a number or '#42'  → exact product-id match (primary-key lookup, instant);
      * anything else      → title OR seller-name substring (parameterized,
                             LIKE-escaped)."""
    sql = (
        "SELECT p.id, p.title, p.price, p.image_path, "
        "       d.name AS department, c.name AS category, "
        "       s.id AS seller_id, s.name AS seller "
        "FROM products p "
        "JOIN sellers s ON s.id = p.seller_id "
        "JOIN departments d ON d.id = p.department_id "
        "LEFT JOIN categories c ON c.id = p.category_id "
        "WHERE p.status = 'live' ")
    params: list = []
    term = (term or "").strip()
    if term:
        ident = term.lstrip("#")
        if ident.isdigit():
            sql += "AND p.id = ? "
            params.append(int(ident))
        else:
            safe = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{safe}%"
            sql += "AND (p.title LIKE ? ESCAPE '\\' OR s.name LIKE ? ESCAPE '\\') "
            params += [like, like]
    sql += "ORDER BY p.created_at DESC, p.id DESC"
    with _conn(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ── decisions (the one path) ───────────────────────────────────────────────────

def _notify(seller_id: int, text: str, db_path: str) -> None:
    """Best-effort Telegram note to the seller (only if they've linked). Never raises."""
    try:
        tid = telegram_link.get_telegram_for_seller(seller_id, db_path=db_path)
        if tid:
            telegram_api.send_message(tid, text)     # already swallows its own errors
    except Exception as e:
        print(f"notify failed for seller {seller_id}: {e}", file=sys.stderr)


def _pending_row(conn, product_id):
    return conn.execute(
        "SELECT id, seller_id, title, category_id FROM products "
        "WHERE id = ? AND status = 'pending'", (product_id,)).fetchone()


def approve(product_id: int, category: str | None = None, actor: str = "human",
            notify: bool = True, db_path: str = DB_PATH) -> dict:
    """
    Promote a pending listing to 'live'. If `category` is given it is set first via
    the governed path (unknown → queued, product stays uncategorized). A product
    that is still uncategorized cannot go live → returns ok=False, reason
    'needs_category'. Returns a result dict.
    """
    with _conn(db_path) as conn:
        row = _pending_row(conn, product_id)
    if row is None:
        return {"ok": False, "reason": "not_pending",
                "message": f"#{product_id} is not a pending product."}

    cat_result = None
    if category and category.strip():
        cat_result = categorize.assign_category(product_id, category, db_path=db_path)

    # Re-check: governance forbids promoting an uncategorized product.
    with _conn(db_path) as conn:
        cat_id = conn.execute("SELECT category_id FROM products WHERE id = ?",
                              (product_id,)).fetchone()[0]
    if cat_id is None:
        msg = f"#{product_id} “{row['title']}” has no category — set one to promote."
        if cat_result and cat_result.get("status") == "pending":
            msg = (f"category “{cat_result['name']}” isn't approved yet (queued) — "
                   f"#{product_id} stays pending until it is.")
        return {"ok": False, "reason": "needs_category", "message": msg,
                "category": cat_result}

    with _conn(db_path) as conn:
        changed = conn.execute(
            "UPDATE products SET status = 'live' WHERE id = ? AND status = 'pending'",
            (product_id,)).rowcount
        conn.commit()
    if not changed:
        return {"ok": False, "reason": "not_pending",
                "message": f"#{product_id} was no longer pending."}

    if notify:
        _notify(row["seller_id"],
                f"✅ Your listing “{row['title']}” (#{product_id}) is now LIVE on "
                "PeerShop.", db_path)
    return {"ok": True, "status": "live", "product_id": product_id,
            "title": row["title"], "actor": actor}


def reject(product_id: int, reason: str | None = None, actor: str = "human",
           notify: bool = True, db_path: str = DB_PATH) -> dict:
    """Reject a pending listing (status → 'rejected'). Returns a result dict."""
    with _conn(db_path) as conn:
        row = _pending_row(conn, product_id)
        if row is None:
            return {"ok": False, "reason": "not_pending",
                    "message": f"#{product_id} is not a pending product."}
        conn.execute(
            "UPDATE products SET status = 'rejected' WHERE id = ? AND status = 'pending'",
            (product_id,))
        conn.commit()
    if notify:
        tail = f"\nReason: {reason}" if reason else ""
        _notify(row["seller_id"],
                f"✗ Your listing “{row['title']}” (#{product_id}) wasn't approved."
                + tail, db_path)
    return {"ok": True, "status": "rejected", "product_id": product_id,
            "title": row["title"], "actor": actor}


def admin_remove(product_id: int, reason: str | None = None, actor: str = "human",
                 notify: bool = True, db_path: str = DB_PATH) -> dict:
    """Admin takedown of ANY product regardless of status (live/pending/rejected)
    → 'removed'. A SOFT delete (matches the seller-remove convention; never a hard
    DELETE), so it disappears from the public site (serve.py shows only 'live') but
    the row and its media are retained. Best-effort seller Telegram notice, same as
    approve/reject. Already-'removed' rows are a no-op (ok=False, already_removed)."""
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT id, seller_id, title, status FROM products WHERE id = ?",
            (product_id,)).fetchone()
        if row is None:
            return {"ok": False, "reason": "not_found",
                    "message": f"#{product_id} does not exist."}
        if row["status"] == "removed":
            return {"ok": False, "reason": "already_removed",
                    "message": f"#{product_id} “{row['title']}” is already removed."}
        conn.execute(
            "UPDATE products SET status = 'removed', updated_at = datetime('now') "
            "WHERE id = ?", (product_id,))
        conn.commit()
    if notify:
        tail = f"\nReason: {reason}" if reason else ""
        _notify(row["seller_id"],
                f"⚠️ Your listing “{row['title']}” (#{product_id}) was removed from "
                "PeerShop by an admin." + tail, db_path)
    return {"ok": True, "status": "removed", "product_id": product_id,
            "title": row["title"], "prev_status": row["status"], "actor": actor}


# ── CLI (human shell OR Hermes) ────────────────────────────────────────────────

def _print_queue(db_path):
    q = pending_queue(db_path)
    if not q:
        print("Nothing pending. All caught up.")
        return
    print(f"{len(q)} pending listing(s):\n")
    for p in q:
        price = f"₹{p['price']:.0f}" if p["price"] is not None else "no price"
        cat = p["category"] or "UNCATEGORIZED (blocks promote — pass --category)"
        print(f"  #{p['id']}  {p['title']!r}")
        print(f"      seller: {p['seller']}  ·  dept: {p['department']}  ·  {price}"
              f"  ·  photos: {len(p['photos'])}")
        print(f"      category: {cat}")
        desc = (p["description"] or "").strip().replace("\n", " ")
        if desc:
            print(f"      desc: {desc[:120]}{'…' if len(desc) > 120 else ''}")
        print()


def main(argv=None):
    ap = argparse.ArgumentParser(description="PeerShop listing approval (admin / Hermes).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="show the pending queue")
    g.add_argument("--approve", type=int, metavar="ID", help="promote a pending listing")
    g.add_argument("--reject", type=int, metavar="ID", help="reject a pending listing")
    g.add_argument("--remove", type=int, metavar="ID",
                   help="admin takedown of ANY product (live/pending/rejected) → removed")
    ap.add_argument("--category", help="category to set when approving (governed)")
    ap.add_argument("--reason", help="optional reason when rejecting/removing")
    ap.add_argument("--actor", default="cli", help="who is deciding (audit label)")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args(argv)

    if args.list:
        _print_queue(args.db)
        return 0
    if args.approve is not None:
        r = approve(args.approve, category=args.category, actor=args.actor, db_path=args.db)
    elif args.remove is not None:
        r = admin_remove(args.remove, reason=args.reason, actor=args.actor, db_path=args.db)
    else:
        r = reject(args.reject, reason=args.reason, actor=args.actor, db_path=args.db)
    print(("OK: " if r["ok"] else "NO: ") + (r.get("message")
          or f"#{r['product_id']} “{r.get('title','')}” → {r.get('status')}"))
    return 0 if r["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
