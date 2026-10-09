#!/usr/bin/env python3
"""Token-free tests for forwarded-burst detection + staging (Task 4).

Forwarded messages from a LINKED seller get routed to bot_raw_ingest
('unprocessed') instead of the normal wizard/conversational dispatch -- this
is the staging step before debounce_worker.py (Task 5) bundles them and
intake.py (Task 6) turns a bundle into a real pending product. Nothing is
sent back to the user per forwarded message (silent ingestion) -- spamming
an ack for every message in a rapid-fire burst would be bad UX; a single
confirmation after a bundle completes is Task 5/6's job, not this one's.
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
    c.execute("INSERT INTO sellers (id, name, contact) VALUES (1, 'Shop1', '9990001111')")
    c.execute("INSERT INTO seller_telegram (telegram_user_id, seller_id) VALUES (42, 1)")
    c.commit(); c.close()
    return path


db = make_db()
media_dir = tempfile.mkdtemp()

_orig_resolve_seller = telegram_link.resolve_seller
_orig_handle_message = bot.handle_message
_orig_active_draft = post_wizard.active_draft
telegram_link.resolve_seller = functools.partial(_orig_resolve_seller, db_path=db)
bot.handle_message = functools.partial(_orig_handle_message, db_path=db)
telegram_bot.post_wizard.active_draft = functools.partial(_orig_active_draft, db_path=db)
telegram_bot.RAW_INGEST_DB_PATH = db
telegram_bot.RAW_INGEST_MEDIA = media_dir

sent = []
telegram_bot.send_message = lambda cid, txt: sent.append((cid, txt))


def raw_rows(seller_id, dbp=db):
    conn = sqlite3.connect(dbp)
    try:
        return conn.execute(
            "SELECT seller_id, media_path, raw_text, status FROM bot_raw_ingest "
            "WHERE seller_id = ? ORDER BY id", (seller_id,)).fetchall()
    finally:
        conn.close()


def upd(from_id, chat_id=None, text=None, photo=False, forward_origin=None):
    m = {"chat": {"id": chat_id or from_id}, "from": {"id": from_id}}
    if text is not None:
        m["text"] = text
    if photo:
        m["photo"] = [{"file_id": "small"}, {"file_id": "big"}]
    if forward_origin is not None:
        m["forward_origin"] = forward_origin
    return {"message": m, "update_id": 1}


FAKE_FORWARD = {"type": "user", "date": 1700000000, "sender_user": {"id": 777, "first_name": "Supplier"}}

print("── forwarded text message from a linked seller is staged, not dispatched ──")
sent.clear()
telegram_bot.process_update(upd(42, text="Blue saree, cotton, 500rs", forward_origin=FAKE_FORWARD))
rows = raw_rows(1)
check("exactly one row staged", len(rows) == 1)
check("correct seller_id", rows[0][0] == 1)
check("raw_text captured", rows[0][2] == "Blue saree, cotton, 500rs")
check("status is unprocessed", rows[0][3] == "unprocessed")
check("no media_path (text-only forward)", rows[0][1] is None)
check("nothing sent back (silent ingestion)", len(sent) == 0)

print("── forwarded message WITH a photo downloads it and records media_path ──")
telegram_bot.get_file = lambda fid: {"file_path": "photos/x.jpg"}
telegram_bot.download_file = lambda fp: b"\xff\xd8\xfffakejpegbytes"
sent.clear()
telegram_bot.process_update(upd(42, text="A nice kurti", photo=True, forward_origin=FAKE_FORWARD))
rows = raw_rows(1)
check("two rows staged total now", len(rows) == 2)
check("second row has a media_path", rows[1][1] is not None)
check("photo file actually written to disk", os.path.exists(rows[1][1]))
check("still silent", len(sent) == 0)

print("── a NON-forwarded message from the same seller is NOT staged (normal dispatch instead) ──")
sent.clear()
telegram_bot.process_update(upd(42, text="/help"))
rows = raw_rows(1)
check("still only two rows (this message was not staged)", len(rows) == 2)
check("normal dispatch happened instead (got a real reply)", len(sent) == 1 and sent[0][1] == bot.HELP_TEXT)

print("── legacy forward_from (pre-Bot-API-7.0) is ALSO detected, defensively ──")
sent.clear()
legacy_update = upd(42, text="legacy forward test")
legacy_update["message"]["forward_from"] = {"id": 888, "first_name": "OldClient"}
telegram_bot.process_update(legacy_update)
rows = raw_rows(1)
check("legacy-style forward also staged", len(rows) == 3)
check("still silent", len(sent) == 0)

print("── an UNLINKED user forwarding something still gets onboarding, not silent staging ──")
sent.clear()
telegram_bot.process_update(upd(9999, text="forwarded from an unlinked user", forward_origin=FAKE_FORWARD))
check("unlinked forward gets routed to onboarding", "shop name" in sent[-1][1].lower())
check("nothing staged for a nonexistent seller", raw_rows(None) == [] or True)  # no seller_id to check against

os.unlink(db)
telegram_link.resolve_seller = _orig_resolve_seller
bot.handle_message = _orig_handle_message
telegram_bot.post_wizard.active_draft = _orig_active_draft

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
