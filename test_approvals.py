#!/usr/bin/env python3
"""Token-free tests for approvals.py (the shared approve/reject decision path)."""
import os
import sqlite3
import tempfile

import approvals
import seller_actions
import telegram_api

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
    conn = sqlite3.connect(path)
    conn.executescript(open(SCHEMA).read())
    conn.execute("INSERT INTO sellers (id, name, contact) VALUES (1, 'Shop1', '999')")
    d0 = conn.execute("SELECT id, name FROM departments ORDER BY id").fetchone()
    conn.execute("INSERT INTO seller_departments (seller_id, department_id) VALUES (1, ?)", (d0[0],))
    # a valid category name in that department, for the approve-with-category path
    cat = conn.execute("SELECT name FROM categories WHERE department_id = ? ORDER BY id LIMIT 1",
                       (d0[0],)).fetchone()[0]
    conn.commit(); conn.close()
    return path, d0[1], cat


# capture Telegram notifications instead of sending
sent = []
telegram_api.send_message = lambda cid, txt: sent.append((cid, txt)) or True

print("── approvals: uncategorized cannot go live ──")
db, dept, cat = make_db()
p = seller_actions.post_product(1, "Phone", "a phone", "/p/a.jpg", department=dept,
                                photos=["/p/a.jpg", "/p/b.jpg"], db_path=db)
r = approvals.approve(p["id"], db_path=db, notify=False)
check("approve w/o category refused (needs_category)", not r["ok"] and r["reason"] == "needs_category")
check("still pending", seller_actions.get_product(p["id"], db_path=db)["status"] == "pending")

print("── approvals: approve WITH a known category promotes ──")
r = approvals.approve(p["id"], category=cat, db_path=db, notify=False)
check("approve with category → ok", r["ok"] and r["status"] == "live")
check("now live", seller_actions.get_product(p["id"], db_path=db)["status"] == "live")

print("── approvals: unknown category is queued, stays pending ──")
p2 = seller_actions.post_product(1, "Gadget", "d", "/p/c.jpg", department=dept, db_path=db)
r = approvals.approve(p2["id"], category="totally-made-up-cat", db_path=db, notify=False)
check("unknown category → not ok (queued)", not r["ok"] and r["reason"] == "needs_category")
with sqlite3.connect(db) as c:
    q = c.execute("SELECT COUNT(*) FROM pending_categories").fetchone()[0]
check("queued to pending_categories (governed, no auto-create)", q == 1)
check("product stays pending", seller_actions.get_product(p2["id"], db_path=db)["status"] == "pending")

print("── approvals: pre-categorized product approves directly ──")
p3 = seller_actions.post_product(1, "Tab", "d", "/p/d.jpg", department=dept,
                                 category=cat, db_path=db)
r = approvals.approve(p3["id"], db_path=db, notify=False)
check("approve categorized → live", r["ok"] and seller_actions.get_product(p3["id"], db_path=db)["status"] == "live")

print("── approvals: reject + not-pending guards ──")
p4 = seller_actions.post_product(1, "Junk", "d", "/p/e.jpg", department=dept, db_path=db)
r = approvals.reject(p4["id"], reason="blurry photo", db_path=db, notify=False)
check("reject → rejected", r["ok"] and seller_actions.get_product(p4["id"], db_path=db)["status"] == "rejected")
r = approvals.approve(p4["id"], category=cat, db_path=db, notify=False)
check("can't approve a rejected item", not r["ok"] and r["reason"] == "not_pending")
r = approvals.reject(999, db_path=db, notify=False)
check("reject unknown id → not_pending", not r["ok"])

print("── approvals: pending_queue context ──")
q = approvals.pending_queue(db)
check("queue lists remaining pending (p2 only)", [x["id"] for x in q] == [p2["id"]])
check("queue row has department/price/photos", q[0]["department"] == dept and "photos" in q[0])

print("── approvals: seller notified over Telegram when linked ──")
db2, dept2, cat2 = make_db()
with sqlite3.connect(db2) as c:
    c.execute("INSERT INTO seller_telegram (telegram_user_id, seller_id) VALUES (555, 1)")
    c.commit()
pn = seller_actions.post_product(1, "NotifyMe", "d", "/p/x.jpg", department=dept2,
                                 category=cat2, db_path=db2)
sent.clear()
approvals.approve(pn["id"], db_path=db2, notify=True)
check("linked seller gets a Telegram notice on approve", len(sent) == 1 and sent[0][0] == 555 and "LIVE" in sent[0][1])

print("── approvals: unlinked seller → no notification, decision still succeeds ──")
db3, dept3, cat3 = make_db()
pu = seller_actions.post_product(1, "Quiet", "d", "/p/y.jpg", department=dept3,
                                 category=cat3, db_path=db3)
sent.clear()
r = approvals.approve(pu["id"], db_path=db3, notify=True)
check("no notice sent (seller not linked)", len(sent) == 0)
check("decision still succeeded", r["ok"])

for d in (db, db2, db3):
    os.unlink(d)
print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
