#!/usr/bin/env python3
"""
PeerShop — Phase J: Telegram /post product-upload wizard (deterministic, NO LLM).

A guided, step-by-step flow so a seller can list a product entirely by chatting
the bot — no natural-language parsing, no Gemini/Groq call anywhere in here:

    /post → name → [department, only if the seller sells in >1] → price →
    up to 4 photos → description → post_product() (status='pending')

State is one row per seller in `post_drafts` (survives a bot restart) and is fully
decoupled from the daily conversational allowance (conversation.py), so the wizard
ALWAYS works — even once the seller's NL window is spent. A fresh /post overwrites
any in-progress draft; the row is deleted on finalize or /cancel.

Photos are downloaded + saved to disk by the transport layer (telegram_bot.py);
this module only records their paths and, on finalize, writes them to
`product_photos` (position 0 mirrored into products.image_path via post_product).

Everything routes through seller_actions.post_product, so ownership + governance
(always 'pending', never bypassing admin review) are unchanged.

Stdlib only.
"""

import json
import os
import sqlite3

import seller_actions
from seller_actions import SellerActionError

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "peershop.db")

MAX_PHOTOS = 4
_STEPS = ("name", "department", "price", "photos", "description")

# Wizard-control tokens the seller may type; anything else at a text step is data.
_CONTROLS = {"/done", "/skip", "/cancel", "/post"}


# ── draft row access ──────────────────────────────────────────────────────────

def _row_to_draft(row) -> dict | None:
    if row is None:
        return None
    return {"seller_id": row[0], "step": row[1], "title": row[2],
            "department": row[3], "price": row[4],
            "photos": json.loads(row[5] or "[]")}


def active_draft(seller_id: int, db_path: str | None = None) -> dict | None:
    """The seller's in-progress draft, or None. Lets transport know we're mid-wizard."""
    db_path = db_path or DB_PATH
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT seller_id, step, title, department, price, photos "
            "FROM post_drafts WHERE seller_id = ?", (seller_id,)).fetchone()
        return _row_to_draft(row)
    finally:
        conn.close()


def _save(seller_id: int, conn, **fields) -> None:
    """Update the given columns on the seller's draft (photos passed as a list)."""
    if "photos" in fields:
        fields["photos"] = json.dumps(fields["photos"])
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE post_drafts SET {cols}, updated_at = datetime('now') "
        "WHERE seller_id = ?", (*fields.values(), seller_id))
    conn.commit()


def cancel(seller_id: int, db_path: str | None = None) -> bool:
    """Abort any in-progress draft. Returns True if one existed."""
    db_path = db_path or DB_PATH
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        cur = conn.execute("DELETE FROM post_drafts WHERE seller_id = ?", (seller_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── departments (deterministic; mirrors seller_actions._resolve_department order) ─

def _seller_departments(conn, seller_id: int) -> list[tuple]:
    return conn.execute(
        "SELECT d.id, d.name FROM seller_departments sd "
        "JOIN departments d ON d.id = sd.department_id "
        "WHERE sd.seller_id = ? ORDER BY d.id;", (seller_id,)).fetchall()


def _dept_prompt(title: str, depts: list[tuple]) -> str:
    lines = "\n".join(f"  {i}) {name.title()}" for i, (_, name) in enumerate(depts, 1))
    return (f"Which department is “{title}” in?\n{lines}\n\n"
            "Reply with the number.")


# ── entry: /post ──────────────────────────────────────────────────────────────

def start(seller_id: int, db_path: str | None = None) -> str:
    """Begin (or restart) the wizard. Returns the first prompt."""
    db_path = db_path or DB_PATH
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        if conn.execute("SELECT 1 FROM sellers WHERE id = ?",
                        (seller_id,)).fetchone() is None:
            return "I can't find your seller account — try re-linking from the website."
        if not _seller_departments(conn, seller_id):
            return ("You have no departments yet — finish onboarding on the website "
                    "before listing products.")
        conn.execute(
            "INSERT INTO post_drafts (seller_id, step, photos) VALUES (?, 'name', '[]') "
            "ON CONFLICT(seller_id) DO UPDATE SET step='name', title=NULL, "
            "department=NULL, price=NULL, photos='[]', updated_at=datetime('now')",
            (seller_id,))
        conn.commit()
    finally:
        conn.close()
    return ("🆕 Let's list a new product.\n"
            "What product would you like to upload? (e.g. \"Denim jeans\", "
            "\"Embroidered saree\")\n\nSend /cancel anytime to stop.")


# ── text steps ────────────────────────────────────────────────────────────────

def handle_text(seller_id: int, text: str, db_path: str | None = None) -> dict:
    """
    Advance the wizard on a text message. Returns {"reply": str, "finalized": bool,
    "product": dict|None}. /cancel and /post are handled by the transport BEFORE
    this is called; /done and /skip are handled here (they're step controls).
    """
    db_path = db_path or DB_PATH
    text = (text or "").strip()
    draft = active_draft(seller_id, db_path)
    if draft is None:                       # not in a wizard (transport guards this)
        return {"reply": "Send /post to start listing a product.", "finalized": False,
                "product": None}

    step = draft["step"]
    low = text.lower()
    # Block stray slash-commands from being swallowed as data (except step controls).
    if text.startswith("/") and low not in _CONTROLS:
        if not (step == "photos" and low == "/done") and low not in ("/done", "/skip"):
            return {"reply": f"You're adding a product (step: {step}). Continue, or "
                             "/cancel to stop.", "finalized": False, "product": None}

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        if step == "name":
            return _step_name(seller_id, conn, text)
        if step == "department":
            return _step_department(seller_id, conn, draft, text)
        if step == "price":
            return _step_price(seller_id, conn, draft, text, low)
        if step == "photos":
            return _step_photos_text(seller_id, conn, draft, low)
        if step == "description":
            return _finalize(seller_id, conn, draft, text, db_path)
    finally:
        conn.close()
    return {"reply": "Something went wrong — send /cancel and try /post again.",
            "finalized": False, "product": None}


def _reply(text: str) -> dict:
    return {"reply": text, "finalized": False, "product": None}


def _step_name(seller_id, conn, text):
    if not text or text.startswith("/"):
        return _reply("Please type the product's name (e.g. \"Denim jeans\").")
    title = text.strip()
    depts = _seller_departments(conn, seller_id)
    if len(depts) == 1:
        _save(seller_id, conn, title=title, department=depts[0][1], step="price")
        return _reply(f"Got it — “{title}”.\nWhat's the price? Send a number in ₹ "
                      "(e.g. 499), or /skip for no price.")
    _save(seller_id, conn, title=title, step="department")
    return _reply(_dept_prompt(title, depts))


def _step_department(seller_id, conn, draft, text):
    depts = _seller_departments(conn, seller_id)
    choice = text.strip().lstrip("#")
    if choice.isdigit() and 1 <= int(choice) <= len(depts):
        name = depts[int(choice) - 1][1]
        _save(seller_id, conn, department=name, step="price")
        return _reply(f"“{draft['title']}” → {name.title()}.\nWhat's the price? Send "
                      "a number in ₹ (e.g. 499), or /skip for no price.")
    return _reply("Please reply with a number from the list:\n"
                  + _dept_prompt(draft["title"], depts))


def _step_price(seller_id, conn, draft, text, low):
    if low == "/skip":
        _save(seller_id, conn, price=None, step="photos")
        return _reply(_photos_prompt(draft["title"]))
    cleaned = text.replace("₹", "").replace(",", "").strip()
    try:
        price = float(cleaned)
        if price < 0:
            raise ValueError
    except ValueError:
        return _reply("That doesn't look like a price. Send a number like 499, "
                      "or /skip for no price.")
    _save(seller_id, conn, price=price, step="photos")
    return _reply(_photos_prompt(draft["title"]))


def _photos_prompt(title: str) -> str:
    return (f"Now send up to {MAX_PHOTOS} photos of “{title}” — one at a time. "
            "Send /done when you're finished (at least one is required).")


def _step_photos_text(seller_id, conn, draft, low):
    n = len(draft["photos"])
    if low == "/done":
        if n == 0:
            return _reply("Please send at least one photo, then /done.")
        _save(seller_id, conn, step="description")
        return _reply(f"Got {n} photo(s). Finally, write a short description of "
                      f"“{draft['title']}”.")
    return _reply("Send a photo of the product, or /done when you've added them all.")


# ── photos (called by transport after it downloads the image) ─────────────────

def add_photo(seller_id: int, image_path: str, db_path: str | None = None) -> dict:
    """
    Record one downloaded photo. Only valid at the 'photos' step. On the 4th photo
    the wizard auto-advances to the description step.
    """
    db_path = db_path or DB_PATH
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        draft = active_draft(seller_id, db_path)
        if draft is None:
            return _reply("Send /post to start listing a product.")
        if draft["step"] != "photos":
            return _reply("I'm not expecting a photo right now. "
                          f"(Current step: {draft['step']}.)")
        photos = draft["photos"]
        if len(photos) >= MAX_PHOTOS:
            _save(seller_id, conn, step="description")
            return _reply(f"You've already added the maximum of {MAX_PHOTOS} photos. "
                          f"Now write a short description of “{draft['title']}”.")
        photos.append(image_path)
        n = len(photos)
        if n >= MAX_PHOTOS:
            _save(seller_id, conn, photos=photos, step="description")
            return _reply(f"📷 Got photo {n}/{MAX_PHOTOS} — that's the max. "
                          f"Now write a short description of “{draft['title']}”.")
        _save(seller_id, conn, photos=photos)
        return _reply(f"📷 Got photo {n}/{MAX_PHOTOS}. Send more, or /done to continue.")
    finally:
        conn.close()


# ── finalize ──────────────────────────────────────────────────────────────────

def _finalize(seller_id, conn, draft, text, db_path) -> dict:
    description = text.strip()
    if not description:
        return _reply("Please write a short description of the product "
                      f"“{draft['title']}”.")
    photos = draft["photos"]
    if not photos:                          # defensive; shouldn't reach here
        _save(seller_id, conn, step="photos")
        return _reply(_photos_prompt(draft["title"]))
    try:
        product = seller_actions.post_product(
            seller_id, draft["title"], description, photos[0],
            department=draft["department"], price=draft["price"],
            photos=photos, db_path=db_path)   # product_photos written centrally
    except SellerActionError as e:
        return _reply(f"Couldn't post it: {e}\nSend /cancel to stop, or try the "
                      "description again.")
    cancel(seller_id, db_path)              # clear the draft

    price_bit = (f"₹{product['price']:.0f}" if product["price"] is not None
                 else "no price set")
    return {"reply": (f"✅ Posted “{product['title']}” (#{product['id']}) — it's now "
                      "pending admin review before it goes live.\n"
                      f"Price: {price_bit} · Photos: {len(photos)} · "
                      f"Department: {product['department'].title()}\n"
                      "Manage it anytime with /mystore."),
            "finalized": True, "product": product}
