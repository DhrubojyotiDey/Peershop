#!/usr/bin/env python3
"""Token-free tests for the /post wizard (post_wizard) + telegram_bot routing."""
import json
import os
import sqlite3
import tempfile

import post_wizard
import seller_actions
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
    # two sellers: #1 single-department, #2 multi-department
    conn.execute("INSERT INTO sellers (id, name, contact) "
                 "VALUES (1, 'Shop1', '9990001111'), (2, 'Shop2', '9990002222')")
    depts = [r[0] for r in conn.execute("SELECT id FROM departments ORDER BY id")]
    conn.execute("INSERT INTO seller_departments (seller_id, department_id) VALUES (1, ?)",
                 (depts[0],))
    conn.execute("INSERT INTO seller_departments (seller_id, department_id) VALUES (2, ?), (2, ?)",
                 (depts[0], depts[1]))
    conn.commit(); conn.close()
    return path


print("── post_wizard: single-department happy path ──")
db = make_db()
r = post_wizard.start(1, db_path=db)
check("start → name step", post_wizard.active_draft(1, db_path=db)["step"] == "name")
r = post_wizard.handle_text(1, "Denim jeans", db_path=db)
d = post_wizard.active_draft(1, db_path=db)
check("name saved, single dept auto → price step", d["step"] == "price" and d["title"] == "Denim jeans")
check("dept auto-assigned", d["department"] is not None)
r = post_wizard.handle_text(1, "499", db_path=db)
d = post_wizard.active_draft(1, db_path=db)
check("price parsed → photos step", d["step"] == "photos" and d["price"] == 499.0)
post_wizard.add_photo(1, "/tmp/a.jpg", db_path=db)
r2 = post_wizard.add_photo(1, "/tmp/b.jpg", db_path=db)
check("2 photos recorded", len(post_wizard.active_draft(1, db_path=db)["photos"]) == 2)
check("photo reply shows 2/4", "2/4" in r2["reply"])
post_wizard.handle_text(1, "/done", db_path=db)
check("/done → description step", post_wizard.active_draft(1, db_path=db)["step"] == "description")
res = post_wizard.handle_text(1, "Comfortable blue denim jeans.", db_path=db)
check("finalized", res["finalized"] is True and res["product"] is not None)
pid = res["product"]["id"]
prod = seller_actions.get_product(pid, db_path=db)
check("product is pending", prod["status"] == "pending")
check("primary image_path = first photo", prod["image_path"] == "/tmp/a.jpg")
check("description stored", prod["description"] == "Comfortable blue denim jeans.")
with sqlite3.connect(db) as c:
    rows = c.execute("SELECT image_path, position FROM product_photos WHERE product_id=? ORDER BY position", (pid,)).fetchall()
check("2 product_photos rows in order", rows == [("/tmp/a.jpg", 0), ("/tmp/b.jpg", 1)])
check("draft deleted after finalize", post_wizard.active_draft(1, db_path=db) is None)
os.unlink(db)

print("── post_wizard: multi-department picks a department ──")
db = make_db()
post_wizard.start(2, db_path=db)
r = post_wizard.handle_text(2, "Notebook", db_path=db)
check("multi-dept → department step", post_wizard.active_draft(2, db_path=db)["step"] == "department")
check("prompt lists numbered depts", "1)" in r["reply"] and "2)" in r["reply"])
r = post_wizard.handle_text(2, "banana", db_path=db)   # invalid choice
check("invalid dept choice re-prompts", post_wizard.active_draft(2, db_path=db)["step"] == "department")
r = post_wizard.handle_text(2, "2", db_path=db)
d = post_wizard.active_draft(2, db_path=db)
check("valid dept choice → price step", d["step"] == "price" and d["department"] is not None)
os.unlink(db)

print("── post_wizard: price validation + /skip ──")
db = make_db()
post_wizard.start(1, db_path=db); post_wizard.handle_text(1, "Thing", db_path=db)
r = post_wizard.handle_text(1, "not a number", db_path=db)
check("bad price stays on price step", post_wizard.active_draft(1, db_path=db)["step"] == "price")
r = post_wizard.handle_text(1, "/skip", db_path=db)
d = post_wizard.active_draft(1, db_path=db)
check("/skip → photos, price None", d["step"] == "photos" and d["price"] is None)
r = post_wizard.handle_text(1, "₹1,299", db_path=db)   # currency symbols/commas tolerated after restart
# (already past price; this is a photos-step text now)
check("text during photos nudges", "photo" in r["reply"].lower())
os.unlink(db)

print("── post_wizard: max 4 photos + /done guard ──")
db = make_db()
post_wizard.start(1, db_path=db); post_wizard.handle_text(1, "Thing", db_path=db)
post_wizard.handle_text(1, "10", db_path=db)
r = post_wizard.handle_text(1, "/done", db_path=db)   # 0 photos
check("/done with 0 photos blocked", post_wizard.active_draft(1, db_path=db)["step"] == "photos")
for i in range(4):
    last = post_wizard.add_photo(1, f"/tmp/p{i}.jpg", db_path=db)
check("4th photo auto-advances to description", post_wizard.active_draft(1, db_path=db)["step"] == "description")
extra = post_wizard.add_photo(1, "/tmp/p5.jpg", db_path=db)   # 5th while at description
check("extra photo refused after max", "not expecting" in extra["reply"].lower())
check("still only 4 photos", len(post_wizard.active_draft(1, db_path=db)["photos"]) == 4)
os.unlink(db)

print("── post_wizard: cancel + cross-seller isolation ──")
db = make_db()
post_wizard.start(1, db_path=db)
post_wizard.start(2, db_path=db); post_wizard.handle_text(2, "S2 item", db_path=db)
check("cancel(1) removes only seller 1's draft", post_wizard.cancel(1, db_path=db) is True)
check("seller 1 draft gone", post_wizard.active_draft(1, db_path=db) is None)
check("seller 2 draft intact", post_wizard.active_draft(2, db_path=db) is not None)
os.unlink(db)

print("── seller_actions: multi-photo post + single-photo + replace sync ──")
db = make_db()
r = seller_actions.post_product(1, "MultiPic", "desc", "/p/a.jpg",
                                photos=["/p/a.jpg", "/p/b.jpg", "/p/c.jpg"], db_path=db)
pid = r["id"]
with sqlite3.connect(db) as c:
    ph = c.execute("SELECT image_path, position FROM product_photos WHERE product_id=? ORDER BY position", (pid,)).fetchall()
check("post_product wrote 3 product_photos in order",
      ph == [("/p/a.jpg", 0), ("/p/b.jpg", 1), ("/p/c.jpg", 2)])
check("primary image_path = first photo", r["image_path"] == "/p/a.jpg")
r2 = seller_actions.post_product(1, "Solo", "d", "/p/x.jpg", db_path=db)
with sqlite3.connect(db) as c:
    n = c.execute("SELECT COUNT(*) FROM product_photos WHERE product_id=?", (r2["id"],)).fetchone()[0]
check("single-photo post → 1 product_photos row (consistent)", n == 1)
seller_actions.update_image(1, pid, "/p/new.jpg", db_path=db)
with sqlite3.connect(db) as c:
    ph2 = c.execute("SELECT image_path, position FROM product_photos WHERE product_id=? ORDER BY position", (pid,)).fetchall()
check("replace-photo resets product_photos to the single new image", ph2 == [("/p/new.jpg", 0)])
os.unlink(db)

print("── telegram_bot routing (monkeypatched, no network) ──")
db = make_db()
sent = []
telegram_bot.send_message = lambda cid, txt: sent.append((cid, txt))
# route identity to seller 1; download stubbed
telegram_link.resolve_seller = lambda fid, db_path=None: 1
telegram_bot.get_file = lambda fid: {"file_path": "photos/x.jpg"}
telegram_bot.download_file = lambda fp: b"\xff\xd8\xffbytes"
# point wizard + bot at the temp DB (both read module DB_PATH default; patch it)
post_wizard.DB_PATH = db
telegram_bot.PRODUCT_MEDIA = tempfile.mkdtemp()

def upd(text=None, photo=False):
    m = {"chat": {"id": 55}, "from": {"id": 999}}
    if text is not None:
        m["text"] = text
    if photo:
        m["photo"] = [{"file_id": "small"}, {"file_id": "big"}]
    return {"message": m, "update_id": 1}

sent.clear(); telegram_bot.process_update(upd(text="/post"))
check("/post starts wizard", post_wizard.active_draft(1, db_path=db) is not None and "product" in sent[-1][1].lower())
sent.clear(); telegram_bot.process_update(upd(text="Red kurti"))
check("name routed to wizard (→ price ask)", "price" in sent[-1][1].lower())
sent.clear(); telegram_bot.process_update(upd(text="250"))
check("price routed to wizard (→ photo ask)", "photo" in sent[-1][1].lower())
sent.clear(); telegram_bot.process_update(upd(photo=True))
check("photo downloaded + recorded", len(post_wizard.active_draft(1, db_path=db)["photos"]) == 1
      and "1/4" in sent[-1][1])
sent.clear(); telegram_bot.process_update(upd(text="/done"))
sent.clear(); telegram_bot.process_update(upd(text="A lovely red kurti."))
check("finalized via bot → posted + pending", "pending" in sent[-1][1].lower())
check("draft cleared after bot finalize", post_wizard.active_draft(1, db_path=db) is None)

# unlinked sender is refused
telegram_link.resolve_seller = lambda fid, db_path=None: None
sent.clear(); telegram_bot.process_update(upd(text="/post"))
check("unlinked user cannot start wizard", post_wizard.active_draft(1, db_path=db) is None
      and "link" in sent[-1][1].lower())
os.unlink(db)

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
