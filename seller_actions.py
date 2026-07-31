#!/usr/bin/env python3
"""
PeerShop — shared seller action layer (foundation for Phase G).

THE single source of truth for product changes made ON A SELLER'S BEHALF. The
Telegram bot (LLM path AND slash-command fallback) and any future website
dashboard call THESE functions — they must never write to `products` directly.

    post_product()    create a listing (ALWAYS status='pending')
    remove_product()  SOFT remove (status -> 'removed'); never a hard DELETE
    update_price()    change price; edit returns the listing to 'pending' review
    get_my_products() a seller's own listings (for /mystore + exact-match lookups)

Invariants enforced here, once, for every caller:
  * Ownership: every mutation checks the product belongs to the seller.
  * Governance: bot-posted / edited products land status='pending' — edits never
    bypass admin review (review.py is still the only path to 'live').
  * Governed taxonomy / freeform attributes reuse the existing modules
    (categorize.assign_*, attributes.set_product_attributes) — no duplication.

Stdlib only. NOTE: intake.py (the heavy AI photo→describe pipeline) still has its
own create path; routing that through post_product() is a sensible follow-up so
there is truly ONE writer, but intake predates this layer and is left as-is for
now (flagged for review).

CLI:
    python3 seller_actions.py --mystore <seller_id>
"""

import os
import sqlite3
import sys

import attributes
import categorize

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "peershop.db")

# A seller's non-deleted store (what get_my_products returns by default).
_VISIBLE_STATUSES = ("pending", "live", "rejected")


class SellerActionError(Exception):
    """Validation / ownership failure in a seller action (safe to show the seller)."""


# ── helpers ──────────────────────────────────────────────────────────────────

def _validate_price(price):
    try:
        p = float(price)
    except (TypeError, ValueError):
        raise SellerActionError(f"'{price}' is not a valid price")
    if p < 0:
        raise SellerActionError("price cannot be negative")
    return p


def _resolve_department(conn, seller_id: int, department: str | None) -> int:
    """Pick the ONE department for this product from the seller's own set."""
    rows = conn.execute(
        "SELECT d.id, d.name FROM seller_departments sd "
        "JOIN departments d ON d.id = sd.department_id "
        "WHERE sd.seller_id = ? ORDER BY d.id;", (seller_id,)).fetchall()
    if not rows:
        raise SellerActionError(
            "you have no departments yet — finish onboarding first")
    names = ", ".join(n for _, n in rows)
    if department and department.strip():
        want = department.strip().lower()
        for did, name in rows:
            if name == want:
                return did
        raise SellerActionError(
            f"'{department}' is not one of your departments ({names})")
    if len(rows) == 1:
        return rows[0][0]
    raise SellerActionError(
        f"you sell in multiple departments ({names}) — say which one this is for")


def _owned_status(conn, seller_id: int, product_id: int) -> str:
    """Return the product's status, or raise if it isn't this seller's."""
    row = conn.execute(
        "SELECT status FROM products WHERE id = ? AND seller_id = ?",
        (product_id, seller_id)).fetchone()
    if row is None:
        raise SellerActionError(f"you have no product with id {product_id}")
    return row[0]


def get_product(product_id: int, db_path: str = DB_PATH) -> dict | None:
    """One product as a plain dict (with taxonomy names + price + status)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute(
            "SELECT p.id, p.seller_id, p.title, p.description, p.price, p.status, "
            "       p.image_path, p.category_id, d.name AS department, "
            "       c.name AS category, sc.name AS subcategory "
            "FROM products p JOIN departments d ON d.id = p.department_id "
            "LEFT JOIN categories c ON c.id = p.category_id "
            "LEFT JOIN subcategories sc ON sc.id = p.subcategory_id "
            "WHERE p.id = ?", (product_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


# ── the four actions ─────────────────────────────────────────────────────────

def post_product(seller_id: int, title: str, description: str, image_path: str,
                 department: str | None = None, category: str | None = None,
                 subcategory: str | None = None, price=None,
                 photos: list[str] | None = None,
                 attribute_values: dict | None = None, job_id: str | None = None,
                 db_path: str = DB_PATH) -> dict:
    """
    Create a listing for a seller. ALWAYS lands status='pending' (never bypasses
    review). Department is resolved from the seller's profile; taxonomy and
    attributes go through the governed helpers. Returns the created product dict.

    `image_path` is the PRIMARY photo (mirrored into products.image_path so all
    single-image rendering keeps working). Optional `photos` is the full ordered
    list (up to 4; first should equal image_path) recorded in product_photos for
    the gallery; if omitted, the single image_path is recorded as the sole photo.
    This is the ONE place product_photos rows are written for a new listing.
    """
    if not (title and title.strip()):
        raise SellerActionError("a title is required")
    if not (image_path and image_path.strip()):
        raise SellerActionError("a product photo is required")
    price = _validate_price(price) if price is not None else None
    photo_list = [p.strip() for p in (photos or [image_path]) if p and p.strip()]

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        if conn.execute("SELECT 1 FROM sellers WHERE id = ?",
                        (seller_id,)).fetchone() is None:
            raise SellerActionError(f"no seller with id {seller_id}")
        dept_id = _resolve_department(conn, seller_id, department)
        pid = conn.execute(
            "INSERT INTO products (seller_id, department_id, image_path, title, "
            "description, price, job_id, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
            (seller_id, dept_id, image_path.strip(), title.strip(),
             (description or "").strip() or None, price, job_id)).lastrowid
        for pos, ppath in enumerate(photo_list[:4]):
            conn.execute(
                "INSERT INTO product_photos (product_id, image_path, position) "
                "VALUES (?, ?, ?)", (pid, ppath, pos))
        conn.commit()
    finally:
        conn.close()

    # Governed taxonomy (seller self-select). Unknown names → pending queue.
    if category and category.strip():
        categorize.assign_category(pid, category, db_path=db_path)
        if subcategory and subcategory.strip():
            categorize.assign_subcategory(pid, subcategory, db_path=db_path)

    # Freeform attributes — only if a real category was set (governance/schema).
    if attribute_values:
        prod = get_product(pid, db_path)
        if prod and prod["category_id"] is not None:
            attributes.set_product_attributes(pid, attribute_values, db_path=db_path)

    return get_product(pid, db_path)


def update_price(seller_id: int, product_id: int, price,
                 db_path: str = DB_PATH) -> dict:
    """
    Change a listing's price. Price is a low-risk, trivial edit, so it does NOT
    change status — a live listing STAYS live. (Content edits — description /
    photo / category — are the ones that revert to 'pending' for re-review; a
    price tweak does not.) Cannot edit a removed product.
    """
    price = _validate_price(price)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        status = _owned_status(conn, seller_id, product_id)
        if status == "removed":
            raise SellerActionError(
                f"product {product_id} was removed — repost it instead of editing")
        conn.execute("UPDATE products SET price = ? WHERE id = ?",
                     (price, product_id))
        conn.commit()
    finally:
        conn.close()
    return get_product(product_id, db_path)


def update_image(seller_id: int, product_id: int, image_path: str,
                 db_path: str = DB_PATH) -> dict:
    """
    Replace a listing's photo. A photo is CONTENT, so this reverts the listing to
    'pending' for re-review (unlike a price-only edit). Cannot edit a removed one.
    """
    if not (image_path and image_path.strip()):
        raise SellerActionError("an image is required")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        status = _owned_status(conn, seller_id, product_id)
        if status == "removed":
            raise SellerActionError(
                f"product {product_id} was removed — repost it instead of editing")
        conn.execute(
            "UPDATE products SET image_path = ?, status = 'pending' WHERE id = ?",
            (image_path.strip(), product_id))
        # Keep the gallery consistent: a single-photo replace resets product_photos
        # to just the new image (the gallery reads product_photos when rows exist).
        conn.execute("DELETE FROM product_photos WHERE product_id = ?", (product_id,))
        conn.execute(
            "INSERT INTO product_photos (product_id, image_path, position) "
            "VALUES (?, ?, 0)", (product_id, image_path.strip()))
        conn.commit()
    finally:
        conn.close()
    return get_product(product_id, db_path)


def remove_product(seller_id: int, product_id: int,
                   db_path: str = DB_PATH) -> dict:
    """SOFT remove: status -> 'removed'. Never a hard DELETE. Idempotent."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        status = _owned_status(conn, seller_id, product_id)
        if status != "removed":
            conn.execute("UPDATE products SET status = 'removed' WHERE id = ?",
                         (product_id,))
            conn.commit()
    finally:
        conn.close()
    return get_product(product_id, db_path)


def get_my_products(seller_id: int, include_removed: bool = False,
                    db_path: str = DB_PATH) -> list[dict]:
    """
    A seller's own listings (pending/live/rejected by default; add removed with
    include_removed=True). Ordered live → pending → rejected → removed, newest first.
    """
    statuses = list(_VISIBLE_STATUSES) + (["removed"] if include_removed else [])
    placeholders = ",".join("?" for _ in statuses)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT p.id, p.title, p.price, p.status, p.image_path, "
            "       d.name AS department, c.name AS category, sc.name AS subcategory "
            "FROM products p JOIN departments d ON d.id = p.department_id "
            "LEFT JOIN categories c ON c.id = p.category_id "
            "LEFT JOIN subcategories sc ON sc.id = p.subcategory_id "
            f"WHERE p.seller_id = ? AND p.status IN ({placeholders}) "
            "ORDER BY CASE p.status WHEN 'live' THEN 0 WHEN 'pending' THEN 1 "
            "         WHEN 'rejected' THEN 2 ELSE 3 END, p.id DESC",
            (seller_id, *statuses)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--mystore":
        import json
        print(json.dumps(get_my_products(int(sys.argv[2])), indent=2))
    else:
        print("Usage: python3 seller_actions.py --mystore <seller_id>")
        raise SystemExit(1)
