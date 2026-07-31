#!/usr/bin/env python3
"""
PeerShop — Phase F: Listing Intelligence (batch question clustering).

The product's differentiator, built LAST. It turns repeated customer questions
(persisted in chat_logs by chat.py) into an OPTIONAL seller nudge:
"Customers often ask about X — want to add this to your description?"

Strictly bounded and cost-controlled (CLAUDE.md):
  * A PERIODIC BATCH job — admin-triggered or nightly. NEVER per customer message,
    never real-time. `run_batch()` is the entry point.
  * ONE narrow FREE-TIER (Groq) call PER PRODUCT per run: that product's recent
    questions in → the top 1-2 recurring themes the description does NOT already
    answer, out. No history, no agent loop, small max_tokens.
  * The result is a SUGGESTION only. It's stored as a `pending` nudge; the seller
    decides (apply / dismiss). The listing is NEVER auto-edited by this module.

Governance: nudges cascade-delete with the product (and thus on account close),
exactly like chat_logs. Themes a seller already saw (any status) are not re-nudged,
so a dismissal is respected across nightly runs.

Stdlib + llm.py only (no Gemini; this is a narrow text call).

CLI:
    python3 insights.py --run              # batch over all eligible products
    python3 insights.py --nudges [pid]     # list pending nudges (optionally one product)
    python3 insights.py --dismiss <id>     # seller dismisses a nudge
    python3 insights.py --apply <id>       # seller marks a nudge acted-on (no auto-edit)
"""

import json
import os
import sqlite3
import sys

import llm

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "peershop.db")

MIN_QUESTIONS = 3      # don't cluster fewer than this — not enough signal
RECENT_LIMIT = 50      # cap questions per product → bounded prompt


def recent_questions(conn, product_id: int, limit: int = RECENT_LIMIT) -> list[str]:
    """The most recent customer questions for one product (newest first)."""
    return [r[0] for r in conn.execute(
        "SELECT question_text FROM chat_logs WHERE product_id = ? "
        "ORDER BY id DESC LIMIT ?", (product_id, limit))]


def eligible_products(conn, min_questions: int = MIN_QUESTIONS) -> list[int]:
    """Product ids with enough logged questions to be worth clustering."""
    return [r[0] for r in conn.execute(
        "SELECT product_id FROM chat_logs GROUP BY product_id "
        "HAVING COUNT(*) >= ? ORDER BY product_id", (min_questions,))]


def _parse_themes(content: str) -> list[dict]:
    """Extract a bounded [{theme, suggestion}] list from the model reply."""
    start, end = content.find("["), content.rfind("]") + 1
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(content[start:end])
    except json.JSONDecodeError:
        return []
    out = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        theme = str(item.get("theme", "")).strip()
        if not theme:
            continue
        out.append({"theme": theme[:120],
                    "suggestion": str(item.get("suggestion", "")).strip()[:300]})
    return out[:2]


def _cluster_call(title: str, description: str, questions: list[str]) -> list[dict]:
    """ONE bounded Groq call → top 1-2 uncovered recurring themes."""
    qlist = "\n".join(f"- {q}" for q in questions)
    prompt = (
        "Below is a product listing and recent customer questions about it. "
        "Identify the TOP 1-2 topics customers REPEATEDLY ask about that the "
        "description does NOT already answer. If the description already covers "
        "what people ask, return an empty array.\n\n"
        f"Title: {title or '(none)'}\n"
        f"Description: {description or '(none)'}\n\n"
        f"Recent customer questions:\n{qlist}\n\n"
        'Reply ONLY as a JSON array (max 2 items), each '
        '{"theme": "<short topic>", "suggestion": "<one sentence the seller could '
        'add to the description>"}. No prose, no code fences.'
    )
    content = llm.complete(
        [{"role": "user", "content": prompt}],
        max_tokens=200,      # a couple of short items; bounded for free-tier TPM
        temperature=0,
    )
    return _parse_themes(content)


def cluster_questions(product_id: int, db_path: str = DB_PATH) -> list[dict]:
    """Cluster one product's recent questions into uncovered themes (one AI call)."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT title, description FROM products WHERE id = ?",
                          (product_id,)).fetchone()
        if row is None:
            raise ValueError(f"no product with id={product_id}")
        questions = recent_questions(conn, product_id)
    finally:
        conn.close()
    if len(questions) < MIN_QUESTIONS:
        return []
    return _cluster_call(row[0], row[1], questions)


def run_batch(db_path: str = DB_PATH, min_questions: int = MIN_QUESTIONS) -> dict:
    """
    The periodic batch job: one bounded call per eligible product, storing any
    NEW uncovered themes as pending nudges. Idempotent-ish — a theme already
    surfaced for a product (any status) is not re-created, so re-runs don't spam
    and dismissals stick. Returns a summary.
    """
    conn = sqlite3.connect(db_path)
    try:
        pids = eligible_products(conn, min_questions)
    finally:
        conn.close()

    summary = {"products_processed": 0, "nudges_created": 0, "created": []}
    for pid in pids:
        themes = cluster_questions(pid, db_path)
        summary["products_processed"] += 1
        if not themes:
            continue
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            seen = {r[0].lower() for r in conn.execute(
                "SELECT theme FROM product_nudges WHERE product_id = ?", (pid,))}
            for t in themes:
                if t["theme"].lower() in seen:
                    continue
                conn.execute(
                    "INSERT INTO product_nudges (product_id, theme, suggestion) "
                    "VALUES (?, ?, ?)", (pid, t["theme"], t["suggestion"] or None))
                seen.add(t["theme"].lower())
                summary["nudges_created"] += 1
                summary["created"].append({"product_id": pid, **t})
            conn.commit()
        finally:
            conn.close()
    return summary


def get_nudges(product_id: int | None = None, status: str | None = "pending",
               db_path: str = DB_PATH) -> list[dict]:
    """Retrieve nudges for surfacing to the seller (default: pending)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        sql = ("SELECT id, product_id, theme, suggestion, status, created_at "
               "FROM product_nudges WHERE 1=1")
        params: list = []
        if status:
            sql += " AND status = ?"; params.append(status)
        if product_id:
            sql += " AND product_id = ?"; params.append(product_id)
        sql += " ORDER BY product_id, id"
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def set_nudge_status(nudge_id: int, status: str, db_path: str = DB_PATH) -> str:
    """
    Record the seller's decision on a nudge. 'applied' means the seller CHOSE to
    act on it — it does NOT edit the listing here; the seller updates their own
    description separately (intake.set_manual_description / refine). Never auto-edit.
    """
    if status not in ("pending", "applied", "dismissed"):
        raise ValueError(f"bad status: {status!r}")
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("UPDATE product_nudges SET status = ? WHERE id = ?",
                           (status, nudge_id))
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"no nudge with id={nudge_id}")
    finally:
        conn.close()
    return status


def dismiss_nudge(nudge_id: int, db_path: str = DB_PATH) -> str:
    return set_nudge_status(nudge_id, "dismissed", db_path)


def apply_nudge(nudge_id: int, db_path: str = DB_PATH) -> str:
    return set_nudge_status(nudge_id, "applied", db_path)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--run":
        print(json.dumps(run_batch(), indent=2))
    elif args and args[0] == "--nudges":
        pid = int(args[1]) if len(args) > 1 else None
        for n in get_nudges(pid):
            s = f" — {n['suggestion']}" if n["suggestion"] else ""
            print(f"#{n['id']} [product {n['product_id']}] {n['theme']}{s}")
    elif args and args[0] == "--dismiss":
        print(dismiss_nudge(int(args[1])))
    elif args and args[0] == "--apply":
        print(apply_nudge(int(args[1])))
    else:
        print(__doc__)
