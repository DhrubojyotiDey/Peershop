#!/usr/bin/env bash
# Entrypoint: dispatches to the right PeerShop process based on $PEERSHOP_ROLE.
#
# IMPORTANT — SQLite persistence bridge:
# The app code hardcodes DB_PATH = <script_dir>/peershop.db (i.e. /app/peershop.db
# inside the container). That path lives on the image's writable layer by
# default, which means it would be silently wiped on every `docker compose up
# --build` / container recreation. To persist it on the `peershop_db` named
# volume (mounted at /app/db) without touching the Python source, we:
#   1. Seed /app/db/peershop.db from the image's baked-in copy on first run
#      (so a fresh volume isn't empty).
#   2. Symlink /app/peershop.db -> /app/db/peershop.db so every module's
#      DB_PATH resolves onto the persistent volume transparently.
set -euo pipefail

DB_VOLUME_PATH="/app/db/peershop.db"
DB_APP_PATH="/app/peershop.db"

mkdir -p /app/db

if [ ! -f "$DB_VOLUME_PATH" ]; then
  if [ -f "${DB_APP_PATH}.image-seed" ]; then
    echo "[entrypoint] seeding fresh volume from image's baked-in peershop.db"
    cp "${DB_APP_PATH}.image-seed" "$DB_VOLUME_PATH"
  else
    echo "[entrypoint] no existing DB on volume and no seed found — init_db.py will create schema"
    touch "$DB_VOLUME_PATH"
  fi
fi

# Replace the in-image db file with a symlink onto the persistent volume.
if [ -e "$DB_APP_PATH" ] && [ ! -L "$DB_APP_PATH" ]; then
  rm -f "$DB_APP_PATH"
fi
ln -sf "$DB_VOLUME_PATH" "$DB_APP_PATH"

# media/ volume mount shadows the image's baked-in seed photos on first run —
# repopulate from the image-seed copy if the volume looks empty.
if [ -d /app/media.image-seed ] && [ -z "$(ls -A /app/media 2>/dev/null)" ]; then
  echo "[entrypoint] seeding fresh media/ volume from image's baked-in seed files"
  cp -a /app/media.image-seed/. /app/media/
fi

case "${PEERSHOP_ROLE:-web}" in
  web)
    echo "[entrypoint] starting PeerShop web (serve.py) on ${PEERSHOP_HOST:-0.0.0.0}:${PEERSHOP_PORT:-8000}"
    exec python3 -u serve.py
    ;;
  admin)
    echo "[entrypoint] starting PeerShop admin (review.py) on ${PEERSHOP_REVIEW_HOST:-0.0.0.0}:${PEERSHOP_REVIEW_PORT:-8001}"
    exec python3 -u review.py
    ;;
  bot)
    echo "[entrypoint] starting PeerShop telegram bot (telegram_bot.py)"
    exec python3 -u telegram_bot.py
    ;;
  initdb)
    echo "[entrypoint] running init_db.py then exiting"
    exec python3 -u init_db.py
    ;;
  *)
    echo "[entrypoint] ERROR: unknown PEERSHOP_ROLE='${PEERSHOP_ROLE:-}' (expected web|admin|bot|initdb)" >&2
    exit 1
    ;;
esac
