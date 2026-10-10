"""
PeerShop Omnichannel — Phase 2 database layer (multi-bot v3).
"""
from __future__ import annotations

import os
from typing import Optional

import asyncpg

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = os.environ["DATABASE_URL"]
        _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def insert_ingest_row(
    *,
    platform: str,
    channel_message_id: str,
    media_url: Optional[str],
    raw_text: str,
    bot_account_id: Optional[int] = None,
) -> int | None:
    """
    Insert one normalized row. Returns the new row's id, or None if this
    (platform, channel_message_id) pair already exists -- i.e. the platform
    redelivered a message we already handled. Callers should treat None as
    "already ingested, not an error".
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO omnichannel_ingest
            (platform, channel_message_id, media_url, raw_text, status, bot_account_id)
        VALUES ($1, $2, $3, $4, 'unprocessed', $5)
        ON CONFLICT (platform, channel_message_id) DO NOTHING
        RETURNING id
        """,
        platform,
        channel_message_id,
        media_url,
        raw_text,
        bot_account_id,
    )
    return row["id"] if row else None
