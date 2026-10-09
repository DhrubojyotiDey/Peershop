#!/usr/bin/env python3
"""Token-free tests for self-service shop creation (telegram_link.py Tasks 1-2)."""
import os
import sqlite3
import tempfile

import auth
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


print("── create_shop: brand-new seller, no pre-existing account needed ──")
db = make_db()
seller_id = telegram_link.create_shop(
    telegram_user_id=999888777, name="Test Bot Shop", contact="9876543210",
    pincode="700001", db_path=db,
)
conn = sqlite3.connect(db)
row = conn.execute("SELECT name, contact, pincode, seller_type FROM sellers WHERE id = ?",
                    (seller_id,)).fetchone()
check("seller row created with given fields", row[:3] == ("Test Bot Shop", "9876543210", "700001"))
check("seller_type defaults to bot_seller", row[3] == "bot_seller")
bound = conn.execute("SELECT seller_id FROM seller_telegram WHERE telegram_user_id = ?",
                     (999888777,)).fetchone()
check("telegram user immediately bound to the new seller", bound is not None and bound[0] == seller_id)
check("resolve_seller finds it", telegram_link.resolve_seller(999888777, db_path=db) == seller_id)

print("── create_shop: validation ──")
try:
    telegram_link.create_shop(telegram_user_id=1, name="  ", contact="9", db_path=db)
    check("empty name rejected", False)
except ValueError:
    check("empty name rejected", True)
try:
    telegram_link.create_shop(telegram_user_id=1, name="X", contact="  ", db_path=db)
    check("empty contact rejected", False)
except ValueError:
    check("empty contact rejected", True)

print("── create_shop_with_login: auto-generated real web credentials ──")
seller_id2, login, password = telegram_link.create_shop_with_login(
    telegram_user_id=999888778, name="Shop 2", contact="9876500000", db_path=db,
)
check("login verifies against the real auth system", auth.verify_login(login, password, db_path=db) == seller_id2)
check("seller_type is bot_seller here too", conn.execute(
    "SELECT seller_type FROM sellers WHERE id=?", (seller_id2,)).fetchone()[0] == "bot_seller")
check("login follows shop<id> convention", login == f"shop{seller_id2}")

print("── re-running create_shop for the same telegram_user_id creates a SEPARATE new shop ──")
# (by design: create_shop is for NEW shops; re-linking an EXISTING shop still goes
#  through redeem_link_code, unchanged. A second /start-from-scratch simply creates
#  a second shop and re-binds the Telegram id to it -- same upsert semantics as
#  redeem_link_code already has.)
seller_id3 = telegram_link.create_shop(
    telegram_user_id=999888777, name="Second Shop Same Phone", contact="111", db_path=db,
)
check("second shop gets its own id", seller_id3 != seller_id)
check("telegram id now rebound to the NEWEST shop", telegram_link.resolve_seller(999888777, db_path=db) == seller_id3)

conn.close()
os.unlink(db)
print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
