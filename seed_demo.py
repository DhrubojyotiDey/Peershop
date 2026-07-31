#!/usr/bin/env python3
"""
PeerShop — demo data for the multi-department catalog (current schema).

Populates sellers (some in MULTIPLE departments), shop photos, and products with
realistic department → category → subcategory taxonomy plus freeform attributes,
across live / pending / uncategorized states. This is what makes Phase E's
per-seller profile pages (department/category grouping, subcategory & attribute
filters) actually verifiable by eye.

Safe to re-run: it clears sellers first (products, seller_departments,
seller_photos, attribute history all cascade away).

    python3 seed_demo.py

Dev convenience, NOT part of the product. Real products arrive via intake.
"""

import json
import os
import secrets
import sqlite3

import auth

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "peershop.db")

# Web login for every demo seller (Phase H). Login = first name, lowercased.
# Password: DEMO_PASSWORD for the named demo shops; USER_DEMO_PASSWORD for the
# User_N dummy shops. Both are strong random values, inserted directly here.
DEMO_PASSWORD = "oQod2BNliPn6sQArITZh"
USER_DEMO_PASSWORD = "0MRQMvlojAJDnT7mQzeD"


def S(fname):
    """Path to a downloaded Pexels seed image (media/seed/)."""
    return os.path.join(HERE, "media", "seed", fname)

# Real processed images so cards actually render; one missing path shows the
# placeholder fallback.
IMG = "/root/hermes-clients/test_client/products/processed/a01f22a7_final.png"
IMG2 = "/root/pipeline/processed/fresh_phone_final.png"
MISSING = "/nonexistent/missing.png"

# name, owner_name, contact, meesho_url, [departments], [shop_photo_paths]
# Departments use the customer-oriented umbrella taxonomy (see schema.sql).
SELLERS = [
    ("Asha Textiles", "Asha Devi", "+91-90000-11111",
     "https://www.meesho.com/asha-textiles", ["apparels"], [IMG, IMG2]),
    ("Meera Crafts", "Meera Sharma", "+91-90000-22222", None,
     ["home & kitchen", "wellness"], [IMG2]),        # multi-department
    ("Ravi General Store", "Ravi Kumar", "+91-90000-33333", None,
     ["grocery", "electronics"], [IMG]),             # multi-department

    # 5 dummy sellers — one category line each, Pexels photos, INR prices.
    # login = User_1..User_5 (name token, lowercased), password "demo".
    ("User_1", "User 1", "+91-90000-10001", None, ["electronics"],   [S("e1_earbuds.jpg")]),
    ("User_2", "User 2", "+91-90000-10002", None, ["apparels"],      [S("a2_saree.jpg")]),
    ("User_3", "User 3", "+91-90000-10003", None, ["grocery"],       [S("g1_rice.jpg")]),
    ("User_4", "User 4", "+91-90000-10004", None, ["home & kitchen"],[S("h1_cookware.jpg")]),
    ("User_5", "User 5", "+91-90000-10005", None, ["wellness"],      [S("w2_yogamat.jpg")]),
]

# seller_idx, department, category|None, subcategory|None, attributes|None,
# status, title, description, image  (no subcategories/attributes in v1 taxonomy)
PRODUCTS = [
    # ── Asha Textiles (apparels) ──
    (0, "apparels", "women's fashion", None, None, "live",
     "Red Silk Saree", "Handwoven red silk saree with a gold zari border, six yards.", IMG),
    (0, "apparels", "women's fashion", None, None, "live",
     "Blue Cotton Kurti", "Blue block-print cotton kurti, straight cut, 3/4 sleeves.", MISSING),
    (0, "apparels", "men's fashion", None, None, "live",
     "Men's Linen Shirt", "Breathable half-sleeve linen shirt in olive.", IMG),
    (0, "apparels", "kids' fashion", None, None, "live",
     "Kids Cotton Frock", "Floral cotton frock, ages 3-5.", IMG),
    (0, "apparels", "women's fashion", None, None, "pending",
     "Yellow Chiffon Saree", "Lightweight yellow chiffon saree with a thin silver border.", IMG),

    # ── Meera Crafts (home & kitchen + wellness) ──
    (1, "home & kitchen", "appliances", None, None, "live",
     "Mixer Grinder 750W", "3-jar mixer grinder for everyday kitchen use.", IMG),
    (1, "home & kitchen", "kitchen", None, None, "live",
     "Steel Cookware Set", "5-piece stainless-steel cookware set.", IMG),
    (1, "wellness", "beauty", None, None, "live",
     "Herbal Face Cream", "Aloe & turmeric face cream, 50g.", IMG),
    (1, "wellness", "fitness", None, None, "live",
     "Yoga Mat 6mm", "Non-slip cushioned yoga mat.", IMG),
    (1, "home & kitchen", None, None, None, "pending",
     "Mystery Item", "Uncategorized, still pending review.", IMG),   # dept set, category NULL

    # ── Ravi General Store (grocery + electronics) ──
    (2, "grocery", "grocery", None, None, "live",
     "Cold-Pressed Mustard Oil 1L", "Kachi-ghani cold-pressed mustard oil, 1 litre.", IMG),
    (2, "grocery", "grocery", None, None, "live",
     "Basmati Rice 5kg", "Aged premium basmati rice, 5kg bag.", IMG),
    (2, "electronics", "mobiles", None, None, "live",
     "Fast Phone Charger 25W", "USB-C 25W fast charger with cable.", IMG),
    (2, "electronics", "electronics", None, None, "live",
     "LED Bulb 9W", "Cool-white energy-saving LED bulb.", IMG),

    # ── User_1 — Electronics (INR) ──
    (3, "electronics", "electronics", None, None, "live",
     "Wireless Earbuds", "Bluetooth 5.3 earbuds with charging case, 24h playtime.", S("e1_earbuds.jpg"), 1499),
    (3, "electronics", "mobiles", None, None, "live",
     "25W USB-C Fast Charger", "25W USB-C power adapter with cable.", S("e2_charger.jpg"), 699),
    (3, "electronics", "electronics", None, None, "live",
     "10000mAh Power Bank", "Slim 10000mAh power bank, dual output.", S("e3_powerbank.jpg"), 1299),

    # ── User_2 — Apparels (INR) ──
    (4, "apparels", "men's fashion", None, None, "live",
     "Men's Cotton T-Shirt", "Round-neck half-sleeve cotton t-shirt.", S("a1_tshirt.jpg"), 499),
    (4, "apparels", "women's fashion", None, None, "live",
     "Cotton Handloom Saree", "Handwoven cotton saree with contrast border.", S("a2_saree.jpg"), 1899),
    (4, "apparels", "kids' fashion", None, None, "live",
     "Kids' Cotton Frock", "Floral printed cotton frock, ages 3-6.", S("a3_frock.jpg"), 649),

    # ── User_3 — Grocery (INR) ──
    (5, "grocery", "grocery", None, None, "live",
     "Basmati Rice 5kg", "Aged long-grain basmati rice, 5kg pack.", S("g1_rice.jpg"), 520),
    (5, "grocery", "grocery", None, None, "live",
     "Mustard Oil 1L", "Kachi-ghani cold-pressed mustard oil, 1 litre.", S("g2_oil.jpg"), 210),
    (5, "grocery", "grocery", None, None, "live",
     "Toor Dal 1kg", "Unpolished toor (arhar) dal, 1kg.", S("g3_dal.jpg"), 160),

    # ── User_4 — Home & Kitchen (INR) ──
    (6, "home & kitchen", "kitchen", None, None, "live",
     "Steel Cookware Set", "5-piece stainless-steel cookware set.", S("h1_cookware.jpg"), 1999),
    (6, "home & kitchen", "appliances", None, None, "live",
     "Mixer Grinder 750W", "3-jar 750W mixer grinder.", S("h2_mixer.jpg"), 2499),
    (6, "home & kitchen", "kitchen", None, None, "live",
     "Non-stick Frying Pan", "28cm non-stick frying pan, induction-friendly.", S("h3_pan.jpg"), 599),

    # ── User_5 — Wellness (INR) ──
    (7, "wellness", "beauty", None, None, "live",
     "Aloe Vera Face Gel", "Soothing aloe vera face & skin gel, 100g.", S("w1_facegel.jpg"), 250),
    (7, "wellness", "fitness", None, None, "live",
     "Yoga Mat 6mm", "Non-slip cushioned yoga mat, 6mm.", S("w2_yogamat.jpg"), 799),
    (7, "wellness", "health", None, None, "live",
     "Whey Protein 1kg", "Whey protein concentrate, chocolate, 1kg.", S("w3_whey.jpg"), 2199),
]


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("DELETE FROM sellers;")   # cascades products / departments / photos / history

    dept_id = {n: i for i, n in conn.execute("SELECT id, name FROM departments")}
    cat_id = {(d, c): i for i, d, c in conn.execute(
        "SELECT c.id, dep.name, c.name FROM categories c "
        "JOIN departments dep ON dep.id = c.department_id")}
    sub_id = {(cid, n): i for i, cid, n in conn.execute(
        "SELECT id, category_id, name FROM subcategories")}

    seller_ids = []
    for name, owner, contact, meesho, depts, photos in SELLERS:
        sid = conn.execute(
            "INSERT INTO sellers (name, owner_name, contact, meesho_store_url) "
            "VALUES (?,?,?,?)",
            (name, owner, contact, meesho)).lastrowid
        seller_ids.append(sid)
        conn.executemany(
            "INSERT INTO seller_departments (seller_id, department_id) VALUES (?,?)",
            [(sid, dept_id[d]) for d in depts])
        conn.executemany(
            "INSERT INTO seller_photos (seller_id, image_path) VALUES (?,?)",
            [(sid, p) for p in photos])

    for row in PRODUCTS:
        si, dept, cat, sub, attrs, status, title, desc, img = row[:9]
        price = row[9] if len(row) > 9 else None
        did = dept_id[dept]
        cid = cat_id.get((dept, cat)) if cat else None
        sid_ = sub_id.get((cid, sub)) if (cid and sub) else None
        conn.execute(
            "INSERT INTO products (seller_id, department_id, category_id, "
            "subcategory_id, image_path, title, description, attributes, price, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (seller_ids[si], did, cid, sid_, img, title, desc,
             json.dumps(attrs) if attrs else None, price, status))

    # Web login for every seller. login = first name-token lowercased; password is
    # DEMO_PASSWORD for named shops, USER_DEMO_PASSWORD for User_N. Inserted
    # directly (hashed) — seed data.
    logins = {}
    for sid, entry in zip(seller_ids, SELLERS):
        login = entry[0].split()[0].lower()
        pw = USER_DEMO_PASSWORD if login.startswith("user_") else DEMO_PASSWORD
        salt = secrets.token_hex(16)
        conn.execute(
            "INSERT INTO seller_credentials (seller_id, login, pw_hash, pw_salt) "
            "VALUES (?,?,?,?)", (sid, login, auth._pbkdf2(pw, salt), salt))
        logins[sid] = (login, pw)

    conn.commit()

    # Summary the way Phase E will group it.
    print("Seeded demo data:\n")
    for sid, (name, *_rest) in zip(seller_ids, SELLERS):
        depts = ", ".join(d for (d,) in conn.execute(
            "SELECT d.name FROM seller_departments sd JOIN departments d "
            "ON d.id=sd.department_id WHERE sd.seller_id=? ORDER BY d.id", (sid,)))
        nphotos = conn.execute("SELECT COUNT(*) FROM seller_photos WHERE seller_id=?",
                               (sid,)).fetchone()[0]
        print(f"● {name}  [{depts}]  ({nphotos} shop photo(s))  "
              f"login: {logins[sid][0]} / {logins[sid][1]}")
        for dep, cat, sub, status, title in conn.execute(
                "SELECT d.name, c.name, s.name, p.status, p.title FROM products p "
                "JOIN departments d ON d.id=p.department_id "
                "LEFT JOIN categories c ON c.id=p.category_id "
                "LEFT JOIN subcategories s ON s.id=p.subcategory_id "
                "WHERE p.seller_id=? ORDER BY d.name, c.name, p.title", (sid,)):
            path = f"{dep}/{cat or '—'}" + (f"/{sub}" if sub else "")
            print(f"    [{status:7}] {path:34} {title}")
    total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    live = conn.execute("SELECT COUNT(*) FROM products WHERE status='live'").fetchone()[0]
    conn.close()
    print(f"\n{total} products ({live} live, {total-live} pending/hidden), "
          f"{len(SELLERS)} sellers.")


if __name__ == "__main__":
    main()
