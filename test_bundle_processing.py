#!/usr/bin/env python3
"""Token-free tests for Task 6: bundle -> AI-draft pipeline -> pending product.

process_bundle() is the tail of debounce_worker's pipeline: given a finalized
('bundled') bundle_id, it must turn real staged bot_raw_ingest rows into ONE
real 'pending' product via intake.ingest_photo_noninteractive() ->
seller_actions.post_product() -- the SAME single writer every other posting
path uses, so the admin review gate (review.py) needs zero changes.

The heavy AI call (describe.describe / identify_object) and the Hermes image
pipeline (receive_photo/remove_background/enhance) are monkeypatched to avoid
real network/model calls in a token-free test -- this mirrors how
test_bot_onboarding.py / test_forward_routing.py avoid real Telegram calls.
"""
import os
import shutil
import sqlite3
import tempfile

import debounce_worker
import intake
import seller_actions
import telegram_link

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = os.path.join(HERE, "schema.sql")

ok = 0
fail = 0
def check(name, cond):
    global ok, fail
    if cond: ok += 1; print(f"  ✓ {name}")
    else: fail += 1; print(f"  ✗ {name}")


def make_db(with_department=True):
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    c = sqlite3.connect(path)
    c.executescript(open(SCHEMA).read())
    c.execute("INSERT INTO sellers (id, name, contact) VALUES (1, 'Shop1', '9990001111')")
    dept_id = c.execute("SELECT id FROM departments ORDER BY id LIMIT 1").fetchone()[0]
    if with_department:
        c.execute("INSERT INTO seller_departments (seller_id, department_id) VALUES (1, ?)",
                  (dept_id,))
    c.commit(); c.close()
    return path


def make_bundle(db, seller_id, rows, bundle_id="bundle-under-test"):
    """rows: list of (media_path, raw_text) tuples, pre-'bundled'."""
    conn = sqlite3.connect(db)
    for media_path, raw_text in rows:
        conn.execute(
            "INSERT INTO bot_raw_ingest (seller_id, media_path, raw_text, status, bundle_id) "
            "VALUES (?, ?, ?, 'bundled', ?)", (seller_id, media_path, raw_text, bundle_id))
    conn.commit()
    conn.close()
    return bundle_id


def row_statuses(db, bundle_id):
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute(
            "SELECT status FROM bot_raw_ingest WHERE bundle_id = ? ORDER BY id",
            (bundle_id,)).fetchall()]
    finally:
        conn.close()


media_dir = tempfile.mkdtemp()
fake_photo = os.path.join(media_dir, "incoming.jpg")
with open(fake_photo, "wb") as f:
    f.write(b"\xff\xd8\xfffakejpegbytes")

# Stub out the heavy pipeline (no rembg/Gemini calls in a token-free test) --
# _run_pipeline normally does receive_photo -> remove_background -> enhance;
# here it's a no-op passthrough, and describe.describe returns a canned draft,
# optionally incorporating a raw_text_hint if intake threads one through.
_orig_run_pipeline = intake._run_pipeline
_orig_describe = intake.describe.describe
_orig_ensure_key = intake._ensure_gemini_key
intake._run_pipeline = lambda incoming_path: incoming_path
intake._ensure_gemini_key = lambda: None


def fake_describe(image_path):
    return {"title": "Blue Cotton Saree", "description": "A lovely saree.", "object": "saree"}
intake.describe.describe = fake_describe


def fake_incorporate_hint(listing, hint):
    result = dict(listing)
    result["description"] = f"{listing['description']} (hint: {hint})"
    return result
_orig_incorporate_hint = intake.describe.incorporate_hint
intake.describe.incorporate_hint = fake_incorporate_hint

# classify_product makes a real Groq call when the seller didn't self-select a
# category (always true for bot-auto-posted bundles) -- stub it to a no-op so
# this stays a genuinely token-free test; taxonomy quality is out of scope here.
_orig_classify_product = intake.categorize.classify_product
intake.categorize.classify_product = lambda product_id, db_path=None: None


print("── a bundle with a photo becomes ONE real pending product ──")
db = make_db(with_department=True)
bundle_id = make_bundle(db, 1, [(fake_photo, "Blue saree, cotton, 500rs")])
result = debounce_worker.process_bundle(bundle_id, db_path=db)
check("result is 'processed'", result["result"] == "processed")
check("a product_id was returned", isinstance(result.get("product_id"), int))
products = seller_actions.get_my_products(1, db_path=db)
check("exactly one product exists for this seller", len(products) == 1)
check("product is pending (admin gate untouched)", products[0]["status"] == "pending")
check("rows marked processed", row_statuses(db, bundle_id) == ["processed"])
os.unlink(db)

print("── multiple rows in one bundle still yield exactly ONE product (v1 scope) ──")
db = make_db(with_department=True)
bundle_id = make_bundle(db, 1, [
    (fake_photo, "first caption"),
    (None, "second caption, text only"),
    (fake_photo, None),
])
result = debounce_worker.process_bundle(bundle_id, db_path=db)
check("result is 'processed'", result["result"] == "processed")
products = seller_actions.get_my_products(1, db_path=db)
check("exactly ONE product for the whole bundle", len(products) == 1)
check("all 3 rows marked processed together", row_statuses(db, bundle_id) == ["processed"] * 3)
os.unlink(db)

print("── a text-only bundle (no photo anywhere) is flagged, not force-posted ──")
db = make_db(with_department=True)
bundle_id = make_bundle(db, 1, [(None, "just a caption, no photo ever sent")])
result = debounce_worker.process_bundle(bundle_id, db_path=db)
check("result is 'needs_photo'", result["result"] == "needs_photo")
check("no product was created", len(seller_actions.get_my_products(1, db_path=db)) == 0)
check("row flagged needs_photo, not silently dropped", row_statuses(db, bundle_id) == ["needs_photo"])
os.unlink(db)

print("── a seller with NO department yet is flagged, not force-posted ──")
db = make_db(with_department=False)
bundle_id = make_bundle(db, 1, [(fake_photo, "a product with nowhere to go")])
result = debounce_worker.process_bundle(bundle_id, db_path=db)
check("result is 'needs_department'", result["result"] == "needs_department")
check("no product was created", len(seller_actions.get_my_products(1, db_path=db)) == 0)
check("row flagged needs_department", row_statuses(db, bundle_id) == ["needs_department"])
os.unlink(db)

print("── admin gets a best-effort push notification when configured ──")
db = make_db(with_department=True)
bundle_id = make_bundle(db, 1, [(fake_photo, "notify me")])
sent = []
_orig_send = debounce_worker.telegram_api.send_message
debounce_worker.telegram_api.send_message = lambda cid, txt: sent.append((cid, txt))
debounce_worker.ADMIN_TELEGRAM_ID = "999000111"
debounce_worker.process_bundle(bundle_id, db_path=db)
check("exactly one admin notification sent", len(sent) == 1)
check("sent to the configured admin id", sent[0][0] == "999000111")
check("mentions the product title", "Blue Cotton Saree" in sent[0][1])
debounce_worker.telegram_api.send_message = _orig_send
debounce_worker.ADMIN_TELEGRAM_ID = None
os.unlink(db)

print("── with NO admin id configured, processing still succeeds (no crash) ──")
db = make_db(with_department=True)
bundle_id = make_bundle(db, 1, [(fake_photo, "no admin configured")])
debounce_worker.ADMIN_TELEGRAM_ID = None
result = debounce_worker.process_bundle(bundle_id, db_path=db)
check("still processed fine with no admin configured", result["result"] == "processed")
os.unlink(db)

intake._run_pipeline = _orig_run_pipeline
intake.describe.describe = _orig_describe
intake.describe.incorporate_hint = _orig_incorporate_hint
intake.categorize.classify_product = _orig_classify_product
intake._ensure_gemini_key = _orig_ensure_key
shutil.rmtree(media_dir, ignore_errors=True)

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
