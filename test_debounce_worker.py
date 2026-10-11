#!/usr/bin/env python3
"""Token-free tests for the debounce worker (Task 5).

scan_and_bundle() groups a seller's 'unprocessed' bot_raw_ingest rows into one
bundle (shared bundle_id, status -> 'bundled') ONLY once the burst has gone
quiet -- the newest unprocessed row for that seller is >= QUIET_WINDOW_SECONDS
old. This is a true quiet-window debounce, not a fixed timer from the first
message, so a slow trickle of forwards still gets treated as one ongoing burst
as long as gaps stay under the window.
"""
import os
import sqlite3
import tempfile

import debounce_worker

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
    c.execute("INSERT INTO sellers (id, name, contact) VALUES (2, 'Shop2', '9990002222')")
    c.commit(); c.close()
    return path


def insert_raw(db, seller_id, raw_text=None, media_path=None, age_seconds=0):
    """Insert a bot_raw_ingest row whose created_at is `age_seconds` in the past."""
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO bot_raw_ingest (seller_id, media_path, raw_text, status, created_at) "
        "VALUES (?, ?, ?, 'unprocessed', datetime('now', ?))",
        (seller_id, media_path, raw_text, f"-{age_seconds} seconds"))
    conn.commit()
    conn.close()


def rows_for(db, seller_id):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT status, bundle_id FROM bot_raw_ingest WHERE seller_id = ? ORDER BY id",
            (seller_id,)).fetchall()
    finally:
        conn.close()


print("── a fresh burst (most recent row < quiet window old) is NOT bundled yet ──")
db = make_db()
insert_raw(db, 1, raw_text="row A", age_seconds=5)
insert_raw(db, 1, raw_text="row B", age_seconds=1)   # most recent: only 1s old
result = debounce_worker.scan_and_bundle(db_path=db, quiet_window=30)
check("nothing bundled", result["bundled"] == 0)
check("rows still unprocessed", all(r[0] == "unprocessed" for r in rows_for(db, 1)))
os.unlink(db)

print("── a quiet burst (most recent row >= quiet window old) IS bundled ──")
db = make_db()
insert_raw(db, 1, raw_text="row A", age_seconds=40)
insert_raw(db, 1, raw_text="row B", age_seconds=31)  # most recent: 31s old, past 30s window
result = debounce_worker.scan_and_bundle(db_path=db, quiet_window=30)
check("one bundle created", result["bundled"] == 1)
rows = rows_for(db, 1)
check("both rows now 'bundled'", all(r[0] == "bundled" for r in rows))
check("both rows share the same bundle_id", rows[0][1] == rows[1][1] and rows[0][1])
os.unlink(db)

print("── two different sellers' quiet bursts get SEPARATE bundle_ids ──")
db = make_db()
insert_raw(db, 1, raw_text="seller1 msg", age_seconds=35)
insert_raw(db, 2, raw_text="seller2 msg", age_seconds=35)
result = debounce_worker.scan_and_bundle(db_path=db, quiet_window=30)
check("two bundles created", result["bundled"] == 2)
b1 = rows_for(db, 1)[0][1]
b2 = rows_for(db, 2)[0][1]
check("different bundle_ids per seller", b1 != b2)
os.unlink(db)

print("── a NEW unprocessed row arriving after an earlier bundle is untouched ──")
db = make_db()
insert_raw(db, 1, raw_text="old quiet burst", age_seconds=40)
debounce_worker.scan_and_bundle(db_path=db, quiet_window=30)   # bundles row 1
insert_raw(db, 1, raw_text="fresh new message", age_seconds=0)
result = debounce_worker.scan_and_bundle(db_path=db, quiet_window=30)
check("second scan bundles nothing new (fresh row too young)", result["bundled"] == 0)
rows = rows_for(db, 1)
check("old row still 'bundled'", rows[0][0] == "bundled")
check("new row still 'unprocessed'", rows[1][0] == "unprocessed")
os.unlink(db)

print("── already-bundled/processed rows are ignored by future scans ──")
db = make_db()
insert_raw(db, 1, raw_text="will be bundled", age_seconds=40)
r1 = debounce_worker.scan_and_bundle(db_path=db, quiet_window=30)
r2 = debounce_worker.scan_and_bundle(db_path=db, quiet_window=30)
check("first scan bundles it", r1["bundled"] == 1)
check("second scan finds nothing left to bundle", r2["bundled"] == 0)
os.unlink(db)

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
