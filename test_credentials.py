#!/usr/bin/env python3
"""Token-free tests for credential-change auditing (auth.py)."""
import os
import sqlite3
import tempfile

import auth

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
    c.execute("INSERT INTO sellers (id, name, contact) VALUES (1,'Shop1','9'),(2,'Shop2','8')")
    c.commit(); c.close()
    return path


print("── credential events: created vs changed + actor ──")
db = make_db()
auth.set_credentials(1, "asha", "firstpass1", db_path=db)      # signup (default actor)
h = auth.credential_history(1, db_path=db)
check("first set logs 'created'", len(h) == 1 and h[0]["event"] == "created")
auth.set_credentials(1, "asha", "secondpass2", db_path=db)     # seller change
h = auth.credential_history(1, db_path=db)
check("second set logs 'changed'", len(h) == 2 and h[0]["event"] == "changed")
check("newest first", h[0]["event"] == "changed" and h[1]["event"] == "created")
check("new password verifies", auth.verify_login("asha", "secondpass2", db_path=db) == 1)
check("old password no longer works", auth.verify_login("asha", "firstpass1", db_path=db) is None)

print("── admin reset is attributed to actor='admin' ──")
auth.set_credentials(1, auth.get_login(1, db_path=db), "adminset3", actor="admin", db_path=db)
h = auth.credential_history(1, db_path=db)
check("admin reset logged with actor=admin", h[0]["event"] == "changed" and h[0]["actor"] == "admin")
check("reset password verifies", auth.verify_login("asha", "adminset3", db_path=db) == 1)
check("get_login returns the login", auth.get_login(1, db_path=db) == "asha")

print("── cross-seller audit log ──")
auth.set_credentials(2, "meera", "meerapass1", db_path=db)
allev = auth.all_credential_events(db_path=db)
check("all_credential_events spans both sellers", {e["seller_id"] for e in allev} == {1, 2})
check("total events = 4 (3 for #1, 1 for #2)", len(allev) == 4)
check("each event names its seller", all(e["seller"] for e in allev))

print("── password is never retrievable (hash only) ──")
with sqlite3.connect(db) as c:
    stored = c.execute("SELECT pw_hash FROM seller_credentials WHERE seller_id=1").fetchone()[0]
check("stored value is a hash, not the plaintext", "adminset3" not in stored and len(stored) == 64)

os.unlink(db)
print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
