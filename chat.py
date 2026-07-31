#!/usr/bin/env python3
"""
PeerShop — Phase 4: per-product chat.

A lightweight, standalone customer-facing assistant scoped to ONE product's own
description — NOT a general shop chatbot. Each question is answered by a single
narrow Groq call whose only knowledge is that product's title + description.

Deliberately lean, per CLAUDE.md:
  * Stateless: each call sends ONLY the current question — no accumulated
    conversation history — so we stay inside free-tier tokens-per-minute limits.
  * One bounded call, small max_tokens, low temperature.
  * The model is instructed to answer strictly from the given description and to
    defer to the seller when the answer isn't there — so it can't wander into
    being a general assistant or invent specifics (price, sizes, stock).

Chat is offered only for LIVE products (same visibility rule as the listing);
callers should not expose it for pending/rejected items.

Each answered question is also PERSISTED to chat_logs (product_id, question_text,
created_at) as fuel for the Phase F "customers often ask about X" clustering
nudge. This is persistence ONLY and is separate from the chat call's own
statelessness — logged questions are NEVER fed back into the model; every call
still sees only the current question. Logging is best-effort: a logging failure
never breaks answering.

Usage:
    from chat import answer_question
    answer_question(product_id=1, question="Is this pure silk?")

CLI:
    python3 chat.py <product_id> "your question"
"""

import os
import sqlite3
import sys

import llm

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "peershop.db")

SYSTEM = (
    "You are a concise shop assistant for ONE product. "
    "Answer the customer's question using ONLY the product information provided. "
    "If the information doesn't cover the question (price, exact size, stock, "
    "shipping, other products, anything not stated), say you don't have that "
    "detail and suggest asking the seller. Never invent specifics. "
    "Reply in 1-3 short sentences."
)


class ProductNotAvailable(Exception):
    """Raised when a product doesn't exist or isn't live (so chat is closed)."""


def _live_product(product_id: int, db_path: str) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, title, description FROM products "
            "WHERE id = ? AND status = 'live'",
            (product_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ProductNotAvailable(f"product {product_id} is not available")
    return row


def _log_question(product_id: int, question: str, db_path: str) -> None:
    """
    Persist one customer question to chat_logs (Phase F fuel). Best-effort: a
    logging failure must never break the customer's answer, so errors are
    swallowed. Only reached for live products with a non-empty question.
    """
    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO chat_logs (product_id, question_text) VALUES (?, ?)",
                (product_id, question))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def answer_question(product_id: int, question: str,
                    db_path: str = DB_PATH) -> str:
    """
    Answer a single customer question about one live product.

    Stateless by design: the model sees only this product's title/description
    and this one question — never prior turns.
    """
    question = (question or "").strip()
    if not question:
        return "Please type a question about this product."

    p = _live_product(product_id, db_path)
    # Persist the question (Phase F fuel) BEFORE the model call, so a free-tier
    # hiccup can't lose it. Does not affect the stateless call below.
    _log_question(product_id, question, db_path)
    product_block = (
        f"Product title: {p['title'] or '(none)'}\n"
        f"Product description: {p['description'] or '(none)'}"
    )

    content = llm.complete(
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user",
             "content": f"{product_block}\n\nCustomer question: {question}"},
        ],
        max_tokens=150,     # a couple of sentences; bounded for free-tier TPM
        temperature=0.2,    # steady, low-creativity answers
    )
    return content.strip()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print('Usage: python3 chat.py <product_id> "your question"')
        raise SystemExit(1)
    try:
        print(answer_question(int(sys.argv[1]), sys.argv[2]))
    except ProductNotAvailable as e:
        print(f"[unavailable] {e}")
