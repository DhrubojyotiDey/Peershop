#!/usr/bin/env python3
"""Sanitize a COPY of the production DB for use as the DEVELOPMENT database.

    python3 sanitize_dev_db.py peershop-dev.db

Scrubs PII / impersonation vectors and repoints media paths from the prod dir to
the dev dir, while KEEPING catalog data (products, categories, pincodes, prices)
so dev has realistic shapes to test against. Idempotent. Refuses to touch prod.
"""
import os
import secrets
import sqlite3
import sys

DEV_PASSWORD = "devpass1"                       # every seller login reset to this in dev
PROD_DIR = "/root/projects/peershop"
DEV_DIR = "/root/projects/peershop-dev"
PROD_DB = os.path.join(PROD_DIR, "peershop.db")


def sanitize(db_path: str) -> None:
    import auth                                  # local import: uses auth._pbkdf2
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF;")
    c = conn.cursor()

    # 1) mask seller contact phone numbers (they surface publicly via wa.me)
    c.execute("UPDATE sellers SET contact = '+91-90000-' || printf('%05d', id)")

    # 2) reset every login to a known dev password (keep the usernames so you can log in)
    for (sid,) in c.execute("SELECT seller_id FROM seller_credentials").fetchall():
        salt = secrets.token_hex(16)
        c.execute("UPDATE seller_credentials SET pw_hash = ?, pw_salt = ? WHERE seller_id = ?",
                  (auth._pbkdf2(DEV_PASSWORD, salt), salt, sid))

    # 3) wipe identity / session / audit / conversation tables (impersonation + chatter)
    for t in ("seller_telegram", "telegram_link_codes", "seller_sessions",
              "credential_events", "chat_logs", "conversation_messages",
              "conversation_windows", "post_drafts"):
        try:
            c.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass

    # 4) repoint absolute media paths prod -> dev
    for tbl, col in (("products", "image_path"), ("product_photos", "image_path"),
                     ("seller_photos", "image_path")):
        try:
            c.execute(f"UPDATE {tbl} SET {col} = REPLACE({col}, ?, ?)", (PROD_DIR, DEV_DIR))
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()
    print(f"sanitized {db_path}: contacts masked · logins reset to '{DEV_PASSWORD}' · "
          "telegram/sessions/audit/chat wiped · media paths repointed to dev.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python3 sanitize_dev_db.py <dev.db>")
    target = sys.argv[1]
    if os.path.abspath(target) == PROD_DB:
        sys.exit("REFUSING: that is the PRODUCTION database. Sanitize a copy only.")
    sanitize(target)
