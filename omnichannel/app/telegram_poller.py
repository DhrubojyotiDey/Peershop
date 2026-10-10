#!/usr/bin/env python3
"""
PeerShop Omnichannel — Telegram long-poll worker (multi-bot capable).

Replaces the earlier webhook-based design: no public HTTPS endpoint needed,
consistent with the existing peershop_bot's long-polling pattern and the
decision to keep the Pi dev environment local-only (no tunnel).

Polls EVERY active, bot_token-configured row in bot_accounts concurrently
(one getUpdates loop per bot, since each Telegram bot token is a fully
separate long-poll stream). New bots registered via the admin API are
picked up automatically on the next refresh cycle -- no restart required
for the framework to scale to bot #2, #3, etc.

Per-bot admin gate: only messages from that bot_account's own
authorized_sender_id are ingested; everything else is logged and dropped.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

import httpx

import bot_accounts
from db import insert_ingest_row, get_pool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("omnichannel.telegram_poller")

MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "/app/media/incoming"))
BOT_REFRESH_INTERVAL = 30  # seconds between re-checking bot_accounts for new/removed bots
POLL_TIMEOUT = 25  # Telegram long-poll timeout (seconds); must be < HTTP client timeout below


async def _download_telegram_file(client: httpx.AsyncClient, bot_token: str, file_id: str) -> Optional[str]:
    try:
        resp = await client.get(f"https://api.telegram.org/bot{bot_token}/getFile", params={"file_id": file_id})
        resp.raise_for_status()
        file_path = resp.json()["result"]["file_path"]
    except Exception:
        logger.exception("getFile failed for file_id=%s", file_id)
        return None

    download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
    local_name = f"telegram_{uuid.uuid4().hex}_{Path(file_path).name}"
    local_path = MEDIA_DIR / local_name

    try:
        img_resp = await client.get(download_url)
        img_resp.raise_for_status()
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(img_resp.content)
    except Exception:
        logger.exception("Image download failed for file_id=%s", file_id)
        return None

    return str(local_path)


async def _poll_one_bot(bot: dict) -> None:
    """
    Runs forever for ONE bot_account row. Each bot gets its own independent
    getUpdates long-poll loop and its own httpx client (separate connection
    pools -- one bot being slow/down must not stall another bot's polling).
    """
    bot_id = bot["id"]
    label = bot["label"]
    token = bot["bot_token"]
    authorized_sender = bot["authorized_sender_id"]
    offset = bot["poll_offset"]

    logger.info("Starting poll loop for bot_account id=%s (%s)", bot_id, label)

    async with httpx.AsyncClient(timeout=POLL_TIMEOUT + 10) as client:
        while True:
            try:
                resp = await client.get(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params={"offset": offset, "timeout": POLL_TIMEOUT},
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                logger.exception("getUpdates failed for bot_account id=%s (%s); retrying in 5s", bot_id, label)
                await asyncio.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("channel_post")
                if message is None:
                    continue

                sender_id = str(message.get("from", {}).get("id", ""))
                if sender_id != authorized_sender:
                    logger.warning(
                        "bot_account id=%s (%s): rejected message from non-admin sender_id=%s",
                        bot_id, label, sender_id,
                    )
                    continue

                message_id = str(message["message_id"])
                raw_text = message.get("caption") or message.get("text") or ""

                media_url: Optional[str] = None
                photos = message.get("photo")
                if photos:
                    largest = photos[-1]
                    media_url = await _download_telegram_file(client, token, largest["file_id"])

                new_id = await insert_ingest_row(
                    platform="telegram",
                    channel_message_id=f"{bot_id}:{message_id}",  # prefix with bot_id -- message_id alone is only unique PER bot token
                    media_url=media_url,
                    raw_text=raw_text,
                    bot_account_id=bot_id,
                )
                if new_id is None:
                    logger.info("bot_account id=%s: duplicate message_id=%s, skipped", bot_id, message_id)
                else:
                    logger.info(
                        "bot_account id=%s (%s): ingested row id=%s has_media=%s",
                        bot_id, label, new_id, media_url is not None,
                    )

            if data.get("result"):
                await bot_accounts.update_poll_offset(bot_id, offset)


async def main() -> None:
    running: dict[int, asyncio.Task] = {}

    while True:
        active_bots = await bot_accounts.get_active_telegram_bots()
        active_ids = {b["id"] for b in active_bots}

        # Start pollers for newly-registered bots.
        for bot in active_bots:
            if bot["id"] not in running:
                running[bot["id"]] = asyncio.create_task(_poll_one_bot(bot))

        # Stop pollers for bots that were deactivated since the last check.
        for bot_id in list(running):
            if bot_id not in active_ids:
                logger.info("bot_account id=%s deactivated, stopping its poll loop", bot_id)
                running[bot_id].cancel()
                del running[bot_id]

        await asyncio.sleep(BOT_REFRESH_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
