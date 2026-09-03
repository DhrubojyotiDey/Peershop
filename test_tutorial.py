#!/usr/bin/env python3
"""Token-free tests for the onboarding tutorial: /tutorial command + auto-send on link."""
import os
import sqlite3
import tempfile

import bot
import telegram_bot
import telegram_link

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = os.path.join(HERE, "schema.sql")

ok = 0
fail = 0
def check(name, cond):
    global ok, fail
    if cond:
        ok += 1; print(f"  ✓ {name}")
    else:
        fail += 1; print(f"  ✗ {name}")


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(open(SCHEMA).read())
    conn.execute("INSERT INTO sellers (id, name, contact) VALUES (1, 'Shop1', '9990001111')")
    conn.commit(); conn.close()
    return path


print("── bot.py: /tutorial is deterministic, no LLM ──")
db = make_db()
reply = bot.handle_message(1, "/tutorial", db_path=db)
check("/tutorial returns the walkthrough", reply == bot.TUTORIAL_TEXT)
check("mentions /post as step 1", "/post" in reply)
check("mentions photos step", "photo" in reply.lower())
check("mentions description step", "description" in reply.lower())
check("mentions /cancel", "/cancel" in reply)
check("/help still mentions /tutorial", "/tutorial" in bot.HELP_TEXT)
os.unlink(db)

print("── telegram_bot: tutorial auto-sent right after a successful /start link ──")
db = make_db()
sent = []
telegram_bot.send_message = lambda cid, txt: sent.append((cid, txt))
telegram_link.redeem_link_code = lambda code, fid: 1 if code == "goodcode" else None
telegram_link.seller_display = lambda sid: "Shop1"

def upd(text):
    return {"message": {"chat": {"id": 55}, "from": {"id": 999}, "text": text}, "update_id": 1}

sent.clear()
telegram_bot.process_update(upd("/start goodcode"))
check("two messages sent on link (confirmation + tutorial)", len(sent) == 2)
check("first message confirms the link", "Linked to" in sent[0][1])
check("second message is the tutorial", sent[1][1] == bot.TUTORIAL_TEXT)

print("── telegram_bot: no tutorial spam on a bad/expired code ──")
sent.clear()
telegram_bot.process_update(upd("/start not-a-real-code"))
check("only one message on failed link", len(sent) == 1)
check("failure message, not the tutorial", "invalid" in sent[0][1].lower())
os.unlink(db)

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
