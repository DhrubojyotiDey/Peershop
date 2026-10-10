#!/usr/bin/env python3
"""Token-free tests for admin account-removal (admin_actions.py)."""
import os
import sqlite3
import tempfile

import admin_actions
import auth
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


def media_dir():
    return tempfile.mkdtemp()


print("── delete_seller: full cascade wipes every related row ──")
db = make_db()
dept_id = sqlite3.connect(db).execute("SELECT id FROM departments LIMIT 1").fetchone()[0]
seller_id, login, pw = telegram_link.create_shop_with_login(
    telegram_user_id=1111, name="Doomed Shop", contact="999", db_path=db)
conn = sqlite3.connect(db)
conn.execute("PRAGMA foreign_keys = ON;")
conn.execute("INSERT INTO seller_departments (seller_id, department_id) VALUES (?, ?)", (seller_id, dept_id))
prod_id = conn.execute(
    "INSERT INTO products (seller_id, department_id, title, status, image_path) "
    "VALUES (?, ?, 'Test Product', 'pending', '/tmp/fake.jpg')", (seller_id, dept_id)).lastrowid
conn.execute("INSERT INTO product_photos (product_id, image_path, position) VALUES (?, '/tmp/fake.jpg', 0)",
             (prod_id,))
conn.execute("INSERT INTO bot_raw_ingest (seller_id, raw_text, status) VALUES (?, 'leftover burst', 'unprocessed')",
             (seller_id,))
conn.commit()

result = admin_actions.delete_seller(seller_id, db_path=db)
check("reports ok", result["ok"] is True)
check("reports the deleted shop's name", result["name"] == "Doomed Shop")

check("sellers row gone", conn.execute("SELECT 1 FROM sellers WHERE id=?", (seller_id,)).fetchone() is None)
check("seller_credentials gone", conn.execute(
    "SELECT 1 FROM seller_credentials WHERE seller_id=?", (seller_id,)).fetchone() is None)
check("seller_telegram gone", conn.execute(
    "SELECT 1 FROM seller_telegram WHERE seller_id=?", (seller_id,)).fetchone() is None)
check("seller_departments gone", conn.execute(
    "SELECT 1 FROM seller_departments WHERE seller_id=?", (seller_id,)).fetchone() is None)
check("products gone", conn.execute("SELECT 1 FROM products WHERE id=?", (prod_id,)).fetchone() is None)
check("product_photos gone (cascaded via products)", conn.execute(
    "SELECT 1 FROM product_photos WHERE product_id=?", (prod_id,)).fetchone() is None)
check("bot_raw_ingest gone", conn.execute(
    "SELECT 1 FROM bot_raw_ingest WHERE seller_id=?", (seller_id,)).fetchone() is None)
check("login no longer verifies", auth.verify_login(login, pw, db_path=db) is None)

print("── delete_seller: ALSO clears bot_onboarding_sessions for the freed telegram id ──")
# Simulate a stray leftover onboarding session for the same telegram_user_id
# (defensive cleanup -- this table has no FK to sellers, so it would NOT
# cascade on its own; delete_seller must clear it explicitly).
conn.execute("INSERT INTO bot_onboarding_sessions (telegram_user_id, step, collected_json) "
             "VALUES (1111, 'awaiting_contact', '{}')")
conn.commit()
seller_id2, _, _ = telegram_link.create_shop_with_login(
    telegram_user_id=2222, name="Second Doomed Shop", contact="888", db_path=db)
admin_actions.delete_seller(seller_id2, db_path=db)
# (seller_id2 was never bound to 1111 -- this just confirms delete_seller doesn't
#  touch unrelated onboarding sessions it has no business touching)
check("unrelated onboarding session for a DIFFERENT telegram id is untouched",
      conn.execute("SELECT 1 FROM bot_onboarding_sessions WHERE telegram_user_id=1111").fetchone() is not None)

print("── delete_seller: the SAME telegram account can set up a brand-new shop afterward ──")
seller_id3, login3, pw3 = telegram_link.create_shop_with_login(
    telegram_user_id=1111, name="Reused Account Shop", contact="777", db_path=db)
check("telegram id 1111 now bound to a FRESH seller", telegram_link.resolve_seller(1111, db_path=db) == seller_id3)
# Note: SQLite's plain `INTEGER PRIMARY KEY` (no AUTOINCREMENT) reuses a freed
# rowid once the table is empty -- seller_id3 landing on the SAME number as
# the deleted seller_id is expected and fine; what matters is there's no stale
# data bleeding through, which the checks below confirm.
check("no stale data bled through from the deleted seller with the same id",
      conn.execute("SELECT name, contact FROM sellers WHERE id=?", (seller_id3,)).fetchone()
      == ("Reused Account Shop", "777"))
admin_actions.delete_seller(seller_id3, db_path=db)
check("that stray onboarding session (from before) is now ALSO cleared, since "
      "1111 is involved again", conn.execute(
      "SELECT 1 FROM bot_onboarding_sessions WHERE telegram_user_id=1111").fetchone() is None)
# Confirm onboarding genuinely restarts clean (no leaked state from any prior attempt).
reply = bot.handle_message(None, "hi", telegram_user_id=1111, db_path=db)
check("onboarding restarts from scratch, no leaked state", "shop name" in reply.lower())

print("── delete_seller: media files on disk are removed, not just DB rows ──")
mdir = media_dir()
photo_path = os.path.join(mdir, "test_photo.jpg")
with open(photo_path, "wb") as f:
    f.write(b"fake jpeg bytes")
seller_id4, _, _ = telegram_link.create_shop_with_login(
    telegram_user_id=3333, name="Shop With Real Files", contact="666", db_path=db)
conn.execute("INSERT INTO bot_raw_ingest (seller_id, media_path, raw_text, status) "
             "VALUES (?, ?, 'has a photo', 'unprocessed')", (seller_id4, photo_path))
conn.commit()
check("photo exists before deletion", os.path.exists(photo_path))
admin_actions.delete_seller(seller_id4, db_path=db)
check("photo file removed from disk after deletion", not os.path.exists(photo_path))

print("── delete_seller: unknown seller_id is a clean no-op, not a crash ──")
result = admin_actions.delete_seller(999999, db_path=db)
check("reports not ok", result["ok"] is False)

conn.close()
os.unlink(db)
print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
