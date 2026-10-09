#!/usr/bin/env python3
"""Token-free tests for conversational self-service onboarding (bot.py, Task 3)."""
import os
import sqlite3
import tempfile

import bot
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


print("── onboarding: first message starts the flow, asks for shop name ──")
db = make_db()
reply = bot.handle_message(None, "/start", telegram_user_id=1111, db_path=db)
check("asks for shop name", "shop name" in reply.lower())
check("not yet linked", telegram_link.resolve_seller(1111, db_path=db) is None)

print("── onboarding: three-question flow completes and creates a real shop ──")
bot.handle_message(None, "anything", telegram_user_id=2222, db_path=db)   # starts flow
r2 = bot.handle_message(None, "My Cool Shop", telegram_user_id=2222, db_path=db)
check("asks for contact next", "phone" in r2.lower() or "contact" in r2.lower())
r3 = bot.handle_message(None, "9876543210", telegram_user_id=2222, db_path=db)
check("asks for pincode next", "pin" in r3.lower())
r4 = bot.handle_message(None, "700001", telegram_user_id=2222, db_path=db)
check("confirms shop creation", "shop" in r4.lower())
seller_id = telegram_link.resolve_seller(2222, db_path=db)
check("telegram user is now linked to a real seller", seller_id is not None)
conn = sqlite3.connect(db)
row = conn.execute("SELECT name, contact, pincode, seller_type FROM sellers WHERE id=?",
                    (seller_id,)).fetchone()
check("seller row has the collected answers", row == ("My Cool Shop", "9876543210", "700001", "bot_seller"))
check("login/password shown in the final reply", "login" in r4.lower() or "shop" + str(seller_id) in r4)

print("── onboarding: 'skip' leaves pincode blank ──")
bot.handle_message(None, "/start", telegram_user_id=3333, db_path=db)
bot.handle_message(None, "Shop Three", telegram_user_id=3333, db_path=db)
bot.handle_message(None, "111", telegram_user_id=3333, db_path=db)
bot.handle_message(None, "skip", telegram_user_id=3333, db_path=db)
seller_id3 = telegram_link.resolve_seller(3333, db_path=db)
pincode = conn.execute("SELECT pincode FROM sellers WHERE id=?", (seller_id3,)).fetchone()[0]
check("pincode left blank on skip", pincode is None)

print("── onboarding: state survives across separate handle_message calls (resumable) ──")
bot.handle_message(None, "/start", telegram_user_id=4444, db_path=db)
bot.handle_message(None, "Resume Shop", telegram_user_id=4444, db_path=db)
# simulate the user closing the app and coming back later -- new call, same telegram_user_id
r_resume = bot.handle_message(None, "9999999999", telegram_user_id=4444, db_path=db)
check("resumed at the pincode step, not restarted", "pin" in r_resume.lower())

print("── handle_message requires telegram_user_id when seller_id is None ──")
try:
    bot.handle_message(None, "hi", db_path=db)
    check("raises without telegram_user_id", False)
except (ValueError, TypeError):
    check("raises without telegram_user_id", True)

print("── existing linked-seller behavior is unchanged (regression) ──")
conn.execute("INSERT INTO sellers (id, name, contact) VALUES (99, 'Existing', '1')")
conn.commit()
reply_existing = bot.handle_message(99, "/help", db_path=db)
check("linked seller /help unaffected", reply_existing == bot.HELP_TEXT)

conn.close()
os.unlink(db)
print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
