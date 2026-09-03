#!/usr/bin/env python3
"""
PeerShop — Phase G: Telegram seller bot (message handler).

NOT a poller. Hermes owns the Telegram side and hands each seller message to
`handle_message(seller_id, text, image_path=None)`, which returns the reply text
for Hermes to send back. PeerShop never re-polls Telegram.

Two paths, gated by the DAILY conversational allowance (conversation.py):

  * Slash commands — /post /remove /price /mystore /help — ALWAYS available,
    fully deterministic, NO LLM. Also the fallback once the allowance is spent.

  * Natural language — only while a conversational window is active. Gemini parses
    the message (WITH this window's short history) into a STRUCTURED intent. The
    LLM NEVER acts: Python resolves the intent against the seller's OWN products
    (exact match required), asks a clarifying question on ambiguity, and requires
    an explicit yes/no confirmation before any destructive (remove) action.

Every mutation goes through seller_actions.py, so bot posts/edits land
status='pending' — the bot never bypasses admin review.

CLI (manual testing — mimics one Hermes handoff):
    python3 bot.py <seller_id> "<message>" [image_path]
"""

import json
import os
import sys

import conversation
import describe                     # google.genai client (Gemini) — reused
import seller_actions
from seller_actions import SellerActionError

DB_PATH = seller_actions.DB_PATH

# Cheap Gemini model for the narrow, frequent intent parse (per conversational
# turn). Configurable; the call is bounded and history is this-window-only.
INTENT_MODEL = os.environ.get("PEERSHOP_INTENT_MODEL", "gemini-3.1-flash-lite")

_AFFIRM = {"yes", "y", "yeah", "yep", "yup", "confirm", "confirmed", "ok",
           "okay", "sure", "do it", "go ahead", "remove it", "delete it"}
_NEGATE = {"no", "n", "nope", "cancel", "stop", "don't", "dont", "nvm",
           "never mind", "nevermind", "keep it"}

_CMD_SUMMARY = ("/post <title> (with a photo), /remove <id>, "
                "/price <id> <amount>, /mystore, /tutorial, /help")
HELP_TEXT = (
    "PeerShop seller bot — you can just chat with me (e.g. \"change the price of "
    "my red saree to 500\", \"take down the blue kurti\") during your daily "
    "conversational window. Anytime — even when that's used up — these commands "
    "work:\n"
    "  /post              list a product — I'll ask for its name, price, up to 4\n"
    "                     photos, then a description (send /cancel to stop)\n"
    "  /remove <id>       take a product down\n"
    "  /price <id> <amt>  change a product's price\n"
    "  /mystore           see your listings and their ids\n"
    "  /tutorial          walk through how to list a product, step by step\n"
    "  /help              this message\n"
    "Everything you post or edit goes to admin review before it appears publicly."
)

# Deterministic, no-LLM walkthrough of the /post wizard (post_wizard.py's actual
# steps). Sent once automatically right after a seller links their Telegram
# account (telegram_bot.py), and replayable anytime with /tutorial.
TUTORIAL_TEXT = (
    "📖 How to list a product, step by step:\n\n"
    "1️⃣ Send /post to start.\n"
    "2️⃣ Type the product's name (e.g. \"Denim jeans\").\n"
    "3️⃣ If you sell in more than one department, I'll show you a numbered list — "
    "reply with the number.\n"
    "4️⃣ Send the price in ₹ (e.g. 499), or /skip to leave it unset for now.\n"
    "5️⃣ Send up to 4 photos, one at a time. Send /done once you've added them "
    "all (at least 1 photo is required).\n"
    "6️⃣ Write a short description of the product.\n\n"
    "That's it — your product is submitted for admin review and goes live once "
    "approved. You can send /cancel at any point during /post to stop without "
    "posting anything.\n\n"
    "Other handy commands:\n"
    "  /mystore           see your listings and their ids\n"
    "  /price <id> <amt>  change a product's price\n"
    "  /remove <id>       take a product down\n"
    "  /tutorial          show this guide again\n"
    "  /help              full command list"
)


# ── shared formatting / product resolution ──────────────────────────────────

def _fmt_price(p):
    return f"₹{p:.0f}" if p is not None else "no price"


def _format_store(seller_id: int, db_path: str) -> str:
    prods = seller_actions.get_my_products(seller_id, db_path=db_path)
    if not prods:
        return ("Your store is empty. Send a photo with /post <title> to list "
                "something.")
    lines = []
    for p in prods:
        bits = [f"#{p['id']}", p["title"] or "(untitled)", f"— {p['status']}"]
        if p["price"] is not None:
            bits.append(f"— {_fmt_price(p['price'])}")
        if p["category"]:
            bits.append(f"[{p['category']}]")
        lines.append(" ".join(bits))
    return "Your store:\n" + "\n".join(lines)


def _resolve_product(seller_id: int, product_ref, db_path: str):
    """
    Exact-match a seller's product from their own words. Returns (product|None,
    candidates). A single exact id- or title-match → (product, []); otherwise
    (None, <candidates to disambiguate>).
    """
    prods = seller_actions.get_my_products(seller_id, db_path=db_path)
    if product_ref is None or not str(product_ref).strip():
        return None, prods
    ref = str(product_ref).strip().lower()
    if ref.lstrip("#").isdigit():
        m = [p for p in prods if p["id"] == int(ref.lstrip("#"))]
        if len(m) == 1:
            return m[0], []
    exact = [p for p in prods if (p["title"] or "").strip().lower() == ref]
    if len(exact) == 1:
        return exact[0], []
    # No unambiguous exact match → offer substring candidates (or all) to clarify.
    subs = [p for p in prods if ref in (p["title"] or "").lower()]
    return None, (subs or prods)


def _clarify(candidates, action: str) -> str:
    if not candidates:
        return (f"I couldn't find a product in your store to {action}. "
                "Type /mystore to see them.")
    listing = "; ".join(f"#{p['id']} {p['title']}" for p in candidates[:8])
    return f"Which one should I {action}? {listing}"


# ── slash commands (deterministic, no LLM) ──────────────────────────────────

def _handle_command(seller_id: int, text: str, image_path, db_path: str) -> str:
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/help":
        return HELP_TEXT
    if cmd == "/tutorial":
        return TUTORIAL_TEXT
    if cmd == "/mystore":
        return _format_store(seller_id, db_path)
    if cmd == "/post":
        if not image_path:
            return "Send a photo together with /post <title> to list it."
        if not rest:
            return "Usage: /post <title>  (with a photo attached)"
        try:
            r = seller_actions.post_product(
                seller_id, rest, None, image_path, db_path=db_path)
        except SellerActionError as e:
            return str(e)
        return (f"Posted “{r['title']}” (#{r['id']}) — pending admin review before "
                "it goes live. Set a price with /price {0} <amount>.".format(r["id"]))
    if cmd == "/remove":
        if not rest.lstrip("#").isdigit():
            return "Usage: /remove <id>   (see ids with /mystore)"
        try:
            r = seller_actions.remove_product(
                seller_id, int(rest.lstrip("#")), db_path=db_path)
        except SellerActionError as e:
            return str(e)
        return f"Removed “{r['title']}” (#{r['id']}) from your store."
    if cmd == "/price":
        a = rest.split()
        if len(a) < 2 or not a[0].lstrip("#").isdigit():
            return "Usage: /price <id> <amount>"
        try:
            r = seller_actions.update_price(
                seller_id, int(a[0].lstrip("#")), a[1], db_path=db_path)
        except SellerActionError as e:
            return str(e)
        return f"Set “{r['title']}” (#{r['id']}) to {_fmt_price(r['price'])}."
    return f"Unknown command. Try: {_CMD_SUMMARY}"


# ── natural-language path (Gemini intent → Python action) ────────────────────

def _gemini_raw(prompt: str) -> str:
    """One bounded Gemini text call. Factored out so tests can stub it."""
    client = describe._client()
    resp = client.models.generate_content(model=INTENT_MODEL, contents=prompt)
    return (resp.text or "").strip()


def _parse_intent(seller_id: int, window: dict, text: str, db_path: str) -> dict:
    """
    Classify the seller's message into a structured intent using Gemini, with
    THIS window's short history for context. The model only classifies — it never
    acts. On any failure we degrade to 'unclear' rather than raising.
    """
    prods = seller_actions.get_my_products(seller_id, db_path=db_path)
    prod_lines = "\n".join(
        f"#{p['id']} {p['title']} ({_fmt_price(p['price'])})" for p in prods
    ) or "(no products yet)"
    history = conversation.window_history(window["id"], limit=10, db_path=db_path)
    hist_text = "\n".join(f"{m['role']}: {m['content']}" for m in history) or "(none)"

    prompt = (
        "You parse a marketplace seller's message into ONE structured intent. Do "
        "NOT act — only classify.\n"
        "Intents: post (list a new product), remove (take a product down), "
        "edit_price (change a price), list (show their products), unclear.\n\n"
        f"The seller's current products:\n{prod_lines}\n\n"
        f"Recent conversation this session:\n{hist_text}\n\n"
        f"New message: {text}\n\n"
        'Reply ONLY as JSON: {"intent": "post|remove|edit_price|list|unclear", '
        '"product_ref": "<the seller\'s words for which product, or null>", '
        '"price": <number or null>, "title": "<for a new post, or null>", '
        '"department": "<for a new post, or null>", '
        '"reply": "<a short clarifying question, only if unclear, else null>"}. '
        "No prose, no code fences."
    )
    try:
        content = _gemini_raw(prompt)
    except Exception:
        return {"intent": "unclear", "reply":
                "Sorry, I couldn't process that just now — try /help for commands."}
    return _parse_intent_json(content)


def _parse_intent_json(content: str) -> dict:
    start, end = content.find("{"), content.rfind("}") + 1
    data = {}
    if 0 <= start < end:
        try:
            data = json.loads(content[start:end])
        except json.JSONDecodeError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    intent = str(data.get("intent", "unclear")).strip().lower()
    if intent not in ("post", "remove", "edit_price", "list", "unclear"):
        intent = "unclear"
    price = data.get("price")
    if isinstance(price, str):
        try:
            price = float(price)
        except ValueError:
            price = None
    return {"intent": intent, "product_ref": data.get("product_ref"),
            "price": price, "title": data.get("title"),
            "department": data.get("department"), "reply": data.get("reply")}


def _try_post(seller_id, intent, image_path, db_path) -> str:
    if not image_path:
        return ("To post a product, send a photo along with what it is "
                "(and a price if you like).")
    title = (intent.get("title") or "").strip()
    if not title:
        return "Got the photo — what should I call this product?"
    try:
        r = seller_actions.post_product(
            seller_id, title, None, image_path,
            department=intent.get("department"), price=intent.get("price"),
            db_path=db_path)
    except SellerActionError as e:
        return str(e)
    tail = f" at {_fmt_price(r['price'])}" if r["price"] is not None else ""
    return (f"Posted “{r['title']}”{tail} — it's pending admin review before it "
            "goes live.")


def _try_edit_price(seller_id, intent, db_path) -> str:
    prod, candidates = _resolve_product(seller_id, intent.get("product_ref"), db_path)
    if prod is None:
        return _clarify(candidates, "change the price of")
    if intent.get("price") is None:
        return f"What price should I set for “{prod['title']}” (#{prod['id']})?"
    try:
        r = seller_actions.update_price(seller_id, prod["id"], intent["price"],
                                        db_path=db_path)
    except SellerActionError as e:
        return str(e)
    return f"Updated “{r['title']}” (#{r['id']}) to {_fmt_price(r['price'])}."


def _try_remove(seller_id, window, intent, db_path) -> str:
    """Resolve + stage a confirmation. NEVER removes here — needs explicit yes."""
    prod, candidates = _resolve_product(seller_id, intent.get("product_ref"), db_path)
    if prod is None:
        return _clarify(candidates, "remove")
    conversation.set_pending_action(
        window["id"],
        json.dumps({"action": "remove", "product_id": prod["id"],
                    "title": prod["title"]}),
        db_path=db_path)
    return (f"Remove “{prod['title']}” (#{prod['id']})? It'll be hidden from your "
            "store. Reply yes to confirm, or no to cancel.")


def _resolve_pending(seller_id, window, text, db_path) -> str:
    """A confirmation is pending — Python (not the LLM) decides on explicit yes/no."""
    pend = json.loads(window["pending_action"])
    conversation.set_pending_action(window["id"], None, db_path=db_path)  # clear always
    low = text.strip().lower()
    if low in _AFFIRM:
        if pend.get("action") == "remove":
            try:
                seller_actions.remove_product(seller_id, pend["product_id"],
                                              db_path=db_path)
            except SellerActionError as e:
                return str(e)
            return f"Done — “{pend['title']}” has been removed from your store."
        return "Okay."
    if low in _NEGATE:
        return "Okay, cancelled — nothing was changed."
    # Anything other than a clear yes/no cancels the destructive action (safe).
    return ("I didn't get a clear yes or no, so I cancelled that removal — nothing "
            "was changed. Tell me again what you'd like to do.")


def _handle_conversational(seller_id, window, text, image_path, db_path) -> str:
    intent = _parse_intent(seller_id, window, text, db_path)
    kind = intent["intent"]
    if kind == "list":
        return _format_store(seller_id, db_path)
    if kind == "post":
        return _try_post(seller_id, intent, image_path, db_path)
    if kind == "edit_price":
        return _try_edit_price(seller_id, intent, db_path)
    if kind == "remove":
        return _try_remove(seller_id, window, intent, db_path)
    return intent.get("reply") or (
        "I can post a product, remove one, change a price, or show your store. "
        "What would you like to do?")


# ── entry point ──────────────────────────────────────────────────────────────

def handle_message(seller_id: int, text: str, image_path: str | None = None,
                   db_path: str = DB_PATH,
                   turns_cap: int = conversation.DEFAULT_TURNS_CAP) -> str:
    """
    Handle one incoming seller message; return the bot's reply. Slash commands
    are always deterministic; natural language uses the daily conversational
    window (Gemini) or, once that's spent, tells the seller to use commands.
    `turns_cap` sizes the NL daily allowance (the Telegram control path passes a
    higher cap; slash commands are always available regardless).
    """
    text = (text or "").strip()
    if text.startswith("/"):
        return _handle_command(seller_id, text, image_path, db_path)
    if not text:
        return "Send me a message, or /help to see what I can do."

    state = conversation.get_or_open_window(seller_id, turns_cap=turns_cap,
                                            db_path=db_path)
    window = state["window"]
    if window is None:            # allowance spent for today → commands only
        return ("You've used today's conversational allowance. You can still "
                f"manage your store with commands: {_CMD_SUMMARY}")

    if window.get("pending_action"):
        reply = _resolve_pending(seller_id, window, text, db_path)
    else:
        reply = _handle_conversational(seller_id, window, text, image_path, db_path)

    # One handled message = one conversational turn (records history + counts it).
    conversation.record_turn(window["id"], text, reply, db_path=db_path)
    return reply


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print('Usage: python3 bot.py <seller_id> "<message>" [image_path]')
        raise SystemExit(1)
    sid = int(sys.argv[1])
    msg = sys.argv[2]
    img = sys.argv[3] if len(sys.argv) > 3 else None
    print(handle_message(sid, msg, img))
