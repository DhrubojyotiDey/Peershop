"""
PeerShop Omnichannel — Admin API for registering bot -> seller pipelines.

Auth: a separate, dedicated ADMIN_API_KEY (NOT the per-bot webhook secret,
NOT a seller login password). This endpoint can register real bot tokens
and route ingested content to any seller_id, so it is the most privileged
surface in this whole pipeline -- treat ADMIN_API_KEY with the same care as
a root credential. Sent as a Bearer token.

Scope note: target_seller_id is validated against PeerShop's real sellers
table (cross-database read -- the SQLite file is bind-mounted read-only into
this container, see docker-compose.omnichannel.yml) to catch typos/fat-
fingered seller IDs at registration time rather than failing silently deep
in Phase 4 when a listing push has nowhere valid to go.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

import bot_accounts

logger = logging.getLogger("omnichannel.admin")

router = APIRouter(prefix="/admin")

ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")
PEERSHOP_DB_PATH = os.environ.get("PEERSHOP_DB_PATH", "/peershop_db/peershop.db")


def _require_admin(authorization: str) -> None:
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=503, detail="ADMIN_API_KEY not configured -- admin API disabled")
    expected = f"Bearer {ADMIN_API_KEY}"
    if authorization != expected:
        raise HTTPException(status_code=403, detail="invalid admin credentials")


def _validate_seller(seller_id: int) -> dict:
    """
    Confirm seller_id exists in the real PeerShop sellers table AND is typed
    bot_seller -- registering a bot pipeline against a human seller's account
    would silently dump auto-ingested listings into someone's real storefront,
    so this is a hard requirement, not a warning.
    """
    try:
        conn = sqlite3.connect(f"file:{PEERSHOP_DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, name, seller_type FROM sellers WHERE id = ?", (seller_id,)
        ).fetchone()
        conn.close()
    except sqlite3.OperationalError as e:
        logger.exception("Could not open PeerShop DB read-only at %s", PEERSHOP_DB_PATH)
        raise HTTPException(status_code=500, detail=f"peershop db unreachable: {e}")

    if row is None:
        raise HTTPException(status_code=404, detail=f"seller_id {seller_id} does not exist in PeerShop")
    if row["seller_type"] != "bot_seller":
        raise HTTPException(
            status_code=400,
            detail=f"seller_id {seller_id} ('{row['name']}') is type '{row['seller_type']}', "
                   f"not 'bot_seller' -- refusing to bind a bot pipeline to a human seller account",
        )
    return dict(row)


class CreateBotAccountRequest(BaseModel):
    platform: str = Field(..., pattern="^(telegram|whatsapp)$")
    label: str
    authorized_sender_id: str
    target_seller_id: int
    bot_token: Optional[str] = None
    whatsapp_phone_number_id: Optional[str] = None
    whatsapp_access_token: Optional[str] = None


@router.post("/bot-accounts")
async def register_bot_account(
    body: CreateBotAccountRequest,
    authorization: str = Header(default=""),
):
    _require_admin(authorization)

    if body.platform == "telegram" and not body.bot_token:
        raise HTTPException(status_code=400, detail="telegram bot accounts require bot_token")
    if body.platform == "whatsapp" and not (body.whatsapp_phone_number_id and body.whatsapp_access_token):
        raise HTTPException(
            status_code=400,
            detail="whatsapp bot accounts require whatsapp_phone_number_id and whatsapp_access_token",
        )

    seller = _validate_seller(body.target_seller_id)

    created = await bot_accounts.create_bot_account(
        platform=body.platform,
        label=body.label,
        authorized_sender_id=body.authorized_sender_id,
        target_seller_id=body.target_seller_id,
        bot_token=body.bot_token,
        whatsapp_phone_number_id=body.whatsapp_phone_number_id,
        whatsapp_access_token=body.whatsapp_access_token,
    )
    logger.info(
        "Registered bot_account id=%s platform=%s -> seller_id=%s (%s)",
        created["id"], body.platform, body.target_seller_id, seller["name"],
    )
    return {"ok": True, "bot_account": created, "target_seller_name": seller["name"]}


@router.get("/bot-accounts")
async def get_bot_accounts(authorization: str = Header(default="")):
    _require_admin(authorization)
    accounts = await bot_accounts.list_bot_accounts(include_secrets=False)
    return {"bot_accounts": accounts}


@router.delete("/bot-accounts/{bot_account_id}")
async def deactivate_bot_account(bot_account_id: int, authorization: str = Header(default="")):
    _require_admin(authorization)
    ok = await bot_accounts.deactivate_bot_account(bot_account_id)
    if not ok:
        raise HTTPException(status_code=404, detail="bot_account not found")
    return {"ok": True, "deactivated": bot_account_id}
