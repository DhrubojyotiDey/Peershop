-- PeerShop Omnichannel Ingestion — Phase 1 schema (SINGLE-TENANT, v2)
-- Replaces the earlier multi-seller design: everything forwarded by the
-- admin (the sole reseller) goes to one master PeerShop account (seller_id
-- 12, "Peershop Internal"). No seller_id/session tracking needed here --
-- that distinction is now made entirely by the Phase 2 admin-ID gate before
-- a row is ever inserted.
--
-- Design notes:
--   - platform is KEPT (dropped from the user's literal column list, but
--     needed functionally): Phase 5's confirmation reply must know whether
--     to answer via Telegram or WhatsApp. Without it the bot cannot route
--     its own "Successfully uploaded X" message back to the admin.
--   - channel_message_id + platform UNIQUE is KEPT for the same reason as
--     before: both platforms redeliver webhooks on timeout/retry
--     independent of single- vs multi-tenant design. Without this, one
--     forwarded photo can silently create duplicate bundle rows.
--   - media_url stores a LOCAL filesystem path (webhook downloads once,
--     workers read from disk) -- same convention as the v1 schema.
--   - status stays a free TEXT column (not enum): Phase 3's debounce
--     bundling and Phase 5's nudge/retry logic will both need new states
--     ('bundled', 'needs_price', etc.) without a migration.

CREATE TABLE IF NOT EXISTS omnichannel_ingest (
    id                  BIGSERIAL PRIMARY KEY,
    platform            TEXT NOT NULL CHECK (platform IN ('telegram', 'whatsapp')),
    channel_message_id  TEXT NOT NULL,              -- platform's own message ID, for dedup
    media_url           TEXT,                       -- local path to downloaded image (nullable: text-only messages)
    raw_text            TEXT NOT NULL DEFAULT '',    -- forwarded caption; OCR worker appends extracted text
    status              TEXT NOT NULL DEFAULT 'unprocessed',
    error_detail        TEXT,                       -- last error message, if any (nullable; ops debugging)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_platform_message UNIQUE (platform, channel_message_id)
);

-- Phase 3 (debounce worker) scans for status = 'unprocessed' repeatedly.
CREATE INDEX IF NOT EXISTS idx_omnichannel_status ON omnichannel_ingest (status);

-- Phase 3's 30-second bundling window groups by arrival time.
CREATE INDEX IF NOT EXISTS idx_omnichannel_created_at ON omnichannel_ingest (created_at);

-- Auto-bump updated_at on any row change (Postgres has no built-in ON UPDATE).
CREATE OR REPLACE FUNCTION omnichannel_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_omnichannel_touch ON omnichannel_ingest;
CREATE TRIGGER trg_omnichannel_touch
    BEFORE UPDATE ON omnichannel_ingest
    FOR EACH ROW
    EXECUTE FUNCTION omnichannel_touch_updated_at();
