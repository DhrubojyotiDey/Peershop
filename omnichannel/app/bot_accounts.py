"""
PeerShop Omnichannel — bot_accounts registry (db layer additions).
"""
from __future__ import annotations

from typing import Optional

from db import get_pool


async def create_bot_account(
    *,
    platform: str,
    label: str,
    authorized_sender_id: str,
    target_seller_id: int,
    bot_token: Optional[str] = None,
    whatsapp_phone_number_id: Optional[str] = None,
    whatsapp_access_token: Optional[str] = None,
) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO bot_accounts
            (platform, label, bot_token, whatsapp_phone_number_id,
             whatsapp_access_token, authorized_sender_id, target_seller_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id, platform, label, authorized_sender_id, target_seller_id,
                  active, created_at
        """,
        platform,
        label,
        bot_token,
        whatsapp_phone_number_id,
        whatsapp_access_token,
        authorized_sender_id,
        target_seller_id,
    )
    return dict(row)


async def list_bot_accounts(include_secrets: bool = False) -> list[dict]:
    pool = await get_pool()
    cols = "id, platform, label, authorized_sender_id, target_seller_id, poll_offset, active, created_at, updated_at"
    if include_secrets:
        cols += ", bot_token, whatsapp_phone_number_id, whatsapp_access_token"
    rows = await pool.fetch(f"SELECT {cols} FROM bot_accounts ORDER BY id")
    return [dict(r) for r in rows]


async def get_active_telegram_bots() -> list[dict]:
    """Used by the long-poller to know which bot tokens to poll."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT id, label, bot_token, authorized_sender_id, target_seller_id, poll_offset
        FROM bot_accounts
        WHERE platform = 'telegram' AND active = true AND bot_token IS NOT NULL
        """
    )
    return [dict(r) for r in rows]


async def update_poll_offset(bot_account_id: int, new_offset: int) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE bot_accounts SET poll_offset = $1 WHERE id = $2",
        new_offset,
        bot_account_id,
    )


async def deactivate_bot_account(bot_account_id: int) -> bool:
    pool = await get_pool()
    result = await pool.execute(
        "UPDATE bot_accounts SET active = false WHERE id = $1", bot_account_id
    )
    return result == "UPDATE 1"
