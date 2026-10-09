#!/usr/bin/env python3
"""
PeerShop — Phase I: seller Telegram control bot (long-polling connector).

Transport only. The brain is bot.handle_message(); identity is telegram_link.
ONE shared PeerShop bot lets each LINKED seller manage ONLY their own store by
chatting in natural language (e.g. "change the price of 'X' to 250").

Deliberate, consented amendment to CLAUDE.md's "Hermes owns Telegram" rule: this
runs PeerShop's OWN long-poller on a SEPARATE bot token (PEERSHOP_TELEGRAM_TOKEN,
a distinct BotFather bot), so it never touches Hermes's poller/token. Two pollers
on the SAME token would 409-conflict — hence a separate token is required.

Stdlib only (urllib, json). Run supervised via run_bot.sh.

CLI:
    python3 telegram_bot.py --getme     # verify token; print the bot's @username
    python3 telegram_bot.py --once      # one getUpdates cycle (test)
    python3 telegram_bot.py             # run the long-poll loop
"""

import json
import os
import sqlite3
import sys
import time
import traceback

import bot
import post_wizard
import telegram_link
import telegram_api
# Re-exported at module level so tests can monkeypatch telegram_bot.<name>.
from telegram_api import get_me, send_message, get_file, download_file

HERE = os.path.dirname(os.path.abspath(__file__))
OFFSET_FILE = os.path.join(HERE, "bot_offset")
PRODUCT_MEDIA = os.path.join(HERE, "media", "products")   # same dir serve.py serves from

# Forwarded-burst staging (Task 4 of the self-service onboarding plan -- see
# .hermes/plans/2026-10-10_000000-peershop-self-service-bot-onboarding.md).
# Separate from PRODUCT_MEDIA: raw forwarded photos are NOT yet real product
# photos -- they only become one after debounce_worker.py + intake.py (Tasks
# 5-6) turn a bundle into an actual pending product.
RAW_INGEST_DB_PATH = telegram_link.DB_PATH
RAW_INGEST_MEDIA = os.path.join(HERE, "media", "raw_ingest")

POLL_TIMEOUT = telegram_api.POLL_TIMEOUT
TURNS_CAP = int(os.environ.get("PEERSHOP_TG_TURNS_CAP", "100"))
MAX_PHOTO_BYTES = telegram_api.MAX_PHOTO_BYTES

_EXT_BY_MIME = {"image/jpeg": ".jpg", "image/png": ".png",
                "image/webp": ".webp", "image/gif": ".gif"}


def get_updates(offset: int, timeout: int = POLL_TIMEOUT) -> list:
    data = telegram_api.api("getUpdates", {"offset": offset, "timeout": timeout,
                                           "allowed_updates": ["message"]})
    return data.get("result", []) if data.get("ok") else []


# ── photo download (for the /post wizard) ─────────────────────────────────────

def _photo_from_message(msg: dict):
    """Return (file_id, ext) for a product photo in this message, else None.
    Prefers a Telegram photo (compressed JPEG); also accepts an image sent as a file."""
    photos = msg.get("photo")
    if isinstance(photos, list) and photos:
        fid = photos[-1].get("file_id")     # last = largest resolution
        if fid:
            return fid, ".jpg"
    doc = msg.get("document")
    if isinstance(doc, dict) and doc.get("file_id"):
        ext = _EXT_BY_MIME.get(doc.get("mime_type", ""))
        if ext:
            return doc["file_id"], ext
    return None


def _save_product_photo(seller_id: int, data: bytes, ext: str) -> str:
    """Persist a downloaded photo to media/products (same layout as web uploads)."""
    os.makedirs(PRODUCT_MEDIA, exist_ok=True)
    path = os.path.join(PRODUCT_MEDIA, f"seller{seller_id}_{os.urandom(6).hex()}{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return path


def _handle_incoming_photo(seller_id: int, photo, draft: dict) -> str:
    """Download + record a photo during the wizard's photo step."""
    if draft["step"] != "photos":
        return (f"I'm not expecting a photo right now (current step: {draft['step']}). "
                "Answer the question, or /cancel to stop.")
    file_id, ext = photo
    try:
        info = get_file(file_id)
        fpath = info.get("file_path")
        if not fpath:
            raise RuntimeError("getFile returned no file_path")
        saved = _save_product_photo(seller_id, download_file(fpath), ext)
    except Exception as e:
        print(f"photo download failed for seller {seller_id}: {e}", file=sys.stderr)
        return "Sorry, I couldn't download that photo — please send it again."
    return post_wizard.add_photo(seller_id, saved)["reply"]


_LINK_HELP = ("To manage your store here, link your account: log in at the "
              "PeerShop website → dashboard → “Link Telegram”, then tap the button "
              "(or send /start <code> with the code shown).")


# ── forwarded-burst staging (Task 4) ──────────────────────────────────────────

def _is_forwarded(msg: dict) -> bool:
    """
    True if this message was forwarded from elsewhere (another chat/group/
    channel), as opposed to typed/sent directly by the seller. Bot API 7.0+
    (current) reports this via forward_origin (a MessageOrigin object);
    forward_from/forward_from_chat are the pre-7.0 equivalents, removed from
    current servers but checked defensively at zero cost in case an older
    Bot API server is ever in the loop.
    """
    return bool(msg.get("forward_origin") or msg.get("forward_from")
                or msg.get("forward_from_chat"))


def _save_raw_ingest_photo(seller_id: int, data: bytes, ext: str) -> str:
    """Persist a forwarded photo to RAW_INGEST_MEDIA (NOT PRODUCT_MEDIA -- this
    is raw staged input, not yet a real product photo; see Tasks 5-6)."""
    os.makedirs(RAW_INGEST_MEDIA, exist_ok=True)
    path = os.path.join(RAW_INGEST_MEDIA, f"seller{seller_id}_{os.urandom(6).hex()}{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return path


def _handle_forwarded_message(seller_id: int, msg: dict, text: str, photo) -> None:
    """
    Stage one forwarded message into bot_raw_ingest as 'unprocessed'. Silent by
    design -- a rapid-fire forwarded burst (the expected real usage: a seller
    dumping a dozen supplier messages at once) must not spam a reply per
    message. debounce_worker.py (Task 5) is what eventually bundles these and
    produces a SINGLE confirmation once a bundle is fully processed.
    """
    media_path = None
    if photo is not None:
        file_id, ext = photo
        try:
            info = get_file(file_id)
            fpath = info.get("file_path")
            if fpath:
                media_path = _save_raw_ingest_photo(seller_id, download_file(fpath), ext)
        except Exception as e:
            # Don't let a bad photo download drop the accompanying text (if any);
            # stage what we have. The bundle processor can flag a missing image.
            print(f"raw_ingest photo download failed for seller {seller_id}: {e}",
                  file=sys.stderr)

    conn = sqlite3.connect(RAW_INGEST_DB_PATH)
    try:
        conn.execute(
            "INSERT INTO bot_raw_ingest (seller_id, media_path, raw_text, status) "
            "VALUES (?, ?, ?, 'unprocessed')",
            (seller_id, media_path, text or None))
        conn.commit()
    finally:
        conn.close()


def process_update(update: dict) -> None:
    """
    Handle one Telegram update. Identity is derived SOLELY from the authenticated
    sender id (message.from.id) via telegram_link — never from message text.

    Routing (after identity is resolved):
      1. /start <code>            → link an EXISTING account (works while unlinked)
      1a. bare /start, unlinked   → begin self-service shop onboarding (bot.py)
      1b. anything else, unlinked → continue/begin onboarding (same as 1a)
      1c. forwarded content       → stage into bot_raw_ingest (never the wizard/NL
                                     path below -- see debounce_worker.py, Task 5)
      2. /post [name]             → start/restart the deterministic post wizard
      3. /cancel                  → abort an in-progress wizard
      4. mid-wizard photo / text  → the wizard (post_wizard, NO LLM)
      5. anything else            → bot.handle_message (NL + other slash commands)
    """
    msg = update.get("message")
    if not isinstance(msg, dict):
        return                                  # ignore edits/callbacks/etc.
    chat_id = (msg.get("chat") or {}).get("id")
    from_id = (msg.get("from") or {}).get("id")
    if chat_id is None or from_id is None:
        return
    text = (msg.get("text") or "").strip()
    photo = _photo_from_message(msg)
    low = text.lower()

    # 1. Linking: /start <code> binds this Telegram user to an EXISTING seller
    #    (auth in the code itself). A BARE /start (no code) has nothing to
    #    redeem -- route it through the same unlinked-user handling as 1b/1c
    #    below instead of showing a confusing "invalid code" message for a
    #    user who never had a code to begin with.
    if text and low.split(maxsplit=1)[0] == "/start":
        parts = text.split(maxsplit=1)
        code = parts[1].strip() if len(parts) > 1 else ""
        if code:
            sid = telegram_link.redeem_link_code(code, from_id)
            if sid:
                send_message(chat_id, f"Linked to {telegram_link.seller_display(sid)} ✓ "
                                      "You can now manage your store here — try /help.")
                send_message(chat_id, bot.TUTORIAL_TEXT)
            else:
                send_message(chat_id, "That link code is missing, invalid, or expired. "
                                      + _LINK_HELP)
            return
        # Bare /start: if already linked, a status note (not a fresh onboarding
        # restart); otherwise fall through to onboarding below.
        existing_sid = telegram_link.resolve_seller(from_id)
        if existing_sid is not None:
            send_message(chat_id, f"You're linked to {telegram_link.seller_display(existing_sid)}. "
                                  "Try /help to see what you can do.")
            return
        # else: fall through — unlinked bare /start is handled identically to
        # 1c below (onboarding's own first-message branch asks for a shop name).

    # Every other message: act ONLY as the bound seller, OR run the self-service
    # onboarding flow if this Telegram user has no account yet. Onboarding is
    # text-only in v1 (no photo step), so a photo from an unlinked user is
    # politely deferred rather than silently dropped.
    seller_id = telegram_link.resolve_seller(from_id)
    if seller_id is None:
        if photo is not None and not text:
            send_message(chat_id, "Let's finish setting up your shop first — "
                                  + bot.handle_message(None, "", telegram_user_id=from_id))
        else:
            send_message(chat_id, bot.handle_message(None, text, telegram_user_id=from_id))
        return

    # 1b. Forwarded content -> the burst staging queue, NEVER the wizard/NL
    #     dispatch below (a forward is raw supplier data, not the seller
    #     answering a wizard question or chatting with the bot).
    if _is_forwarded(msg):
        _handle_forwarded_message(seller_id, msg, text, photo)
        return

    # 2. Start/restart the post wizard (deterministic; an optional name may follow).
    if low == "/post" or low.startswith("/post "):
        reply = post_wizard.start(seller_id)
        rest = text.split(maxsplit=1)
        if len(rest) > 1 and rest[1].strip() and post_wizard.active_draft(seller_id):
            reply = post_wizard.handle_text(seller_id, rest[1].strip())["reply"]
        send_message(chat_id, reply)
        return

    draft = post_wizard.active_draft(seller_id)

    # 3. Cancel an in-progress wizard.
    if low == "/cancel":
        if draft:
            post_wizard.cancel(seller_id)
            send_message(chat_id, "Cancelled — nothing was posted.")
        else:
            send_message(chat_id, "Nothing to cancel. Send /post to list a product.")
        return

    # 4. Mid-wizard: both photos and text belong to the wizard (no LLM, no allowance).
    if draft:
        if photo is not None:
            send_message(chat_id, _handle_incoming_photo(seller_id, photo, draft))
        elif text:
            send_message(chat_id, post_wizard.handle_text(seller_id, text)["reply"])
        else:
            send_message(chat_id, "Please answer the question, send a photo, or /cancel.")
        return

    # 5. Not in a wizard.
    if photo is not None:
        send_message(chat_id, "To list a product with photos, send /post first.")
        return
    if not text:
        send_message(chat_id, "Send a text instruction, or /post to list a product. "
                              "Try /help.")
        return
    send_message(chat_id, bot.handle_message(seller_id, text, turns_cap=TURNS_CAP))


def _read_offset() -> int:
    try:
        return int(open(OFFSET_FILE).read().strip() or "0")
    except (OSError, ValueError):
        return 0


def _write_offset(offset: int) -> None:
    try:
        with open(OFFSET_FILE, "w") as f:
            f.write(str(offset))
    except OSError as e:
        print(f"offset write failed: {e}", file=sys.stderr)


def main() -> None:
    print(f"PeerShop Telegram bot polling (turns_cap={TURNS_CAP}). Ctrl-C to stop.")
    offset = _read_offset()
    while True:
        try:
            updates = get_updates(offset)
        except Exception as e:
            print(f"getUpdates error: {e}", file=sys.stderr)
            time.sleep(3)
            continue
        for u in updates:
            try:
                process_update(u)
            except Exception:
                traceback.print_exc()          # one bad update can't kill the loop
            offset = u.get("update_id", offset - 1) + 1
            _write_offset(offset)               # advance even on handler error


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--getme":
        me = get_me()
        print(json.dumps(me, indent=2))
        if me.get("username"):
            print(f"\nSet PEERSHOP_TELEGRAM_BOT_USERNAME={me['username']} for dashboard links.")
    elif arg == "--once":
        offset = _read_offset()
        ups = get_updates(offset, timeout=0)
        print(f"got {len(ups)} update(s)")
        for u in ups:
            process_update(u)
            offset = u.get("update_id", offset - 1) + 1
            _write_offset(offset)
    else:
        main()
