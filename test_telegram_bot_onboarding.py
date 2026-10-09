#!/usr/bin/env python3
"""Token-free tests for telegram_bot.py's unlinked-user routing (Task 3 wiring).

telegram_bot.py's process_update() calls telegram_link.*/bot.handle_message
WITHOUT an explicit db_path (it always targets the real on-disk peershop.db in
production). To test against an isolated temp DB without touching production
data, we wrap each call site telegram_bot.py actually uses with
functools.partial(..., db_path=TEST_DB) -- this is the real, safe way to
redirect a module whose functions default db_path at import time; assigning a
DB_PATH module attribute afterwards would NOT reach already-bound defaults.
"""
import functools
import os
import sqlite3
import tempfile

import bot
import post_wizard
import telegram_bot
import telegram_link

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = os.path.join(HERE, "schema.sql")

ok = 0
fail = 0
def check(name, cond):
    global ok, fail
    if cond: ok += 1; print(f"  ✓ {name}")
    else: fail += 1; print(f"  ✗ {name}")


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    c = sqlite3.connect(path)
    c.executescript(open(SCHEMA).read())
    c.close()
    return path


def upd(from_id, text):
    return {"message": {"chat": {"id": from_id}, "from": {"id": from_id}, "text": text},
            "update_id": 1}


db = make_db()

# Redirect the exact call sites telegram_bot.process_update uses, to the temp DB.
_orig_resolve_seller = telegram_link.resolve_seller
_orig_redeem_link_code = telegram_link.redeem_link_code
_orig_seller_display = telegram_link.seller_display
_orig_handle_message = bot.handle_message
_orig_active_draft = post_wizard.active_draft
telegram_link.resolve_seller = functools.partial(_orig_resolve_seller, db_path=db)
telegram_link.redeem_link_code = functools.partial(_orig_redeem_link_code, db_path=db)
telegram_link.seller_display = functools.partial(_orig_seller_display, db_path=db)
bot.handle_message = functools.partial(_orig_handle_message, db_path=db)
telegram_bot.post_wizard.active_draft = functools.partial(_orig_active_draft, db_path=db)

sent = []
telegram_bot.send_message = lambda cid, txt: sent.append((cid, txt))

print("── bare /start from an unlinked user begins onboarding (not the old link-error message) ──")
sent.clear()
telegram_bot.process_update(upd(5001, "/start"))
check("one message sent", len(sent) == 1)
check("asks for shop name, not an error", "shop name" in sent[0][1].lower())
check("does NOT show the old invalid-code message", "invalid" not in sent[0][1].lower())

print("── /start <code> with a real bad code still shows the explicit link-failure message ──")
sent.clear()
telegram_bot.process_update(upd(5002, "/start not-a-real-code"))
check("one message sent", len(sent) == 1)
check("still says invalid/expired (unchanged regression)", "invalid" in sent[0][1].lower())

print("── any plain text from an unlinked user routes into onboarding ──")
sent.clear()
telegram_bot.process_update(upd(5003, "hello"))
check("routed to onboarding (asks shop name), not the old link-help message",
      "shop name" in sent[0][1].lower())

print("── onboarding completes end-to-end through the real transport layer ──")
sent.clear()
telegram_bot.process_update(upd(5004, "hi"))
telegram_bot.process_update(upd(5004, "Transport Test Shop"))
telegram_bot.process_update(upd(5004, "9998887777"))
telegram_bot.process_update(upd(5004, "skip"))
check("four replies sent, one per turn", len(sent) == 4)
check("final reply confirms shop creation", "shop created" in sent[-1][1].lower())
seller_id = telegram_link.resolve_seller(5004)
check("telegram user now linked to a real seller via the full transport path", seller_id is not None)

print("── a linked seller's existing behavior is completely unaffected (regression) ──")
conn = sqlite3.connect(db)
conn.execute("INSERT INTO sellers (id, name, contact) VALUES (777, 'Existing', '1')")
conn.execute("INSERT INTO seller_telegram (telegram_user_id, seller_id) VALUES (6001, 777)")
conn.commit()
sent.clear()
telegram_bot.process_update(upd(6001, "/help"))
check("linked seller still gets normal /help reply", sent[0][1] == bot.HELP_TEXT)

print("── bare /start from an ALREADY-linked seller does not restart onboarding ──")
sent.clear()
telegram_bot.process_update(upd(6001, "/start"))
check("one message, a simple status note, not a new onboarding flow", len(sent) == 1)
check("does not ask for shop name again", "shop name" not in sent[0][1].lower())

conn.close()
os.unlink(db)

# Restore originals (good hygiene, matters if more tests run in the same interpreter).
telegram_link.resolve_seller = _orig_resolve_seller
telegram_link.redeem_link_code = _orig_redeem_link_code
telegram_link.seller_display = _orig_seller_display
bot.handle_message = _orig_handle_message
telegram_bot.post_wizard.active_draft = _orig_active_draft

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
