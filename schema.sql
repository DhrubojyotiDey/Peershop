-- PeerShop — Phase A schema (multi-department restructure)
-- SQLite. Lean and native. No ORM, no migrations framework.
--
-- Hierarchy:  department → category (dept-scoped) → subcategory (category-scoped)
--             + attributes (lightweight, per-category, NOT governed)
--
-- Integrity is enforced with COMPOSITE foreign keys, not triggers:
--   * a product's category must belong to the product's department
--   * a product's subcategory (if any) must belong to the product's category
-- This is why categories/subcategories carry extra UNIQUE(id, parent) indexes —
-- they are the targets those composite FKs point at.
--
-- Governance: new categories/subcategories are NEVER auto-created from seller
-- input; proposals land in pending_categories / pending_subcategories for admin
-- approval (which then inserts the real row).
--
-- This is a destructive restructure of the earlier flat/fashion schema: it
-- DROPs the old tables and rebuilds. Dev data is demo-only, so that's fine.
-- init_db.py therefore now performs a clean rebuild each run.

PRAGMA foreign_keys = ON;

-- Drop old + current objects (children first) so the rebuild is deterministic.
DROP TABLE IF EXISTS credential_events;
DROP TABLE IF EXISTS post_drafts;
DROP TABLE IF EXISTS product_photos;
DROP TABLE IF EXISTS telegram_link_codes;
DROP TABLE IF EXISTS seller_telegram;
DROP TABLE IF EXISTS bot_onboarding_sessions;
DROP TABLE IF EXISTS bot_raw_ingest;
DROP TABLE IF EXISTS seller_sessions;
DROP TABLE IF EXISTS seller_credentials;
DROP TABLE IF EXISTS conversation_messages;
DROP TABLE IF EXISTS conversation_windows;
DROP TABLE IF EXISTS product_nudges;
DROP TABLE IF EXISTS chat_logs;
DROP TABLE IF EXISTS seller_attribute_history;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS seller_photos;
DROP TABLE IF EXISTS seller_departments;
DROP TABLE IF EXISTS pending_departments;
DROP TABLE IF EXISTS pending_subcategories;
DROP TABLE IF EXISTS pending_categories;
DROP TABLE IF EXISTS category_attributes;
DROP TABLE IF EXISTS subcategories;
DROP TABLE IF EXISTS categories;
DROP TABLE IF EXISTS sellers;
DROP TABLE IF EXISTS departments;

-- ─────────────────────────────────────────────────────────────────────────────
-- departments — the 5 launch departments (approved; seeded below)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE departments (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

-- ─────────────────────────────────────────────────────────────────────────────
-- sellers — one row per neighbor seller (department links are many-to-many below)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE sellers (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,             -- shop / store name (buyer-facing)
    owner_name       TEXT,                       -- owner's name (buyer-facing recognition); optional
    contact          TEXT NOT NULL,             -- phone/number buyers use to REACH the seller
    meesho_store_url TEXT,                       -- OPTIONAL outbound link only
    pincode          TEXT,                       -- 6-digit India PIN for hyperlocal "nearest first"; optional
    seller_type      TEXT NOT NULL DEFAULT 'human', -- 'human' (manually onboarded) or 'bot_seller' (created via the Telegram bot's self-service onboarding flow)
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- seller ↔ department : MANY-TO-MANY. A seller can belong to multiple departments;
-- we never force exactly one.
CREATE TABLE seller_departments (
    seller_id     INTEGER NOT NULL REFERENCES sellers(id)     ON DELETE CASCADE,
    department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (seller_id, department_id)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- seller_photos (Phase C) — the shop/storefront photo(s) a seller uploads at
-- onboarding. These are PERSISTED and referenced here, NOT discarded after the
-- department classification reads them: Phase E's seller profile page displays
-- them. One-to-many (a seller may upload several). ON DELETE CASCADE → wiped on
-- account close, like all other seller-owned data.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE seller_photos (
    id         INTEGER PRIMARY KEY,
    seller_id  INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    image_path TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_seller_photos_seller ON seller_photos(seller_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- categories — scoped to exactly one department. Closed, governed list.
--   UNIQUE(department_id, name): no dup category name within a department.
--   UNIQUE(id, department_id):   composite-FK target so products can prove a
--                                category belongs to their department.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE categories (
    id            INTEGER PRIMARY KEY,
    department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    UNIQUE (department_id, name),
    UNIQUE (id, department_id)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- subcategories — genuine type-distinctions within a category. Closed, governed.
--   UNIQUE(id, category_id): composite-FK target for products.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE subcategories (
    id          INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    UNIQUE (category_id, name),
    UNIQUE (id, category_id)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- category_attributes — lightweight per-category variant-property DEFINITIONS.
--   NOT a governed taxonomy entity, NOT in the pending queue. Just declares which
--   attribute KEYS a category offers, for dropdowns/filters. A product stores its
--   chosen values in products.attributes (JSON).
--   DECISION 2 (v1): attribute VALUES are freeform text — no enum enforcement,
--   no pending queue, nothing checked beyond basic input sanitization at the app
--   layer. `suggested_values` below is an OPTIONAL, NON-ENFORCED hint used only
--   to seed a dropdown; the DB never constrains products.attributes to it.
--   e.g. (stationery→paper products): name='ruling', suggested='["ruled","unruled","blank"]'
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE category_attributes (
    id               INTEGER PRIMARY KEY,
    category_id      INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,           -- e.g. 'ruling', 'size', 'color'
    suggested_values TEXT,                    -- OPTIONAL JSON array; UI hint only, NOT enforced
    UNIQUE (category_id, name)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Governance queues — proposals awaiting manual admin approval.
-- Nothing here is usable; on approval an admin inserts the real categories/
-- subcategories row. Never auto-created from seller/AI input.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE pending_categories (
    id            INTEGER PRIMARY KEY,
    department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    proposed_name TEXT NOT NULL,
    proposed_by   INTEGER REFERENCES sellers(id) ON DELETE SET NULL,
    context       TEXT,                        -- e.g. the product/desc that triggered it
    status        TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at    TEXT
);

CREATE TABLE pending_subcategories (
    id            INTEGER PRIMARY KEY,
    category_id   INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    proposed_name TEXT NOT NULL,
    proposed_by   INTEGER REFERENCES sellers(id) ON DELETE SET NULL,
    context       TEXT,
    status        TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at    TEXT
);

-- pending_departments (Phase H) — a seller's proposed NEW genre/department (from
-- the signup "Others" free-text) that matched none of the launch departments.
-- Their shop sits under the 'others' bucket (public label "Others") until an
-- admin approves; on approval a real department is created and the seller (and
-- their 'others'-bucket products) are re-tagged onto it. Same governed model as
-- pending_categories — nothing public is silently created.
CREATE TABLE pending_departments (
    id            INTEGER PRIMARY KEY,
    proposed_name TEXT NOT NULL,
    proposed_by   INTEGER REFERENCES sellers(id) ON DELETE SET NULL,
    context       TEXT,
    status        TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at    TEXT
);

-- ─────────────────────────────────────────────────────────────────────────────
-- products — one row per uploaded item.
--   department_id : which of the seller's departments this product is in (required).
--   category_id   : NULL until classified/approved.
--   subcategory_id: NULL unless a subcategory applies.
--   attributes    : JSON object of per-category attribute values, e.g. {"ruling":"ruled"}.
--   price         : seller-set price, nullable (currency-agnostic numeric).
--   status        : pending → live → (or) rejected; 'removed' = seller soft-delete
--                   (never a hard DELETE). Always STARTS 'pending'. CONTENT edits
--                   (description / photo / category) revert it to 'pending' for
--                   re-review; a low-risk PRICE-only edit does NOT change status.
-- Composite FKs enforce category∈department and subcategory∈category.
-- CHECK stops a subcategory being set without a category (composite FK alone
-- can't, since a NULL category_id makes MATCH-SIMPLE skip the check).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE products (
    id             INTEGER PRIMARY KEY,
    seller_id      INTEGER NOT NULL REFERENCES sellers(id)     ON DELETE CASCADE,
    department_id  INTEGER NOT NULL REFERENCES departments(id),
    category_id    INTEGER,
    subcategory_id INTEGER,
    job_id         TEXT,                         -- optional link to intake pipeline
    image_path     TEXT NOT NULL,
    title          TEXT,
    description    TEXT,
    attributes     TEXT,                         -- JSON; per-category attribute values
    refine_rounds  INTEGER NOT NULL DEFAULT 0,   -- Phase D: AI description refinements used (cap enforced in intake)
    price          REAL,                         -- seller-set price; nullable, currency-agnostic
    status         TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'live', 'rejected', 'removed')),
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (subcategory_id IS NULL OR category_id IS NOT NULL),
    FOREIGN KEY (category_id, department_id)
        REFERENCES categories(id, department_id),
    FOREIGN KEY (subcategory_id, category_id)
        REFERENCES subcategories(id, category_id)
);

-- Keep updated_at honest without app-side bookkeeping.
CREATE TRIGGER products_touch_updated_at
AFTER UPDATE ON products
FOR EACH ROW
BEGIN
    UPDATE products SET updated_at = datetime('now') WHERE id = NEW.id;
END;

-- Indexes for the queries later phases will run.
CREATE INDEX idx_products_seller      ON products(seller_id);
CREATE INDEX idx_products_status      ON products(status);
CREATE INDEX idx_products_department  ON products(department_id);
CREATE INDEX idx_products_category    ON products(category_id);
CREATE INDEX idx_products_subcategory ON products(subcategory_id);
CREATE INDEX idx_pending_cat_status    ON pending_categories(status);
CREATE INDEX idx_pending_subcat_status ON pending_subcategories(status);

-- ─────────────────────────────────────────────────────────────────────────────
-- seller_attribute_history (DECISION 3) — per-seller record of attribute values
-- previously used, so the product form can PRE-FILL suggested values via a plain
-- SQL lookup of this seller's own most-frequently-used values for a
-- category/attribute. ZERO AI: this is NOT an AI feature and NOT a growing
-- freeform "soul.md" memory file — structured rows only. Prefill query (Phase D):
--   SELECT attribute_value FROM seller_attribute_history
--    WHERE seller_id=? AND category_id=? AND attribute_name=?
--    ORDER BY times_used DESC, last_used_at DESC LIMIT 1;
-- UNIQUE(seller_id, category_id, attribute_name, attribute_value) → upsert to
-- bump times_used. ON DELETE CASCADE + explicit DELETE-by-seller on account close.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE seller_attribute_history (
    id              INTEGER PRIMARY KEY,
    seller_id       INTEGER NOT NULL REFERENCES sellers(id)    ON DELETE CASCADE,
    category_id     INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    attribute_name  TEXT NOT NULL,
    attribute_value TEXT NOT NULL,            -- freeform, same policy as products.attributes
    times_used      INTEGER NOT NULL DEFAULT 1,
    last_used_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (seller_id, category_id, attribute_name, attribute_value)
);
CREATE INDEX idx_sah_lookup
    ON seller_attribute_history(seller_id, category_id, attribute_name);

-- ─────────────────────────────────────────────────────────────────────────────
-- chat_logs (DECISION 4) — persistence of customer questions asked of the
-- per-product chat. Created now; POPULATED and USED in Phase F (Listing
-- Intelligence) only. Persistence is SEPARATE from the chat call's own bounded
-- statelessness: logging here NEVER feeds conversation history back into the
-- chat call. A periodic batch job (never per-message) clusters these into an
-- optional seller nudge. ON DELETE CASCADE → deleted entirely when the seller
-- (and thus their products) is removed on account close.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE chat_logs (
    id            INTEGER PRIMARY KEY,
    product_id    INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    question_text TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_chat_logs_product ON chat_logs(product_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- product_nudges (Phase F) — output of the periodic batch clustering job: an
-- OPTIONAL "customers often ask about X" suggestion for the seller. The seller
-- decides (pending → applied / dismissed); the listing is NEVER auto-edited.
-- Cascades away with the product (and thus on account close), like chat_logs.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE product_nudges (
    id         INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    theme      TEXT NOT NULL,                -- the recurring question topic
    suggestion TEXT,                         -- optional one-line the seller could add
    status     TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'applied', 'dismissed')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_product_nudges_product ON product_nudges(product_id, status);

-- ─────────────────────────────────────────────────────────────────────────────
-- conversation_windows (Phase G) — the Telegram bot's DAILY conversational
-- allowance. A seller gets (by default) one window per reset period (daily at
-- local midnight): it lives until window_end and permits up to turns_cap LLM
-- turns. When no window is active (none today, or turns_cap reached), the bot
-- falls back to deterministic slash commands — no LLM parsing.
--   window_end : when this window expires (default = next local midnight).
--   turns_used : conversational turns consumed (one per handled seller message).
--   turns_cap  : per-window LLM turn budget (default 15; stored per-window so a
--                config change never mutates an already-open window).
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE conversation_windows (
    id             INTEGER PRIMARY KEY,
    seller_id      INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    window_start   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    window_end     TEXT NOT NULL,               -- expiry; default next local midnight
    turns_used     INTEGER NOT NULL DEFAULT 0,
    turns_cap      INTEGER NOT NULL DEFAULT 15,
    pending_action TEXT                         -- JSON: a destructive action awaiting
                                                -- explicit seller yes/no; NULL when none
);
CREATE INDEX idx_conv_windows_seller ON conversation_windows(seller_id, window_end);

-- ─────────────────────────────────────────────────────────────────────────────
-- conversation_messages (Phase G) — EPHEMERAL short history for ONE window, used
-- to give the LLM this session's multi-turn context (never global, never cross-
-- window, never cross-seller). Tied to a window; safe to prune/delete once the
-- window expires. Cascades on window delete (and thus on account close).
--   role : 'seller' | 'bot'.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE conversation_messages (
    id         INTEGER PRIMARY KEY,
    window_id  INTEGER NOT NULL REFERENCES conversation_windows(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('seller', 'bot')),
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX idx_conv_messages_window ON conversation_messages(window_id, id);

-- ─────────────────────────────────────────────────────────────────────────────
-- seller_credentials (Phase H) — web login for a seller. Separate table because
-- Telegram-onboarded sellers may have no web login. Password is stored as a
-- PBKDF2-HMAC-SHA256 hash with a per-seller random salt (never plaintext).
-- `login` is a seller-chosen username/phone, stored lowercased, UNIQUE.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE seller_credentials (
    seller_id  INTEGER PRIMARY KEY REFERENCES sellers(id) ON DELETE CASCADE,
    login      TEXT NOT NULL UNIQUE,
    pw_hash    TEXT NOT NULL,
    pw_salt    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────────────────────────────────────────────────────────────────────
-- seller_sessions (Phase H) — server-side web sessions (revocable, expiring).
-- token = random cookie value; csrf = synchronizer token echoed by forms.
-- Cascades on account close.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE seller_sessions (
    token      TEXT PRIMARY KEY,
    seller_id  INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    csrf       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);

-- Credential-change audit (admin visibility). One row per password create/change.
-- Passwords themselves are one-way hashed and NEVER stored/shown in cleartext —
-- this records WHEN a password was set/changed and by WHOM (seller vs admin), so
-- the admin Users page can surface password-change history. Cascades on account close.
CREATE TABLE credential_events (
    id         INTEGER PRIMARY KEY,
    seller_id  INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    event      TEXT NOT NULL,                  -- 'created' | 'changed'
    actor      TEXT,                           -- 'seller' | 'admin' | 'signup'
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_credential_events_seller ON credential_events(seller_id, created_at);
CREATE INDEX idx_seller_sessions_seller ON seller_sessions(seller_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- Seller Telegram control bot (Phase I). ONE shared PeerShop bot; each seller
-- LINKS their Telegram account to their store so the bot only ever acts on that
-- seller's data. seller_telegram is the verified binding (Telegram's authenticated
-- from.id → seller). telegram_link_codes are one-time, short-lived codes minted
-- only inside an authenticated web session, redeemed via /start <code> in the bot.
-- Both cascade on account close.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE seller_telegram (
    telegram_user_id INTEGER PRIMARY KEY,
    seller_id        INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    linked_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_seller_telegram_seller ON seller_telegram(seller_id);

CREATE TABLE telegram_link_codes (
    code       TEXT PRIMARY KEY,
    seller_id  INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    used_at    TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Self-service bot onboarding (new-shop creation FROM the bot, no pre-existing
-- web login required -- see telegram_link.create_shop_with_login). Holds
-- in-progress multi-turn Q&A state so a user can close Telegram mid-onboarding
-- and resume later without losing earlier answers.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE bot_onboarding_sessions (
    telegram_user_id INTEGER PRIMARY KEY,
    step             TEXT NOT NULL,
    collected_json   TEXT NOT NULL DEFAULT '{}',
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Forwarded-burst staging (raw ingest before debounce bundling + AI extraction).
-- A seller forwards a rapid-fire dump of text/photos from another chat; each
-- forwarded message lands here as 'unprocessed' first. debounce_worker.py groups
-- rows per seller_id once the burst has gone quiet, then hands the bundle to
-- intake.py's AI-draft path -> seller_actions.post_product() (always 'pending').
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE bot_raw_ingest (
    id         INTEGER PRIMARY KEY,
    seller_id  INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    media_path TEXT,
    raw_text   TEXT,
    bundle_id  TEXT,
    status     TEXT NOT NULL DEFAULT 'unprocessed',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_bot_raw_ingest_seller_status ON bot_raw_ingest(seller_id, status);
CREATE INDEX idx_bot_raw_ingest_bundle ON bot_raw_ingest(bundle_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- Product photos (multi-image). A product may carry up to 4 photos (enforced in
-- app code, not schema). position 0 is the PRIMARY photo and is ALSO mirrored to
-- products.image_path, so every existing single-image render (cards, /image/<id>,
-- dashboard thumbnail) keeps working unchanged; the gallery reads all rows here
-- and falls back to products.image_path when a product has none (web-added / old).
-- Cascades away with the product.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE product_photos (
    id         INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    image_path TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,     -- 0 = primary
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_product_photos_product ON product_photos(product_id, position);

-- ─────────────────────────────────────────────────────────────────────────────
-- Telegram /post wizard state (Phase J). A deterministic, NO-LLM guided flow:
-- /post → name → (department, only if the seller sells in >1) → price → up to 4
-- photos → description → post (status='pending'). ONE in-progress draft per seller
-- (a fresh /post overwrites it); the row is deleted on finalize or /cancel. State
-- lives in the DB so it survives a bot restart. Decoupled from conversation_windows
-- so it NEVER consumes the daily conversational allowance and always works.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE post_drafts (
    seller_id  INTEGER PRIMARY KEY REFERENCES sellers(id) ON DELETE CASCADE,
    step       TEXT NOT NULL,                  -- name|department|price|photos|description
    title      TEXT,
    department TEXT,
    price      REAL,
    photos     TEXT NOT NULL DEFAULT '[]',     -- JSON list of saved image paths (<=4)
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─────────────────────────────────────────────────────────────────────────────
-- SEED — departments + starter taxonomy (CUSTOMER-oriented umbrella genres).
--
-- Departments are broad, shopper-mental-model umbrellas — NOT Amazon's raw
-- compound labels and NOT seller jargon. Grouped by where a CUSTOMER would look:
-- e.g. appliances live under Home & Kitchen (not Electronics), because that's
-- where a shopper expects a washing machine or microwave. Each umbrella holds the
-- concrete categories people browse. Subcategories/attributes are NOT seeded here
-- — they grow later via the governed pending queue rather than being invented now.
-- 'others' / 'uncategorized' remain system buckets (not pickable launch depts).
-- ─────────────────────────────────────────────────────────────────────────────
INSERT INTO departments (name) VALUES
    ('electronics'),
    ('home & kitchen'),
    ('apparels'),
    ('wellness'),
    ('grocery'),
    ('toys & baby'),
    ('automotive & industrial'),
    ('books & media'),
    ('bags & luggage'),
    -- System buckets (Phase H) — NOT launch/pickable. 'others' = seller-proposed
    -- genre pending admin approval (buyers see "Others"); 'uncategorized' = seller
    -- doesn't know their line yet.
    ('others'),
    ('uncategorized');

-- categories under each umbrella department (the shopper-facing browse level).
-- Subcategories/attributes are intentionally NOT seeded — added later via the
-- governed pending queue, not invented now.
INSERT INTO categories (department_id, name)
SELECT d.id, v.name FROM departments d
JOIN (
    SELECT 'electronics'  AS dept, 'mobiles'          AS name UNION ALL
    SELECT 'electronics',          'computers'               UNION ALL
    SELECT 'electronics',          'tv'                      UNION ALL
    SELECT 'electronics',          'electronics'             UNION ALL

    SELECT 'home & kitchen',       'home'                    UNION ALL
    SELECT 'home & kitchen',       'kitchen'                 UNION ALL
    SELECT 'home & kitchen',       'appliances'              UNION ALL
    SELECT 'home & kitchen',       'pets'                    UNION ALL

    SELECT 'apparels',             'men''s fashion'          UNION ALL
    SELECT 'apparels',             'women''s fashion'        UNION ALL
    SELECT 'apparels',             'kids'' fashion'          UNION ALL

    SELECT 'wellness',             'beauty'                  UNION ALL
    SELECT 'wellness',             'health'                  UNION ALL
    SELECT 'wellness',             'sports'                  UNION ALL
    SELECT 'wellness',             'fitness'                 UNION ALL

    SELECT 'grocery',              'grocery'                 UNION ALL

    SELECT 'toys & baby',          'toys'                    UNION ALL
    SELECT 'toys & baby',          'baby products'           UNION ALL

    SELECT 'automotive & industrial', 'car'                 UNION ALL
    SELECT 'automotive & industrial', 'motorbike'           UNION ALL
    SELECT 'automotive & industrial', 'industrial'          UNION ALL

    SELECT 'books & media',        'books'                   UNION ALL
    SELECT 'books & media',        'movies'                  UNION ALL
    SELECT 'books & media',        'music'                   UNION ALL
    SELECT 'books & media',        'video games'             UNION ALL

    SELECT 'bags & luggage',       'bags'                    UNION ALL
    SELECT 'bags & luggage',       'luggage'                 UNION ALL

    -- 'others' catch-all category (under the 'others' system department) so an
    -- admin can always file a product that fits no specific category and still
    -- promote it, rather than leaving it stuck uncategorized.
    SELECT 'others',               'others'
) v ON v.dept = d.name;
