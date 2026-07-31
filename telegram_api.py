#!/usr/bin/env python3
"""
PeerShop — shared Telegram Bot API primitives (stdlib only).

The raw HTTP layer for PeerShop's own bot (token load + one-call helper + the few
methods we use). Deliberately dependency-free (urllib/json) so it can be imported
by BOTH the long-poller (telegram_bot.py) AND the stdlib-only core (approvals.py
uses send_message to notify sellers of a review decision) — importing telegram_bot
would drag in google.genai via bot.py, which the core must not require.

Token is PeerShop's OWN PEERSHOP_TELEGRAM_TOKEN (env or /root/.hermes/.env), never
Hermes's TELEGRAM_BOT_TOKEN (separate bot; two pollers on one token → 409).
"""

import json
import os
import sys
import urllib.request

HERMES_ENV = "/root/.hermes/.env"
POLL_TIMEOUT = 50                       # Telegram long-poll seconds
API_TIMEOUT = POLL_TIMEOUT + 15
MAX_PHOTO_BYTES = 8 * 1024 * 1024


def load_token() -> str:
    """PeerShop's OWN bot token: env PEERSHOP_TELEGRAM_TOKEN, else /root/.hermes/.env."""
    if os.environ.get("PEERSHOP_TELEGRAM_TOKEN"):
        return os.environ["PEERSHOP_TELEGRAM_TOKEN"]
    if os.path.exists(HERMES_ENV):
        for line in open(HERMES_ENV):
            line = line.strip()
            if line.startswith("PEERSHOP_TELEGRAM_TOKEN=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("PEERSHOP_TELEGRAM_TOKEN not found in env or /root/.hermes/.env")


def api(method: str, params: dict, timeout: int = API_TIMEOUT) -> dict:
    """One Telegram Bot API call. Single choke point (tests monkeypatch this)."""
    url = f"https://api.telegram.org/bot{load_token()}/{method}"
    req = urllib.request.Request(
        url, data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "peershop-bot/0.1"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def get_me() -> dict:
    return api("getMe", {}, timeout=15).get("result", {})


def send_message(chat_id, text: str) -> bool:
    """Send a message. Failures are logged and swallowed (never raised) — callers
    (the poll loop, review notifications) must survive a delivery failure."""
    try:
        api("sendMessage", {"chat_id": chat_id, "text": text}, timeout=20)
        return True
    except Exception as e:
        print(f"sendMessage failed for {chat_id}: {e}", file=sys.stderr)
        return False


def get_file(file_id: str) -> dict:
    return api("getFile", {"file_id": file_id}, timeout=20).get("result", {})


def download_file(file_path: str, max_bytes: int = MAX_PHOTO_BYTES) -> bytes:
    """Download bytes for a Telegram file_path (from getFile). Size-capped."""
    url = f"https://api.telegram.org/file/bot{load_token()}/{file_path}"
    req = urllib.request.Request(url, headers={"User-Agent": "peershop-bot/0.1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read(max_bytes + 1)
    if not data:
        raise ValueError("empty file")
    if len(data) > max_bytes:
        raise ValueError("file exceeds size cap")
    return data
