"""
PeerShop Omnichannel — FastAPI app entrypoint (v3: multi-bot framework).

Telegram no longer uses a webhook router here -- see telegram_poller.py,
which runs as its own separate long-running process/container (no public
HTTPS endpoint needed, consistent with the existing peershop_bot pattern
and the decision to keep Pi dev local-only).

This app now serves:
  - /admin/bot-accounts  (register/list/deactivate bot -> seller pipelines)
  - /webhook/whatsapp    (still stubbed; WhatsApp is webhook-only by Meta's
                           design, on hold until a tunnel/public endpoint is
                           deliberately accepted for that platform)
  - /health
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from admin_api import router as admin_router
from db import close_pool, get_pool
from whatsapp_webhook import router as whatsapp_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="PeerShop Omnichannel Ingestion", version="0.3.0")

app.include_router(admin_router)
app.include_router(whatsapp_router)


@app.on_event("startup")
async def startup() -> None:
    await get_pool()


@app.on_event("shutdown")
async def shutdown() -> None:
    await close_pool()


@app.get("/health")
async def health():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok"}
