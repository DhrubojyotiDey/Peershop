-- PeerShop Omnichannel Ingestion — Phase 1 schema
-- Normalizes messy multi-platform seller data (Telegram/WhatsApp) into one
-- standard table that downstream OCR/LLM/nudge workers all read and write.
--
-- Design notes (see DIARY.md / review for full reasoning):
--   - channel_message_id + platform is UNIQUE: Telegram and WhatsApp both
--     redeliver webhooks on timeout/retry. Without this dedup key, a single
--     photo message can silently create 2-3 duplicate rows under load.
--   - status is a free column (not an enum) deliberately — Phase 4/5 will
--     need new states (e.g. 'needs_price', 'nudged') without a migration.
--   - media_url stores a LOCAL filesystem path (not a remote URL) per the
--     original spec: webhook downloads the image once, workers read it from
--     disk. If this ever moves to GCS/S3 (e.g. on the GCP side), media_url
--     becomes a gs:// or s3:// URI instead — callers should treat it as an
--     opaque locator, not assume local-disk semantics.
--   - seller_id is TEXT, not a FK to any existing PeerShop table: the two
--     databases (this one, peershop.db's sellers table) are intentionally
--     decoupled. Phase 4 maps seller_id -> a real PeerShop seller via the
--     HTTP API call, never a direct DB join.

CREATE TABLE IF NOT EXISTS omnichannel_ingest (
    id                  BIGSERIAL PRIMARY KEY,
    platform            TEXT NOT NULL CHECK (platform IN ('telegram', 'whatsapp')),
    seller_id           TEXT NOT NULL,              -- phone number or Telegram chat ID
    channel_message_id  TEXT NOT NULL,               -- platform's own message ID, for dedup
    media_url           TEXT,                        -- local path to downloaded image (nullable: text-only messages)
    raw_text            TEXT NOT NULL DEFAULT '',     -- chat caption; OCR worker appends extracted text
    status              TEXT NOT NULL DEFAULT 'unprocessed',
    error_detail        TEXT,                        -- last error message, if any (nullable; for ops debugging)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_platform_message UNIQUE (platform, channel_message_id)
);

-- Phase 3 (OCR worker) scans for status = 'unprocessed' repeatedly — index it.
CREATE INDEX IF NOT EXISTS idx_omnichannel_status ON omnichannel_ingest (status);

-- Phase 4 (LLM synthesizer) groups unprocessed rows BY seller_id.
CREATE INDEX IF NOT EXISTS idx_omnichannel_seller_status ON omnichannel_ingest (seller_id, status);

-- Phase 5 (nudge) finds rows stuck unprocessed for > 30 min by created_at.
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
