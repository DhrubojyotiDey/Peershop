-- PeerShop Omnichannel Ingestion — Phase 3: bundle tracking
--
-- A "bundle" is a set of rows from the SAME bot_account that arrived within
-- a quiet-debounce window of each other -- i.e. forwarded as one rapid-fire
-- burst representing a single product (caption + N photos, typically).
--
-- Design: bundle_id is assigned ONLY once the burst has gone quiet for
-- DEBOUNCE_WINDOW seconds (true debounce semantics, not just "group by
-- 30-second buckets" -- a bucket approach could cut a burst in half if the
-- worker's scan happens to land mid-burst). See debounce_worker.py.

ALTER TABLE omnichannel_ingest
    ADD COLUMN IF NOT EXISTS bundle_id UUID;

CREATE INDEX IF NOT EXISTS idx_omnichannel_bundle ON omnichannel_ingest (bundle_id);

-- Efficient per-bot-account scan: "give me this bot's unbundled unprocessed
-- rows in arrival order" is the debounce worker's core query, run every tick.
CREATE INDEX IF NOT EXISTS idx_omnichannel_bot_unbundled
    ON omnichannel_ingest (bot_account_id, created_at)
    WHERE status = 'unprocessed' AND bundle_id IS NULL;
