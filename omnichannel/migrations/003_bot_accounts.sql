-- PeerShop Omnichannel Ingestion — Phase 1b: bot_accounts registry
--
-- This is the framework piece that makes the pipeline multi-bot-capable:
-- each row binds ONE bot credential (a Telegram bot token, or later a
-- WhatsApp phone_number_id) to ONE authorized human forwarder and ONE
-- target PeerShop seller (a bot_seller-typed account, e.g. "Peershop
-- Internal", "Peershop Internal 2", ...).
--
-- Design notes:
--   - bot_token is stored here (not just in .env) because the system must
--     support registering NEW bots at runtime via the admin API, not just
--     the one bot baked into deployment config. Treat this table as secret
--     material: same sensitivity as .env, never logged, never returned by
--     any non-admin-authenticated endpoint.
--   - authorized_sender_id is a TEXT platform-native ID (Telegram numeric
--     user ID; later a WhatsApp E.164 phone number) -- the ONLY sender this
--     bot account will accept content from. This is what "admin gate" now
--     means per-bot rather than one global env var.
--   - target_seller_id is a plain INTEGER FK match against the EXISTING
--     peershop.db sellers table (SQLite, cross-database, not a real FK
--     constraint -- enforced in application code at insert/validation time,
--     same decoupling principle as omnichannel_ingest's seller_id was never
--     a real FK in the original design).
--   - poll_offset: Telegram's long-poll getUpdates needs a persisted last-
--     seen update_id per bot, so a poller restart doesn't replay old
--     messages or lose the place. Lives here, not a separate file per bot,
--     so it scales cleanly to N bots.
--   - platform is a CHECK, not free text, deliberately: WhatsApp's row shape
--     will need different fields later (phone_number_id, access_token) --
--     better to widen this table with platform-specific nullable columns
--     when that lands, rather than let typos silently create a third
--     unsupported platform value.

CREATE TABLE IF NOT EXISTS bot_accounts (
    id                    BIGSERIAL PRIMARY KEY,
    platform              TEXT NOT NULL CHECK (platform IN ('telegram', 'whatsapp')),
    label                 TEXT NOT NULL,           -- human-readable, e.g. "Peershop Internal bot"
    bot_token             TEXT,                    -- Telegram bot token (nullable: WhatsApp uses different auth)
    whatsapp_phone_number_id TEXT,                 -- WhatsApp Business phone_number_id (nullable: Telegram N/A)
    whatsapp_access_token TEXT,                    -- WhatsApp access token (nullable: Telegram N/A)
    authorized_sender_id  TEXT NOT NULL,            -- ONLY this platform-native sender ID is accepted
    target_seller_id      INTEGER NOT NULL,         -- maps to peershop.db sellers.id (bot_seller type)
    poll_offset           BIGINT NOT NULL DEFAULT 0, -- Telegram getUpdates offset; unused for WhatsApp
    active                BOOLEAN NOT NULL DEFAULT true,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One bot credential should not be registered twice.
    CONSTRAINT uq_bot_token UNIQUE (bot_token)
);

CREATE INDEX IF NOT EXISTS idx_bot_accounts_active ON bot_accounts (active) WHERE active = true;

DROP TRIGGER IF EXISTS trg_bot_accounts_touch ON bot_accounts;
CREATE TRIGGER trg_bot_accounts_touch
    BEFORE UPDATE ON bot_accounts
    FOR EACH ROW
    EXECUTE FUNCTION omnichannel_touch_updated_at();  -- reuses the function from migration 002

-- Link each ingested row back to the bot account it arrived through, so
-- Phase 4 knows which target_seller_id to push the finished listing to.
-- Nullable for backward compat with any pre-existing rows; new inserts
-- should always set this.
ALTER TABLE omnichannel_ingest
    ADD COLUMN IF NOT EXISTS bot_account_id BIGINT REFERENCES bot_accounts(id);

CREATE INDEX IF NOT EXISTS idx_omnichannel_bot_account ON omnichannel_ingest (bot_account_id);
