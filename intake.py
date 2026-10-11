#!/usr/bin/env python3
"""
PeerShop — intake bridge + per-product listing (Phase D).

Connects the existing Hermes photo pipeline (/root/pipeline/product_intake.py:
receive_photo → remove_background → enhance) to PeerShop's catalog, turning a
real incoming photo into a PENDING product row — then runs the per-product
listing flow around it.

Image PROCESSING (bg-removal + enhance) ALWAYS runs — it's independent of how the
description is authored. The DESCRIPTION has two first-class, parallel paths:

  * AI-draft path  (ingest_photo)  — PeerShop's Gemini-3 two-step describe, then
    an optional bounded refinement loop (refine_description, capped at 3 rounds),
    ending in approve-as-is or a hand-edit (set_manual_description).
  * Manual path    (ingest_manual) — the seller writes their own title +
    description; NO describe/refine calls are made (0 Gemini). Available from the
    first message; switching to it mid-refinement ends AI drafting immediately.

Taxonomy: the seller's self-selection wins (assign_category/subcategory, governed);
otherwise the narrow AI two-step suggests (categorize.classify_product — Groq,
free). Category-scoped attribute values are then prefilled from the seller's own
history (attributes.prefill_attributes — plain SQL, zero AI).

Department: a product belongs to ONE of the seller's departments (they may have
several). It's resolved from the seller's profile — passed explicitly, or the
sole department if they have exactly one.

Hard rule (unchanged): every product created here lands status='pending',
UNCONDITIONALLY. Nothing here promotes to 'live' — that's review.py, a human admin.

This module talks to the heavy pipeline (rembg / PIL / Gemini), so ingest_* must
run under the project venv:  ./.venv/bin/python intake.py ...
PeerShop's core (serve/review/categorize/attributes) stays stdlib-only.

CLI:
    ./.venv/bin/python intake.py <seller_id> <photo> [department] [seller_category]
    ./.venv/bin/python intake.py --manual <seller_id> <photo> <department> "<title>" "<desc>"
    ./.venv/bin/python intake.py --refine <product_id> "<change request>"
    ./.venv/bin/python intake.py --set-desc <product_id> "<title>" "<desc>"
"""

import importlib.util
import json
import os
import sqlite3
import sys

import attributes
import categorize
import describe
import seller_actions

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "peershop.db")
PIPELINE_PATH = "/root/pipeline/product_intake.py"
HERMES_ENV = "/root/.hermes/.env"

REFINE_CAP = 3          # bounded 2–3 round refinement (CLAUDE.md); then approve/hand-edit
_CAP_MESSAGE = ("Refinement limit reached — approve as-is, or submit your own "
                "final description.")


def _load_pipeline():
    """Import /root/pipeline/product_intake.py under a distinct module name."""
    spec = importlib.util.spec_from_file_location(
        "hermes_pipeline_intake", PIPELINE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ensure_gemini_key():
    """describe reads GEMINI_API_KEY from the env — load it if absent."""
    if os.environ.get("GEMINI_API_KEY"):
        return
    if os.path.exists(HERMES_ENV):
        for line in open(HERMES_ENV):
            line = line.strip()
            if line.startswith("GEMINI_API_KEY=") and not line.startswith("#"):
                os.environ["GEMINI_API_KEY"] = line.split("=", 1)[1]
                return


def _run_pipeline(incoming_path: str) -> str:
    """Background removal + enhance ALWAYS runs. Returns the processed image path."""
    pipeline = _load_pipeline()
    received = pipeline.receive_photo(incoming_path)
    nobg = pipeline.remove_background(received)
    return pipeline.enhance_image(nobg)


def _finalize(seller_id: int, department: str | None, image_path: str,
              title: str, description: str, seller_category: str | None,
              seller_subcategory: str | None, db_path: str, extra: dict) -> dict:
    """
    Shared tail of both intake paths. The product ROW is created through the ONE
    seller-behalf write path (seller_actions.post_product) — intake no longer
    INSERTs into products directly. post_product resolves the department and
    applies the seller's self-selected taxonomy (governed); when the seller did
    NOT self-select, we run the AI classifier here.
    """
    prod = seller_actions.post_product(
        seller_id, title, description, image_path,
        department=department, category=seller_category,
        subcategory=seller_subcategory, db_path=db_path)
    product_id = prod["id"]

    # AI taxonomy only when the seller did NOT self-select a category (Groq, free).
    if not (seller_category and seller_category.strip()):
        categorize.classify_product(product_id, db_path=db_path)

    # Read back stored taxonomy, then compute the zero-AI attribute prefill.
    conn = sqlite3.connect(db_path)
    try:
        r = conn.execute(
            "SELECT d.name, p.category_id, c.name, s.name "
            "FROM products p JOIN departments d ON d.id = p.department_id "
            "LEFT JOIN categories c ON c.id = p.category_id "
            "LEFT JOIN subcategories s ON s.id = p.subcategory_id "
            "WHERE p.id = ?", (product_id,)).fetchone()
    finally:
        conn.close()
    dept_name, category_id, category_name, subcategory_name = r

    prefill = attributes.prefill_attributes(seller_id, category_id, db_path) if category_id else {}
    attr_defs = attributes.get_category_attributes(category_id, db_path) if category_id else []

    summary = {
        "product_id": product_id,
        "status": "pending",
        "image_path": image_path,
        "title": title,
        "description": description,
        "department": dept_name,
        "category": category_name,
        "subcategory": subcategory_name,
        "attribute_fields": [a["name"] for a in attr_defs],
        "attribute_prefill": prefill,     # form defaults from this seller's history
    }
    summary.update(extra or {})
    return summary


def ingest_photo(seller_id: int, incoming_path: str, department: str | None = None,
                 seller_category: str | None = None,
                 seller_subcategory: str | None = None,
                 db_path: str = DB_PATH) -> dict:
    """AI-draft path: process image → Gemini two-step describe → pending product."""
    _ensure_gemini_key()
    final_image = _run_pipeline(incoming_path)
    listing = describe.describe(final_image)
    return _finalize(
        seller_id, department, final_image, listing["title"], listing["description"],
        seller_category, seller_subcategory, db_path,
        extra={"path": "ai-draft", "detected_object": listing.get("object")})


def ingest_photo_noninteractive(seller_id: int, incoming_path: str,
                                raw_text_hint: str | None = None,
                                department: str | None = None,
                                seller_category: str | None = None,
                                seller_subcategory: str | None = None,
                                db_path: str = DB_PATH) -> dict:
    """
    AI-draft path for bulk/bot-forwarded intake (Task 6): same as ingest_photo
    (process image -> Gemini two-step describe -> pending product via
    _finalize/post_product, no refine loop -- there's no human on the other
    end of a forwarded burst to iterate with) but ALSO accepts an optional
    raw_text_hint (the seller's/supplier's forwarded caption text, e.g. "Blue
    saree, cotton, 500rs") which is folded into STEP 2's listing-copy prompt
    so real caption details aren't thrown away just because this came from a
    forward instead of the conversational wizard.

    Any SellerActionError from the underlying post_product() call (e.g. "you
    have no departments yet") propagates unchanged -- the caller
    (debounce_worker.process_bundle) is responsible for turning that into a
    flagged bot_raw_ingest status rather than a lost product, not this layer.
    """
    _ensure_gemini_key()
    final_image = _run_pipeline(incoming_path)
    listing = describe.describe(final_image)
    if raw_text_hint and raw_text_hint.strip():
        listing = describe.incorporate_hint(listing, raw_text_hint.strip())
    return _finalize(
        seller_id, department, final_image, listing["title"], listing["description"],
        seller_category, seller_subcategory, db_path,
        extra={"path": "ai-draft-noninteractive", "detected_object": listing.get("object")})


def ingest_manual(seller_id: int, incoming_path: str, department: str,
                  title: str, description: str,
                  seller_category: str | None = None,
                  seller_subcategory: str | None = None,
                  db_path: str = DB_PATH) -> dict:
    """
    Manual-description path: bg-removal STILL runs, but NO describe call is made
    (0 Gemini). The seller supplies the title + description themselves.
    """
    if not (title and title.strip()) or not (description and description.strip()):
        raise ValueError("manual path requires a title and description")
    final_image = _run_pipeline(incoming_path)
    return _finalize(
        seller_id, department, final_image, title.strip(), description.strip(),
        seller_category, seller_subcategory, db_path,
        extra={"path": "manual", "detected_object": None})


def refine_description(product_id: int, change_request: str,
                       db_path: str = DB_PATH) -> dict:
    """
    One bounded AI refinement round. Returns the new title/description and the
    round number. At the cap (REFINE_CAP) it makes NO AI call and returns
    capped=True with a nudge to approve or hand-edit. Raises if the product is gone.
    """
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT title, description, refine_rounds FROM products WHERE id = ?",
            (product_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"no product with id={product_id}")
    title, desc, rounds = row

    if rounds >= REFINE_CAP:
        return {"product_id": product_id, "capped": True, "round": rounds,
                "title": title, "description": desc, "message": _CAP_MESSAGE}

    new = describe.refine(title, desc, change_request)   # Gemini (venv)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE products SET title = ?, description = ?, "
            "refine_rounds = refine_rounds + 1 WHERE id = ?",
            (new["title"], new["description"], product_id))
        conn.commit()
    finally:
        conn.close()

    reached = (rounds + 1) >= REFINE_CAP
    return {"product_id": product_id, "capped": False, "round": rounds + 1,
            "title": new["title"], "description": new["description"],
            "message": _CAP_MESSAGE if reached else None}


def set_manual_description(product_id: int, title: str, description: str,
                           db_path: str = DB_PATH) -> dict:
    """
    Hand-edit end state / switch to manual mid-refinement: NO AI. Sets the final
    text and LOCKS further AI refinement (refine_rounds -> cap), so switching to
    manual ends AI drafting immediately.
    """
    if not (title and title.strip()) or not (description and description.strip()):
        raise ValueError("title and description are required")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        if conn.execute("SELECT 1 FROM products WHERE id = ?",
                        (product_id,)).fetchone() is None:
            raise ValueError(f"no product with id={product_id}")
        conn.execute(
            "UPDATE products SET title = ?, description = ?, refine_rounds = ? "
            "WHERE id = ?",
            (title.strip(), description.strip(), REFINE_CAP, product_id))
        conn.commit()
    finally:
        conn.close()
    return {"product_id": product_id, "title": title.strip(),
            "description": description.strip(), "manual": True, "capped": True}


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--refine":
        print(json.dumps(refine_description(int(args[1]), args[2]), indent=2))
    elif args and args[0] == "--manual":
        print(json.dumps(ingest_manual(int(args[1]), args[2], args[3],
                                       args[4], args[5]), indent=2))
    elif args and args[0] == "--set-desc":
        print(json.dumps(set_manual_description(int(args[1]), args[2], args[3]),
                         indent=2))
    elif len(args) >= 2:
        seller = int(args[0])
        photo = args[1]
        dept = args[2] if len(args) > 2 else None
        scat = args[3] if len(args) > 3 else None
        print(json.dumps(ingest_photo(seller, photo, dept, scat), indent=2))
    else:
        print("Usage: ./.venv/bin/python intake.py <seller_id> <photo> "
              "[department] [seller_category]")
        print("       intake.py --manual <seller_id> <photo> <department> "
              "\"<title>\" \"<desc>\"")
        print("       intake.py --refine <product_id> \"<change>\"")
        print("       intake.py --set-desc <product_id> \"<title>\" \"<desc>\"")
        raise SystemExit(1)
