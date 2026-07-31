#!/usr/bin/env python3
"""
PeerShop — Phase B: department-aware, GOVERNED product categorization.

Two narrow, single-purpose AI calls per product — never one blended call:

    1. category    : pick ONE category from the product's DEPARTMENT's closed
                     list (the department is already known from the seller's
                     profile / the product row), or None.
    2. subcategory : if the chosen category has subcategories, pick ONE from
                     THAT category's closed list, or None. Categories with no
                     subcategories skip this call entirely.

Each call is the same lean shape as before (CLAUDE.md):
  * No history, no tools, no agent framework, no big system prompt.
  * Tiny bounded prompt in, one short answer out (small max_tokens).
  * Stdlib only (urllib, via llm.py). Free-tier provider (Groq default).

GOVERNANCE (the Phase-B change): the taxonomy never grows silently.
  * The AI only ever returns a name from the closed list, or None. It cannot
    invent a category/subcategory.
  * When a SELLER names a category/subcategory that doesn't exist yet, it is
    NOT auto-created. It is queued in `pending_categories` / `pending_subcategories`
    for manual admin approval. (This replaces the old create_if_missing=True.)

Both the category list and subcategory lists are read from the DB at call time,
so the governed taxonomy stays the single source of truth.

Usage:
    from categorize import classify_product, set_product_category

    classify_product(product_id=1)          # AI two-step, persists both
    set_product_category(1, "footwear")     # seller override (governed)
    set_product_subcategory(1, "crocs")     # seller override (governed)

CLI:
    python3 categorize.py --text boutique "Red Kito Slippers" "comfy rubber"
    python3 categorize.py --product 1
    python3 categorize.py --pending         # classify every uncategorized product
"""

import os
import sqlite3
import sys

import llm

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "peershop.db")

# One short answer is plenty; a few names are multi-word ("staples & grains",
# "office/school supplies"), so leave a little headroom — still tiny.
_MAX_TOKENS = 16


# ── DB reads (the closed, governed lists) ────────────────────────────────────

def _categories(conn, department_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM categories WHERE department_id = ? ORDER BY name;",
        (department_id,))]


def _subcategories(conn, category_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM subcategories WHERE category_id = ? ORDER BY name;",
        (category_id,))]


def get_categories(department_id: int, db_path: str = DB_PATH) -> list[str]:
    """The closed list of category names for one department."""
    conn = sqlite3.connect(db_path)
    try:
        return _categories(conn, department_id)
    finally:
        conn.close()


def get_subcategories(category_id: int, db_path: str = DB_PATH) -> list[str]:
    """The closed list of subcategory names for one category."""
    conn = sqlite3.connect(db_path)
    try:
        return _subcategories(conn, category_id)
    finally:
        conn.close()


# ── The narrow AI call ───────────────────────────────────────────────────────

def _match(content: str, options: list[str]) -> str | None:
    """
    Map a model reply to exactly one allowed option, or None.

    Validated against the closed list — a hallucinated or off-list reply becomes
    None rather than corrupting the catalog. Tolerates a little verbosity
    (labels, trailing punctuation, "the category is ...").
    """
    answer = (content or "").strip().lower().strip(" .\"'`\n")
    for prefix in ("category:", "subcategory:", "answer:",
                   "the category is", "the subcategory is"):
        if answer.startswith(prefix):
            answer = answer[len(prefix):].strip(" .\"'`")
    if answer in ("", "none"):
        return None

    allowed = {c.lower(): c for c in options}
    if answer in allowed:
        return allowed[answer]
    # Verbose reply fallback: longest allowed name contained in the answer.
    hits = [c for c in options if c.lower() in answer]
    return max(hits, key=len) if hits else None


def _classify(title: str, description: str, options: list[str],
              kind: str, context_line: str) -> str | None:
    """One bounded classification call over a closed list. Returns a name or None."""
    if not options:
        return None
    prompt = (
        f"Classify this marketplace product into exactly ONE {kind}.\n"
        f"{context_line}"
        f"Allowed values: {', '.join(options)}.\n"
        f"Reply with only the single best {kind} name from that list, lowercase, "
        "nothing else. If none clearly fit, reply: none\n\n"
        f"Title: {title or '(none)'}\n"
        f"Description: {description or '(none)'}"
    )
    content = llm.complete(
        [{"role": "user", "content": prompt}],
        max_tokens=_MAX_TOKENS,
        temperature=0,          # deterministic classification
    )
    return _match(content, options)


def suggest(department: str, title: str, description: str,
            db_path: str = DB_PATH) -> dict:
    """
    Pure AI suggestion (no DB writes): department name + text -> {category, subcategory}.

    Handy for previewing/testing the two-step without a product row.
    """
    conn = sqlite3.connect(db_path)
    try:
        drow = conn.execute("SELECT id FROM departments WHERE name = ?",
                            (department,)).fetchone()
        if drow is None:
            raise ValueError(f"unknown department: {department!r}")
        dept_id = drow[0]

        cat = _classify(title, description, _categories(conn, dept_id),
                        "category", f"Department: {department}.\n")
        sub = None
        if cat is not None:
            cat_id = conn.execute(
                "SELECT id FROM categories WHERE department_id = ? AND name = ?",
                (dept_id, cat)).fetchone()[0]
            subs = _subcategories(conn, cat_id)
            if subs:
                sub = _classify(title, description, subs, "subcategory",
                                f"This is a {cat} product.\n")
        return {"category": cat, "subcategory": sub}
    finally:
        conn.close()


# ── Persisting AI suggestions onto a product (the two-step) ──────────────────

def classify_product(product_id: int, db_path: str = DB_PATH) -> dict:
    """
    Run the department-aware two-step on one product row and persist both the
    category_id and (if applicable) subcategory_id.

    Returns {"category": name|None, "subcategory": name|None}. The stored values
    are just the AI's suggestion — the seller can override via set_product_*().
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        row = conn.execute(
            "SELECT p.department_id, d.name, p.title, p.description "
            "FROM products p JOIN departments d ON d.id = p.department_id "
            "WHERE p.id = ?", (product_id,)).fetchone()
        if row is None:
            raise ValueError(f"no product with id={product_id}")
        dept_id, dept_name, title, desc = row

        result = {"category": None, "subcategory": None}

        cat = _classify(title, desc, _categories(conn, dept_id),
                        "category", f"Department: {dept_name}.\n")
        if cat is None:
            # No confident match — leave uncategorized (admin/seller resolves).
            # Governance: the AI never proposes a NEW category.
            return result

        cat_id = conn.execute(
            "SELECT id FROM categories WHERE department_id = ? AND name = ?",
            (dept_id, cat)).fetchone()[0]
        # Setting/refreshing the category invalidates any old subcategory.
        conn.execute(
            "UPDATE products SET category_id = ?, subcategory_id = NULL WHERE id = ?",
            (cat_id, product_id))
        result["category"] = cat

        subs = _subcategories(conn, cat_id)
        if subs:
            sub = _classify(title, desc, subs, "subcategory",
                            f"This is a {cat} product.\n")
            if sub is not None:
                sub_id = conn.execute(
                    "SELECT id FROM subcategories WHERE category_id = ? AND name = ?",
                    (cat_id, sub)).fetchone()[0]
                conn.execute(
                    "UPDATE products SET subcategory_id = ? WHERE id = ?",
                    (sub_id, product_id))
                result["subcategory"] = sub

        conn.commit()
        return result
    finally:
        conn.close()


def categorize_product(product_id: int, db_path: str = DB_PATH) -> str | None:
    """Back-compat: run the two-step, return just the category name (or None)."""
    return classify_product(product_id, db_path)["category"]


# ── Seller overrides — GOVERNED (no silent taxonomy growth) ──────────────────

def _propose(conn, table: str, scope_col: str, scope_id: int,
             name: str, seller_id: int | None, context: str | None) -> int:
    """Enqueue a pending_* proposal (deduped on an open proposal). Returns its id."""
    existing = conn.execute(
        f"SELECT id FROM {table} "
        f"WHERE {scope_col} = ? AND lower(proposed_name) = ? AND status = 'pending'",
        (scope_id, name.lower())).fetchone()
    if existing:
        return existing[0]
    cur = conn.execute(
        f"INSERT INTO {table} ({scope_col}, proposed_name, proposed_by, context) "
        "VALUES (?, ?, ?, ?)",
        (scope_id, name, seller_id, context))
    return cur.lastrowid


def assign_category(product_id: int, category_name: str | None,
                    db_path: str = DB_PATH) -> dict:
    """
    Set/clear a product's category by NAME — the seller-facing override, GOVERNED.

    - Known name (in the product's department)  -> set it. Changing the category
      clears any existing subcategory (it belonged to the old category).
    - Unknown name  -> NOT created. Queued in pending_categories for admin
      approval; the product stays uncategorized.
    - None/blank    -> clears category (and subcategory).

    Returns {"status": set|cleared|pending, "name": str|None, "pending_id": int|None}.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        prod = conn.execute(
            "SELECT department_id, seller_id, category_id, title "
            "FROM products WHERE id = ?", (product_id,)).fetchone()
        if prod is None:
            raise ValueError(f"no product with id={product_id}")
        dept_id, seller_id, cur_cat_id, title = prod

        if category_name is None or not category_name.strip():
            conn.execute(
                "UPDATE products SET category_id = NULL, subcategory_id = NULL "
                "WHERE id = ?", (product_id,))
            conn.commit()
            return {"status": "cleared", "name": None, "pending_id": None}

        name = category_name.strip().lower()
        row = conn.execute(
            "SELECT id FROM categories WHERE department_id = ? AND name = ?",
            (dept_id, name)).fetchone()

        if row is None:
            # GOVERNED: no auto-create — queue for admin approval.
            pending_id = _propose(conn, "pending_categories", "department_id",
                                  dept_id, name, seller_id, title)
            conn.commit()
            return {"status": "pending", "name": name, "pending_id": pending_id}

        cat_id = row[0]
        if cat_id == cur_cat_id:
            conn.commit()  # no change; keep any existing subcategory
            return {"status": "set", "name": name, "pending_id": None}
        conn.execute(
            "UPDATE products SET category_id = ?, subcategory_id = NULL WHERE id = ?",
            (cat_id, product_id))
        conn.commit()
        return {"status": "set", "name": name, "pending_id": None}
    finally:
        conn.close()


def assign_subcategory(product_id: int, subcategory_name: str | None,
                       db_path: str = DB_PATH) -> dict:
    """
    Set/clear a product's subcategory by NAME — seller-facing override, GOVERNED.

    Requires the product to already have a category (a subcategory is always
    scoped to one). Unknown names queue in pending_subcategories; nothing is
    auto-created. Returns the same shape as assign_category.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        prod = conn.execute(
            "SELECT seller_id, category_id FROM products WHERE id = ?",
            (product_id,)).fetchone()
        if prod is None:
            raise ValueError(f"no product with id={product_id}")
        seller_id, cat_id = prod

        if subcategory_name is None or not subcategory_name.strip():
            conn.execute("UPDATE products SET subcategory_id = NULL WHERE id = ?",
                         (product_id,))
            conn.commit()
            return {"status": "cleared", "name": None, "pending_id": None}

        if cat_id is None:
            raise ValueError("set a category before a subcategory")

        name = subcategory_name.strip().lower()
        row = conn.execute(
            "SELECT id FROM subcategories WHERE category_id = ? AND name = ?",
            (cat_id, name)).fetchone()

        if row is None:
            pending_id = _propose(conn, "pending_subcategories", "category_id",
                                  cat_id, name, seller_id, None)
            conn.commit()
            return {"status": "pending", "name": name, "pending_id": pending_id}

        conn.execute("UPDATE products SET subcategory_id = ? WHERE id = ?",
                     (row[0], product_id))
        conn.commit()
        return {"status": "set", "name": name, "pending_id": None}
    finally:
        conn.close()


def assign_category_by_id(product_id: int, category_id: int,
                          db_path: str = DB_PATH) -> dict:
    """
    Set a product's category by its GLOBAL id — admin override that may pick a
    category in a DIFFERENT department than the product currently sits in. Because
    a category is owned by exactly one department (composite FK), this also MOVES
    the product into that category's department and clears any subcategory (it
    belonged to the old category). Admin-only; the governed seller-facing path
    (assign_category, by name within the seller's department) is unchanged.

    Returns {"status":"set","name","category_id","department_id"}.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        if conn.execute("SELECT 1 FROM products WHERE id = ?",
                        (product_id,)).fetchone() is None:
            raise ValueError(f"no product with id={product_id}")
        cat = conn.execute(
            "SELECT id, department_id, name FROM categories WHERE id = ?",
            (category_id,)).fetchone()
        if cat is None:
            raise ValueError(f"no category with id={category_id}")
        conn.execute(
            "UPDATE products SET department_id = ?, category_id = ?, "
            "subcategory_id = NULL WHERE id = ?", (cat[1], cat[0], product_id))
        conn.commit()
        return {"status": "set", "name": cat[2], "category_id": cat[0],
                "department_id": cat[1]}
    finally:
        conn.close()


def admin_add_category(product_id: int, name: str, db_path: str = DB_PATH) -> dict:
    """
    ADMIN privilege: create a category (under the product's current department) and
    assign it to the product. This is the admin's explicit, confirmed decision — the
    admin IS the approver, so unlike the seller-facing `assign_category` (which
    QUEUES unknown names in pending_categories), this creates the category directly.
    If a category of that name already exists in the department it is reused, not
    duplicated. Clears any subcategory. Returns
    {"status":"created"|"set","name","category_id","department_id"}.
    """
    name = (name or "").strip().lower()
    if not name:
        raise ValueError("category name is empty")
    if len(name) > 50:
        raise ValueError("category name too long")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        prod = conn.execute("SELECT department_id FROM products WHERE id = ?",
                            (product_id,)).fetchone()
        if prod is None:
            raise ValueError(f"no product with id={product_id}")
        dept_id = prod[0]
        row = conn.execute(
            "SELECT id FROM categories WHERE department_id = ? AND name = ?",
            (dept_id, name)).fetchone()
        if row:
            cid, status = row[0], "set"
        else:
            cid = conn.execute(
                "INSERT INTO categories (department_id, name) VALUES (?, ?)",
                (dept_id, name)).lastrowid
            status = "created"
        conn.execute(
            "UPDATE products SET category_id = ?, subcategory_id = NULL WHERE id = ?",
            (cid, product_id))
        conn.commit()
        return {"status": status, "name": name, "category_id": cid,
                "department_id": dept_id}
    finally:
        conn.close()


def set_product_category(product_id: int, category_name: str | None,
                         db_path: str = DB_PATH) -> str | None:
    """
    Back-compat wrapper over assign_category: returns the stored category name
    when it was actually set on the product, else None (cleared, or queued for
    approval). Callers wanting the full outcome should use assign_category.
    """
    r = assign_category(product_id, category_name, db_path)
    return r["name"] if r["status"] == "set" else None


def set_product_subcategory(product_id: int, subcategory_name: str | None,
                            db_path: str = DB_PATH) -> str | None:
    """Back-compat-style wrapper over assign_subcategory (see set_product_category)."""
    r = assign_subcategory(product_id, subcategory_name, db_path)
    return r["name"] if r["status"] == "set" else None


def classify_pending(db_path: str = DB_PATH) -> list[tuple[int, dict]]:
    """Classify every product with no category yet. Returns [(id, result), ...]."""
    conn = sqlite3.connect(db_path)
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM products WHERE category_id IS NULL ORDER BY id;")]
    finally:
        conn.close()
    return [(pid, classify_product(pid, db_path)) for pid in ids]


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--text":
        # --text <department> "<title>" "<description>"
        dept = args[1] if len(args) > 1 else ""
        title = args[2] if len(args) > 2 else ""
        desc = args[3] if len(args) > 3 else ""
        print(suggest(dept, title, desc))
    elif args and args[0] == "--product":
        print(classify_product(int(args[1])))
    elif args and args[0] == "--pending":
        for pid, result in classify_pending():
            print(f"product {pid}: {result}")
    else:
        print(__doc__)
