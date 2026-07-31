#!/usr/bin/env python3
"""
PeerShop — Phase C: seller onboarding.

A seller signs up, (optionally) uploads shop/storefront photo(s), and is tagged
with one or MORE of the five departments. Two symmetric ways to pick departments
— AI is never the only way in (CLAUDE.md):

  * AI path     : suggest_departments() sends the shop photo(s) to the QUALITY
                  multimodal model (gemini-3.1-pro-preview — a deliberate
                  quality-over-cost choice, once per seller) and returns the
                  suggested department(s). The seller then CONFIRMS/adjusts, and
                  the confirmed list is applied via set_departments().
  * Manual path : the seller self-selects department(s) directly — no AI call.
                  set_departments() (or onboard(departments=[...])) applies them.

Shop photos are PERSISTED to disk and recorded in `seller_photos` (NOT discarded
after classification) — Phase E's profile page displays them.

Governance note: departments are a FIXED closed list of five. An unknown
department name is rejected outright (ValueError), NOT queued — the pending
queue is only for categories/subcategories.

Core is stdlib-only. The one multimodal call reuses describe.py's google.genai
client (project venv); run those paths with ./.venv/bin/python.

CLI:
    python3 onboarding.py --create "Anita Stores" "+91..." grocery,stationery
    python3 onboarding.py --profile 1
    ./.venv/bin/python onboarding.py --suggest shop1.jpg shop2.jpg
"""

import os
import re
import shutil
import sqlite3
import sys

import llm  # free-tier text call for the "Others" genre → department matcher

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "peershop.db")
MEDIA_DIR = os.path.join(HERE, "media", "seller_photos")

# India PIN code: exactly 6 digits, first digit 1–9 (0 is not a valid region).
_PINCODE_RE = re.compile(r"^[1-9][0-9]{5}$")


def normalize_pincode(value: str | None) -> str | None:
    """Return a validated 6-digit PIN, or None if blank. Raises ValueError on a
    non-blank value that isn't a valid India PIN (used by signup/dashboard/onboard)."""
    s = (value or "").strip()
    if not s:
        return None
    if not _PINCODE_RE.match(s):
        raise ValueError("pincode must be 6 digits")
    return s


def set_pincode(seller_id: int, pincode: str | None, db_path: str = DB_PATH) -> str | None:
    """Set/clear a seller's PIN (dashboard use). Validates via normalize_pincode."""
    pin = normalize_pincode(pincode)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE sellers SET pincode = ? WHERE id = ?", (pin, seller_id))
        conn.commit()
    finally:
        conn.close()
    return pin

# System bucket departments (Phase H) — real rows, but NOT launch/pickable.
#   others        : seller proposed a new genre, pending admin approval (public "Others")
#   uncategorized : seller doesn't know their line yet (placeholder)
SYSTEM_DEPARTMENTS = ("others", "uncategorized")
_MATCH_THRESHOLD = 80          # semantic-confidence gate for "Others" → a launch dept

# Quality multimodal model — once per seller, so correctness > per-call price
# (the inverse of the cheap per-product image-id model). Overridable for tests.
SHOP_MODEL = os.environ.get("PEERSHOP_SHOP_MODEL", "gemini-3.1-pro-preview")
_TIMEOUT = 110


# ── departments (the fixed closed list of five) ─────────────────────────────

def _departments(conn) -> dict:
    """{lowercase name -> id} for the five seeded departments."""
    return {r[1].lower(): r[0]
            for r in conn.execute("SELECT id, name FROM departments;")}


def get_departments(db_path: str = DB_PATH) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [r[0] for r in conn.execute(
            "SELECT name FROM departments ORDER BY id;")]
    finally:
        conn.close()


def launch_departments(db_path: str = DB_PATH) -> list[str]:
    """The pickable launch departments — everything except the system buckets."""
    return [d for d in get_departments(db_path) if d not in SYSTEM_DEPARTMENTS]


# ── seller + shop photos ─────────────────────────────────────────────────────

def create_seller(name: str, contact: str, owner_name: str | None = None,
                  meesho_store_url: str | None = None, pincode: str | None = None,
                  db_path: str = DB_PATH) -> int:
    """Create a seller row (departments are attached separately). Returns its id."""
    if not name or not name.strip():
        raise ValueError("seller name is required")
    if not contact or not contact.strip():
        raise ValueError("seller contact is required")
    pin = normalize_pincode(pincode)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        cur = conn.execute(
            "INSERT INTO sellers (name, owner_name, contact, meesho_store_url, pincode) "
            "VALUES (?, ?, ?, ?, ?)",
            (name.strip(), (owner_name or "").strip() or None, contact.strip(),
             (meesho_store_url or "").strip() or None, pin))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def add_shop_photo(seller_id: int, source_path: str,
                   db_path: str = DB_PATH) -> str:
    """
    Persist one shop photo to disk and record it in seller_photos. Returns the
    stored path. The file is COPIED into media/ so it survives independently of
    wherever the intake handoff dropped it.
    """
    if not os.path.exists(source_path):
        raise FileNotFoundError(source_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        if conn.execute("SELECT 1 FROM sellers WHERE id = ?",
                        (seller_id,)).fetchone() is None:
            raise ValueError(f"no seller with id={seller_id}")
        n = conn.execute("SELECT COUNT(*) FROM seller_photos WHERE seller_id = ?",
                        (seller_id,)).fetchone()[0]
        os.makedirs(MEDIA_DIR, exist_ok=True)
        ext = os.path.splitext(source_path)[1] or ".jpg"
        dest = os.path.join(MEDIA_DIR, f"seller_{seller_id}_{n + 1}{ext}")
        shutil.copy2(source_path, dest)
        conn.execute("INSERT INTO seller_photos (seller_id, image_path) VALUES (?, ?)",
                     (seller_id, dest))
        conn.commit()
        return dest
    finally:
        conn.close()


def get_shop_photos(seller_id: int, db_path: str = DB_PATH) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [r[0] for r in conn.execute(
            "SELECT image_path FROM seller_photos WHERE seller_id = ? ORDER BY id;",
            (seller_id,))]
    finally:
        conn.close()


# ── AI department suggestion (quality multimodal — needs the venv) ───────────

def _match_departments(text: str, valid: list[str]) -> list[str]:
    """
    Keep only real department names mentioned, in order of first appearance.
    The five names are single words, so a word-boundary scan robustly handles
    comma lists, "and"-joined, or prose replies without false hits (e.g. 'food'
    won't match inside 'seafood'). Off-list words and 'none' yield [].
    """
    import re
    lowered = (text or "").lower()
    hits = []
    for disp in valid:
        m = re.search(rf"\b{re.escape(disp.lower())}\b", lowered)
        if m:
            hits.append((m.start(), disp))
    return [d for _, d in sorted(hits)]


def suggest_departments(photo_paths: list[str],
                        db_path: str = DB_PATH) -> list[str]:
    """
    ONE quality multimodal call over the seller's shop photo(s) → suggested
    department(s), validated against the closed five. Returns [] if none clearly
    apply. The result is only a SUGGESTION — the seller must confirm before it is
    applied via set_departments().
    """
    valid = launch_departments(db_path)   # never suggest the system buckets
    if not photo_paths:
        return []

    from google.genai import types
    import describe                     # reuse its configured google.genai client
    client = describe._client()

    prompt = (
        "These are photo(s) of one neighbourhood shop / storefront. Which of "
        "these marketplace departments does it sell in? Choose ALL that clearly "
        "apply.\n"
        f"Departments: {', '.join(valid)}.\n"
        "Reply with only a comma-separated list of matching department names, "
        "lowercase, nothing else. If none clearly apply, reply: none"
    )
    parts = [prompt]
    for p in photo_paths:
        mime = "image/png" if p.lower().endswith(".png") else "image/jpeg"
        with open(p, "rb") as f:
            parts.append(types.Part.from_bytes(data=f.read(), mime_type=mime))

    resp = client.models.generate_content(model=SHOP_MODEL, contents=parts)
    return _match_departments(resp.text, valid)


def propose_departments(photo_paths: list[str], db_path: str = DB_PATH) -> dict:
    """
    The onboarding step that decides what to show the seller next — so an empty
    or failed shop-photo classification NEVER dead-ends them with zero departments.

    Returns:
      {"suggestions": [...],          # AI's guess (possibly empty)
       "needs_manual": bool,          # True -> caller MUST ask for self-select
       "reason": "ok"|"no_photos"|"no_match"|"error:<Type>",
       "prompt": str,                 # ready-to-show message for the seller
       "all_departments": [...]}      # the full pick list for manual selection

    - suggestions present  -> show them for confirm/adjust (manual still allowed).
    - suggestions empty     -> needs_manual: prompt the seller to self-select.
    A classification error (bad image, API hiccup) is swallowed into needs_manual
    rather than raised — a failed photo becomes a manual prompt, not a crash.
    Either branch ends in set_departments() with a non-empty confirmed list.
    """
    all_depts = launch_departments(db_path)   # manual pick list excludes buckets
    if not photo_paths:
        suggestions, reason = [], "no_photos"
    else:
        try:
            suggestions = suggest_departments(photo_paths, db_path)
            reason = "ok" if suggestions else "no_match"
        except Exception as e:
            suggestions, reason = [], f"error:{type(e).__name__}"

    needs_manual = not suggestions
    if needs_manual:
        why = ("You didn't send a shop photo" if reason == "no_photos"
               else "We couldn't confidently read your shop's department from the "
                    "photo(s)")
        prompt = (f"{why}. Please choose your department(s) — you can pick more "
                  f"than one: {', '.join(all_depts)}.")
    else:
        prompt = ("Based on your shop photo(s), we suggest: "
                  f"{', '.join(suggestions)}. Confirm these, or adjust / pick your "
                  f"own from: {', '.join(all_depts)}.")
    return {"suggestions": suggestions, "needs_manual": needs_manual,
            "reason": reason, "prompt": prompt, "all_departments": all_depts}


# ── applying the confirmed department(s) ─────────────────────────────────────

def set_departments(seller_id: int, dept_names: list[str],
                    db_path: str = DB_PATH) -> list[str]:
    """
    Attach the CONFIRMED department(s) to a seller (many-to-many), replacing any
    prior set. Names are validated against the fixed five — an unknown name is
    rejected (NOT queued). A seller needs at least one department.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        if conn.execute("SELECT 1 FROM sellers WHERE id = ?",
                        (seller_id,)).fetchone() is None:
            raise ValueError(f"no seller with id={seller_id}")
        valid = _departments(conn)
        chosen = []
        for name in dept_names:
            key = (name or "").strip().lower()
            if key not in valid:
                raise ValueError(
                    f"unknown department: {name!r} "
                    f"(closed list: {', '.join(sorted(valid))})")
            if key not in chosen:
                chosen.append(key)
        if not chosen:
            raise ValueError("a seller needs at least one department")

        conn.execute("DELETE FROM seller_departments WHERE seller_id = ?",
                     (seller_id,))
        conn.executemany(
            "INSERT INTO seller_departments (seller_id, department_id) VALUES (?, ?)",
            [(seller_id, valid[k]) for k in chosen])
        conn.commit()
        return chosen
    finally:
        conn.close()


def onboard(name: str, contact: str, owner_name: str | None = None,
            photo_paths: list[str] | None = None,
            departments: list[str] | None = None,
            meesho_store_url: str | None = None, pincode: str | None = None,
            db_path: str = DB_PATH) -> dict:
    """
    Create a seller, persist any shop photo(s), and (if given) apply the CONFIRMED
    departments. `departments` is the human-confirmed list — either self-selected
    (manual path) or an AI suggestion the seller already confirmed. Left None on
    the AI path until the seller confirms (caller then calls set_departments).
    """
    seller_id = create_seller(name, contact, owner_name=owner_name,
                              meesho_store_url=meesho_store_url, pincode=pincode,
                              db_path=db_path)
    stored = [add_shop_photo(seller_id, p, db_path) for p in (photo_paths or [])]
    applied = set_departments(seller_id, departments, db_path) if departments else None
    return {"seller_id": seller_id, "photos": stored, "departments": applied}


def get_profile(seller_id: int, db_path: str = DB_PATH) -> dict:
    """Everything Phase E's profile page needs: seller, departments, photo paths."""
    conn = sqlite3.connect(db_path)
    try:
        s = conn.execute(
            "SELECT id, name, owner_name, contact, meesho_store_url "
            "FROM sellers WHERE id = ?", (seller_id,)).fetchone()
        if s is None:
            raise ValueError(f"no seller with id={seller_id}")
        depts = [r[0] for r in conn.execute(
            "SELECT d.name FROM seller_departments sd "
            "JOIN departments d ON d.id = sd.department_id "
            "WHERE sd.seller_id = ? ORDER BY d.id;", (seller_id,))]
        photos = [r[0] for r in conn.execute(
            "SELECT image_path FROM seller_photos WHERE seller_id = ? ORDER BY id;",
            (seller_id,))]
        return {"id": s[0], "name": s[1], "owner_name": s[2], "contact": s[3],
                "meesho_store_url": s[4], "departments": depts, "photos": photos}
    finally:
        conn.close()


# ── "Others" free-text genre → governed department (Phase H) ─────────────────

def _parse_match(content: str, launch: list[str]) -> dict:
    raw = (content or "").strip().lower().strip(" .\"'`")
    if raw.startswith("none"):
        return {"department": None, "confidence": 0}
    dept, _sep, conf = raw.partition("|")
    dept = dept.strip(" .\"'`")
    digits = "".join(ch for ch in conf if ch.isdigit())
    confidence = int(digits) if digits else 100      # bare name ⇒ model is sure
    allowed = {d.lower(): d for d in launch}
    if dept in allowed and confidence >= _MATCH_THRESHOLD:
        return {"department": allowed[dept], "confidence": confidence}
    return {"department": None, "confidence": confidence if dept in allowed else 0}


def match_department(text: str, db_path: str = DB_PATH) -> dict:
    """
    SEMANTICALLY map a seller's free-text product line to ONE launch department
    (one narrow free-tier Groq call). Returns {"department": name|None,
    "confidence": int}; department is set only when the model is >= _MATCH_THRESHOLD
    sure it fits (e.g. "jeans" -> boutique). Anything else -> None (→ 'others').
    """
    text = (text or "").strip()
    launch = launch_departments(db_path)
    if not text:
        return {"department": None, "confidence": 0}
    # Show each department WITH its categories, so the match respects the curated
    # grouping (e.g. a washing machine -> home & kitchen via 'appliances', not
    # electronics) rather than the model's own generic word associations.
    conn = sqlite3.connect(db_path)
    try:
        lines = []
        for d in launch:
            cats = [r[0] for r in conn.execute(
                "SELECT c.name FROM categories c JOIN departments dep "
                "ON dep.id = c.department_id WHERE dep.name = ? ORDER BY c.name", (d,))]
            lines.append(f"- {d}: {', '.join(cats)}" if cats else f"- {d}")
    finally:
        conn.close()
    prompt = (
        f'A neighbourhood shop sells this line of products: "{text}".\n'
        "Which ONE of these marketplace departments does it belong under? Decide "
        "using the kinds of products listed under each department:\n"
        + "\n".join(lines) + "\n"
        "If it clearly fits one, reply exactly `<department>|<confidence 0-100>` "
        "using the department name EXACTLY as written above. If it does not clearly "
        "fit ANY of them, reply exactly `none|0`. Lowercase, nothing else."
    )
    try:
        content = llm.complete([{"role": "user", "content": prompt}],
                               max_tokens=12, temperature=0)
    except Exception:
        return {"department": None, "confidence": 0}
    return _parse_match(content, launch)


def request_department(seller_id: int, proposed_name: str,
                       context: str | None = None, db_path: str = DB_PATH) -> int:
    """Queue a seller-proposed new genre for admin approval. Returns the row id."""
    name = (proposed_name or "").strip()
    if not name:
        raise ValueError("proposed genre is required")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        cur = conn.execute(
            "INSERT INTO pending_departments (proposed_name, proposed_by, context) "
            "VALUES (?, ?, ?)", (name, seller_id, context))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_pending_departments(db_path: str = DB_PATH) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT pd.id, pd.proposed_name, pd.proposed_by, s.name AS seller, "
            "pd.status, pd.created_at FROM pending_departments pd "
            "LEFT JOIN sellers s ON s.id = pd.proposed_by "
            "WHERE pd.status = 'pending' ORDER BY pd.id")]
    finally:
        conn.close()


def approve_department(pending_id: int, db_path: str = DB_PATH) -> dict:
    """
    Admin action: turn a pending genre into a REAL department, then re-tag the
    proposing seller (and their 'others'-bucket products) onto it. Idempotent on
    the department name (won't duplicate if it already exists).
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        row = conn.execute(
            "SELECT proposed_name, proposed_by, status FROM pending_departments "
            "WHERE id = ?", (pending_id,)).fetchone()
        if row is None:
            raise ValueError(f"no pending department with id={pending_id}")
        name, seller_id, status = row[0].strip().lower(), row[1], row[2]
        if status != "pending":
            raise ValueError(f"request already {status}")

        dep = conn.execute("SELECT id FROM departments WHERE name = ?",
                          (name,)).fetchone()
        dept_id = dep[0] if dep else conn.execute(
            "INSERT INTO departments (name) VALUES (?)", (name,)).lastrowid

        others = conn.execute("SELECT id FROM departments WHERE name = 'others'"
                              ).fetchone()[0]
        if seller_id is not None:
            # Move the seller off the 'others' bucket onto the new department.
            conn.execute("DELETE FROM seller_departments "
                         "WHERE seller_id = ? AND department_id = ?",
                         (seller_id, others))
            conn.execute("INSERT OR IGNORE INTO seller_departments "
                         "(seller_id, department_id) VALUES (?, ?)",
                         (seller_id, dept_id))
            conn.execute("UPDATE products SET department_id = ? "
                         "WHERE seller_id = ? AND department_id = ?",
                         (dept_id, seller_id, others))
        conn.execute("UPDATE pending_departments SET status = 'approved', "
                     "decided_at = datetime('now') WHERE id = ?", (pending_id,))
        conn.commit()
        return {"pending_id": pending_id, "department": name, "seller_id": seller_id}
    finally:
        conn.close()


def reject_department(pending_id: int, db_path: str = DB_PATH) -> dict:
    """Admin action: reject a proposed genre (seller stays in 'others')."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE pending_departments SET status = 'rejected', "
            "decided_at = datetime('now') WHERE id = ? AND status = 'pending'",
            (pending_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"no pending department with id={pending_id}")
    finally:
        conn.close()
    return {"pending_id": pending_id, "status": "rejected"}


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--pending-departments":
        import json
        print(json.dumps(list_pending_departments(), indent=2))
    elif args and args[0] == "--approve-dept":
        import json
        print(json.dumps(approve_department(int(args[1])), indent=2))
    elif args and args[0] == "--reject-dept":
        import json
        print(json.dumps(reject_department(int(args[1])), indent=2))
    elif args and args[0] == "--create":
        # --create "Name" "contact" [dept1,dept2,...]
        name = args[1] if len(args) > 1 else ""
        contact = args[2] if len(args) > 2 else ""
        depts = args[3].split(",") if len(args) > 3 and args[3] else None
        import json
        print(json.dumps(onboard(name, contact, departments=depts), indent=2))
    elif args and args[0] == "--profile":
        import json
        print(json.dumps(get_profile(int(args[1])), indent=2))
    elif args and args[0] == "--suggest":
        print(suggest_departments(args[1:]))
    else:
        print(__doc__)
