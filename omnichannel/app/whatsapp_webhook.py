"""
PeerShop Omnichannel — WhatsApp webhook adapter (Phase 2, single-tenant v2, STUBBED).

STATUS: deferred pending Meta Developer App setup (not a ToS blocker anymore
-- the single-tenant "admin forwards 1:1 to the bot" design uses the OFFICIAL
WhatsApp Business Cloud API exactly as intended, unlike the earlier
multi-seller "read group messages" design which had no sanctioned path).

Once ADMIN_WHATSAPP_NUMBER + WHATSAPP_ACCESS_TOKEN + WHATSAPP_PHONE_NUMBER_ID
are configured, this becomes a straightforward admin-gated ingest identical
in shape to telegram_webhook.py.
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, HTTPException, Query, Request

logger = logging.getLogger("omnichannel.whatsapp")

router = APIRouter()

WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
ADMIN_WHATSAPP_NUMBER = os.environ.get("ADMIN_WHATSAPP_NUMBER", "")


@router.get("/webhook/whatsapp")
async def whatsapp_verify(
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_verify_token: str = Query(default="", alias="hub.verify_token"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
):
    """Meta's webhook verification handshake (required before real events flow)."""
    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN and WHATSAPP_VERIFY_TOKEN:
        return int(hub_challenge) if hub_challenge.isdigit() else hub_challenge
    raise HTTPException(status_code=403, detail="verification failed")


@router.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    payload = await request.json()
    logger.warning(
        "WhatsApp webhook received but NOT ingested (stub -- Meta app not yet "
        "configured, see module docstring): %s",
        payload,
    )
    return {"ok": True, "status": "stub_not_ingested"}
