# PeerShop

A **multi-department, hyperlocal, multi-vendor marketplace**. Neighbor sellers
run shops across five departments; they upload products (via Telegram, reusing
the existing photo pipeline, or web); AI drafts each listing and *suggests*
department/category/subcategory; a human seller confirms and a human admin
approves before anything goes live. Each product also gets its own tiny
customer-facing chat assistant scoped to that one product's description.

> **Scope note:** This project started as a fashion-only catalog (that history is
> in the codebase and git). It has since expanded to the multi-department vision
> below. Where the current code still reflects the old flat/fashion model, this
> document marks it as **[to change]** — the Phase A–F plan restructures it.

## What this is (and isn't)

- **Multi-department, not single-category.** Departments are broad,
  **customer-oriented umbrella genres** (not seller jargon): **electronics,
  home & kitchen, apparels, wellness, grocery, toys & baby, automotive & industrial,
  books & media, bags & luggage** — plus the system buckets `others` /
  `uncategorized`. Grouped by where a *shopper* looks (e.g. appliances live under
  Home & Kitchen, not Electronics). A hyperlocal marketplace of local shops, not
  one vertical. *(History: launched as grocery/boutique/electronics/stationery/food;
  re-grouped into the umbrella taxonomy above — see Starter taxonomy.)*
- **A seller can belong to MULTIPLE departments.** A general store may sell both
  groceries and electronics. **Never force one department per seller.**
- **Human-in-the-loop everywhere AI touches.** AI *suggests* (department,
  category, subcategory, description); the seller confirms; the admin promotes.
  No AI suggestion is ever final on its own, and **AI is never the only way in** —
  every AI-suggested step has a symmetric manual path (self-select department(s),
  hand-write the description, self-select taxonomy).
- **Governed taxonomy, no silent growth.** New categories/subcategories are
  **never** auto-created from seller input — they go to a pending-approval queue
  for an admin. (This is a **change** from current behavior; see Governance.)
- **Not a Meesho mirror.** Sellers may *optionally* link out to a Meesho
  storefront. Plain outbound link only — no scraping, no Meesho API sync (it's
  partner-gated, out of scope).
- **Not a general chatbot.** Per-product chat answers from that product's own
  description only.

## Build philosophy — read before writing code

- **Lean and native first.** SQLite. PeerShop's core (schema, serve, review,
  categorize, chat, llm) is **Python stdlib only**. Heavy pipeline deps (rembg,
  opencv, google-generativeai) live in the project venv `.venv/`, used only by
  intake/describe. No Odoo, no ERP, no web framework.
- **Every AI call is narrow, bounded, single-purpose.**
  - No conversation history, no agent framework, no big system prompts / tool
    schemas. Small prompt in, small answer out.
  - Narrow **text** calls (categorization, subcategorization, per-product chat,
    the Phase F question-clustering call) use a **free-tier provider** (Groq
    default; OpenRouter alt) precisely because they're small — see `llm.py`.
    Bloating them with history breaks free-tier tokens-per-minute limits.
  - The one exception is the description refinement loop (Phase D), which is
    still bounded — **capped at 2–3 rounds** — then the seller is nudged to
    approve or hand-edit, rather than looping (cost control).
- **Work in phases. Stop between them.** After each phase: STOP, show real
  artifacts (file contents, real schema, a way to test it), wait for explicit
  go-ahead. Never chain phases autonomously.

## Taxonomy model (replaces the current flat `categories` table)

A three-level governed hierarchy, plus lightweight attributes:

```
department  →  category (scoped to a department)  →  subcategory (scoped to a category)
                                                   +  attributes (lightweight, per-category)
```

- **departments** — 9 customer-oriented umbrella genres (electronics, home & kitchen,
  apparels, wellness, grocery, toys & baby, automotive & industrial, books & media,
  bags & luggage) + `others`/`uncategorized` buckets. See Starter taxonomy below.
- **categories** — scoped to exactly one department. Closed list, governed.
- **subcategories** — genuine *type* distinctions within a category. Closed list,
  governed the same way as categories. **None seeded in the current taxonomy** —
  added later via the governed pending queue rather than invented.
- **attributes** — **different from subcategories, and NOT governed via the
  pending queue.** Lightweight per-category variant properties (e.g. notebook
  ruling: ruled/unruled/blank; clothing size/color).
  **Resolved (decision 2, v1):** attribute **values are freeform text** — no
  pending-approval queue, no enum enforcement, **no check beyond basic input
  sanitization.** A deliberate simplicity choice for v1. Shape: a
  `category_attributes` table declares which attribute *names* a category offers
  (plus optional, **non-enforced** suggested values, only to seed a dropdown);
  the chosen values are stored as freeform text in `products.attributes` (JSON).
  See Attribute-value prefill below.

### Attribute-value prefill (per-seller history — STRUCTURED, not AI, not a memory file)

Decision 3. To save sellers re-typing the same attribute values, a
**`seller_attribute_history`** table records each value a seller has used:
`(seller_id, category_id, attribute_name, attribute_value, times_used,
last_used_at)`. When a seller lists a new product, the form **pre-fills suggested
attribute values via a plain SQL lookup** of that seller's own most-frequently-
used past values for the given category/attribute — **zero AI calls, zero added
cost.**

- This is **NOT an AI feature** and **NOT a growing freeform file.** Do **not**
  build anything resembling a per-seller `soul.md`-style markdown/text memory that
  accumulates and gets re-read into prompts. **Structured rows only.**
- **Deletion governance:** all of a seller's rows are deleted entirely
  (`DELETE FROM seller_attribute_history WHERE seller_id = ?`, also covered by
  `ON DELETE CASCADE`) when that seller closes their account.
- The **table is created in Phase A**; the prefill is **wired into the product
  form in Phase D**.

### Starter taxonomy — CUSTOMER-ORIENTED umbrellas (seeded by `schema.sql`)

Departments are broad, shopper-mental-model umbrellas; each holds the concrete
categories a customer browses. `init_db.py --show` prints the live tree.

- **electronics:** mobiles, computers, tv, electronics
- **home & kitchen:** home, kitchen, **appliances**, pets
- **apparels:** men's fashion, women's fashion, kids' fashion
- **wellness:** beauty, health, sports, fitness
- **grocery:** grocery
- **toys & baby:** toys, baby products
- **automotive & industrial:** car, motorbike, industrial
- **books & media:** books, movies, music, video games
- **bags & luggage:** bags, luggage
- *(system buckets, not pickable)* **others**, **uncategorized**

**Design intent:** grouped by where a *customer* would look, not by seller/tech
logic — e.g. **appliances live under Home & Kitchen, not Electronics** (a shopper
hunts a washing machine under "home"). Umbrellas merge related lines
(men's/women's/kids' fashion → Apparels; beauty/health/sports/fitness → Wellness);
things that don't merge accurately are kept separate (Grocery, Bags & Luggage).

**No subcategories or attributes are seeded** in this taxonomy — they grow later
via the governed pending queue rather than being invented. This replaced the
original grocery/boutique/electronics/stationery/food launch set (the old
fashion/grocery-specific categories and their subcategories/attributes are gone).

### Governance (unchanged rule, now enforced by schema)

- **No silent auto-creation.** If a seller's item matches no existing category or
  subcategory, it lands in **`pending_categories`** / **`pending_subcategories`**
  for **manual admin approval** — never instantly created or usable.
- **Attributes are the exception to governance:** attribute *values* are freeform
  text (sanitization only), never queued (decision 2). Only categories and
  subcategories are governed via the pending queue.
- **[to change]** Current code violates the no-auto-create rule:
  `categorize.set_product_category` (`create_if_missing=True`) and
  `intake.ingest_photo(seller_category=…)` currently auto-create categories.
  Phase A/B replaces that with the pending queue.

## Flows

### Seller onboarding (new)

1. Seller signs up via the Telegram bot.
2. Bot requests one or more **shop/storefront photos** (optional — a seller may
   take the manual path in step 3 instead). **Any uploaded photos are persisted
   to disk and recorded in the `seller_photos` table (one-to-many)** — they are
   durable shop assets for the Phase E profile page, *not* throwaway inputs that
   get discarded once the department classification has read them.
3. **Two symmetric paths in — AI suggestion is never the only way in:**
   - **AI path.** AI analyzes the photo(s) and **suggests department(s)** (e.g. a
     clothing rack → apparels), using **`gemini-3.1-pro-preview`** — the
     higher-quality multimodal model, deliberately NOT the cheap
     `gemini-3.1-flash-lite` used for per-product image-id (see Phase C).
   - **Manual path (decision 1).** The seller may **skip AI shop-photo
     classification entirely and self-select their department(s)** directly — the
     same symmetry as the manual-description path for products (a first-class
     parallel path, not just a fallback). Skipping AI here means **0
     shop-classification calls.**
4. Seller **confirms or adjusts** the AI suggestion (AI path) — never final
   without confirmation. **If the AI returns no confident department (a bad or
   non-storefront photo, or an API error), the flow does NOT dead-end** — it
   routes straight to the manual self-select prompt with the full department
   list. This decision is made by `onboarding.propose_departments()`, which
   returns `needs_manual` + a ready-to-show prompt so a seller is never stranded
   with zero departments.
5. Seller profile is created, tagged with the confirmed / self-selected
   department(s) (one or many). `set_departments` requires at least one.

### Per-product listing (extends the existing pipeline)

**The description can be AI-drafted OR hand-written — the seller's choice, and
these run in parallel, switchable at any point (not AI-first-then-maybe-manual).**

1. Seller sends product photo(s) to the bot.
2. Existing pipeline runs background removal (always — that's image processing,
   independent of how the description is authored).
3. **Manual-description path — available from the very first message and at any
   point thereafter.** A seller may write their own title + description instead
   of AI drafting. Choosing it from the start **skips the AI describe calls
   entirely**; switching to it mid-refinement **ends AI drafting immediately**.
   This is a first-class parallel path, not just a fallback.
4. **AI-draft path.** AI drafts title + description (`describe.py`, Gemini-3
   two-step: cheap image-id → quality copy).
5. AI **suggests category then subcategory** — department is already known from
   the seller's profile, so this is a two-step *taxonomy* classification
   (category → subcategory), each a narrow/bounded/no-history call like
   `categorize.py`. (The seller may also self-select these.) Category-scoped
   **attribute values are pre-filled** from the seller's own
   `seller_attribute_history` via plain SQL (no AI) — see Attribute-value prefill.
6. Bot sends the draft description to the seller for review.
7. Seller can request changes in plain language; the bot regenerates
   incorporating the addition. **Capped at 2–3 rounds.**
8. **After the cap, hand-edit is the accepted end state** — the seller submits
   their own final description text directly, and **no further AI calls are
   made**. (The seller may also simply approve-as-is at the cap.)
9. Seller approves → product enters as **status='pending'** (as now).
10. **Promotion is UNCHANGED:** `review.py` — explicit human admin review, no
    auto-publish, regardless of anything above.

### Listing intelligence (post-launch — Phase F)

*Builds LAST (after E), once real customer chat activity exists.* The product's
differentiator: turn repeated customer questions into a description improvement,
without ever auto-editing the listing or bloating the chat call.

1. **Logging.** `chat.py` writes each customer question to a new **`chat_logs`**
   table `(product_id, question_text, created_at)`. This adds **persistence only**
   — **no conversation history is ever fed back INTO the chat call**; the chat
   stays bounded and stateless exactly as today. Logging is separate from the
   chat's own statelessness.
2. **Batch clustering.** A **periodic batch job** (admin-triggered or nightly —
   *never* per-message, *never* real-time) makes **one bounded AI call per product
   per run**: a small prompt of that product's recent questions, asking for the
   top 1–2 recurring themes **not already covered by the description**. Narrow
   free-tier text call, same discipline as `categorize.py`.
3. **Optional seller nudge.** The result surfaces to the seller as a suggestion —
   *"Customers often ask about X — want to add this to your description?"* The
   **seller decides; the listing is never auto-edited.**
4. **Deletion governance.** `chat_logs` rows are deleted entirely when the seller
   closes their account (via `ON DELETE CASCADE`, products → sellers → chat_logs).

## Seller-facing website

- **Per-seller profile page (Facebook-style):** shop photo(s) at top (from
  `seller_photos`, persisted at onboarding), seller info, then their products
  grouped by **department → category**, with a
  **subcategory dropdown/filter** and **attribute pills** shown per category *when
  any exist* (none are seeded in the current taxonomy, so these appear only once a
  category gains governed subcategories/attributes).
- **Cross-seller browse stays:** the current all-sellers listing (`serve.py`)
  remains as a cross-seller view; each seller *additionally* gets their own
  dedicated profile page.

> **Seller login / dashboard (Phase H — H1 DONE).** `serve.py` now has seller
> **signup + login + logout** (cookie sessions, `auth.py`) and a **dashboard**
> (`/seller/dashboard`) where a logged-in seller sees their listings and can
> **edit price** / **remove** (soft) — all via `seller_actions.py`. A "Seller?
> Log in or create an account" link sits in the public listing header.
> **H2 DONE:** the dashboard now supports the full product lifecycle from the web
> — **add a product** (stdlib multipart photo upload → `seller_actions.post_product`,
> lands pending), **edit price** (stays live), **replace photo** (content edit →
> reverts to pending), and **soft-remove** — all ownership- + CSRF-checked. Images
> are stored under `media/products/` and served to the seller via an authenticated
> `/seller/product/<id>/image` route. Web add stores the photo as uploaded (no
> bg-removal / AI draft — that remains the Telegram/intake path).

## AI calls & cost tally

Which calls each flow makes, and the min–max range. **Groq text calls are
free-tier (effectively $0); the cost that matters is Gemini calls**, split into
cheap `gemini-3.1-flash-lite` (image-id) and paid-quality `gemini-3.1-pro-preview`
(shop classification, description copy, refinement regenerations). Background
removal (rembg) is local compute — no API cost. Per-product chat is
post-publish/customer-facing, billed per customer question, not part of seller
onboarding/listing.

**Seller onboarding — once per seller:**

| Call | Model | Count |
|------|-------|-------|
| Shop-photo department classification | `gemini-3.1-pro-preview` (quality) | **0–1** |

- **0** if the seller self-selects department(s) manually (decision 1, manual
  onboarding fallback); **1** if they use AI shop-photo classification.

**Per-product listing — per product**, range depends on path:

| Call | Model | Full-AI (max) | Manual-desc (min) |
|------|-------|:---:|:---:|
| describe: image-id | `gemini-3.1-flash-lite` (cheap) | 1 | 0 |
| describe: copy | `gemini-3.1-pro-preview` (quality) | 1 | 0 |
| refinement regenerations | `gemini-3.1-pro-preview` (quality) | up to 3 | 0 |
| category suggestion | Groq (free) | 1 | 0–1 |
| subcategory suggestion | Groq (free) | 1 | 0–1 |
| **Gemini (paid) total** | | **up to 5** (1 cheap + 4 quality) | **0** |
| **Groq (free) total** | | 2 | 0–2 |

- **Full-AI-refinement path (max):** ~5 Gemini calls (1 cheap image-id + 1 copy +
  up to 3 refinement regenerations) + 2 free Groq taxonomy calls.
- **Skip-AI / manual-description path (min):** **0 Gemini calls** — writing the
  description from the first message skips both describe calls and all
  refinement; taxonomy may still be AI-suggested (2 free Groq) or self-selected
  (0). Hand-editing *after* the cap adds **no** further calls.
- So per product the paid range is **0 → 5 Gemini calls**; the free-tier range is
  0 → 2 Groq calls. A seller who always hand-writes costs effectively nothing per
  product beyond the (optional) one-time onboarding classification.

**Post-launch — Listing Intelligence (Phase F), per product per batch run:**

| Call | Model | Count |
|------|-------|-------|
| Recurring-question clustering | Groq (free), bounded | **1 / product / batch** |

- Runs in a **periodic BATCH** (admin-triggered or nightly), **never per customer
  message.** Small prompt (recent questions for one product → top 1–2 recurring
  themes not already in the description). Customer-facing per-product chat itself
  is billed per question and is separate from onboarding/listing (unchanged).

## Existing foundation — reuse, do NOT rebuild

- **`/root/pipeline/product_intake.py` — the REAL image pipeline.** PeerShop
  reuses `receive_photo` → `remove_background` (rembg + edge-detection fallback)
  → `enhance_image`. It does **not** use that file's `describe_product` (pins the
  retired `gemini-2.0-flash`, 404, unconfigurable) nor `create_listing` (stamps
  `status:"published"`). Heavy deps live in `.venv/`.
- **Hermes** is an always-on production agent operating the Telegram side.
  PeerShop does **not** replace Hermes or duplicate its Telegram polling. Assume
  photo delivery lands as a **file path**; PeerShop picks up from there. (The
  onboarding/listing bots in Phases C–D define how PeerShop is invoked from that
  handoff — to be designed, not by re-polling Telegram.)
  **AMENDED (Phase I, with user consent):** PeerShop DOES now run its own Telegram
  long-poller (`telegram_bot.py`) for the **seller store-control bot** — but on a
  SEPARATE PeerShop bot token (`PEERSHOP_TELEGRAM_TOKEN`), a distinct BotFather bot
  that never touches Hermes's token/poller (two pollers on one token 409-conflict).

## Current codebase (what exists today)

- `schema.sql` / `init_db.py` — SQLite schema. **Phase A** replaces the old flat
  `sellers`/`products`/`categories` with the governed department→category→
  subcategory hierarchy, `category_attributes`, `pending_categories` /
  `pending_subcategories`, seller↔department **many-to-many**, plus
  **`seller_attribute_history`** (decision 3), **`chat_logs`** (decision 4),
  **`seller_photos`** (Phase C — persisted shop photos), **`refine_rounds`** on
  products (Phase D refinement cap), **`product_nudges`** (Phase F clustering
  output), and **Phase G**: `conversation_windows` (+`pending_action`) /
  `conversation_messages` (bot daily allowance + ephemeral history),
  `products.price`, and the `'removed'` product status; **Phase H**:
  `seller_credentials` (hashed web login), `seller_sessions`, and **Phase K**
  `credential_events` (password-change audit); plus
  `sellers.owner_name`, two system-bucket departments (`others`, `uncategorized`),
  and `pending_departments` (governed new-genre queue); **Phase I**:
  `seller_telegram` (verified Telegram↔seller binding) and `telegram_link_codes`
  (one-time link codes); **Phase J**: `product_photos` (up to 4 per product;
  position 0 mirrored into `products.image_path`) and `post_drafts` (Telegram
  /post wizard state, one per seller).
- `llm.py` — shared narrow-call plumbing (Groq default; Cloudflare UA fix).
- `categorize.py` — **Phase B done.** Two narrow Groq calls: department-aware
  category → subcategory, each validated against the closed governed list (off-
  list/hallucinated → None). Seller overrides (`assign_category`/`assign_subcategory`,
  plus back-compat `set_product_category` for intake/review) route unknown names
  to `pending_categories`/`pending_subcategories` — no auto-create.
- `describe.py` — Gemini-3 two-step describe (image-id `gemini-3.1-flash-lite` →
  copy `gemini-3.1-pro-preview`, text-only, with fallback), on the **google.genai**
  SDK. Phase D added a bounded text-only `refine()` for the refinement loop.
- `attributes.py` — **Phase D**, stdlib. Per-category attribute values (freeform,
  decision 2) stored as JSON on `products.attributes`, plus zero-AI
  `seller_attribute_history` prefill/record (decision 3).
- `onboarding.py` — **Phase C** + **Phase H** genre governance. Adds
  `owner_name`; `match_department` (Groq semantic map of a free-text line → a
  launch department, ≥80% gate); `request_department` (→ `pending_departments`)
  and admin `approve_department`/`reject_department` (create real dept + re-tag
  seller/products out of the `others` bucket). `launch_departments()` excludes the
  `others`/`uncategorized` system buckets. Original Phase C: seller creation,
  shop-photo persistence
  (`seller_photos`), quality multimodal department suggestion
  (`gemini-3.1-pro-preview`, once per seller) + a manual self-select fallback,
  and confirmed-department assignment (many-to-many, closed-list validated).
- `intake.py` — **Phase D done.** The per-product listing flow: AI-draft +
  manual description paths, bounded refinement, department resolution, governed
  taxonomy, attribute prefill. Always `status='pending'`. Heavy-dep (venv).
- `serve.py` — **Phase E done.** Cross-seller listing + per-product detail/chat,
  plus per-seller profile pages (`/seller/<id>`) with department→category
  grouping and server-side subcategory/attribute filters. Stdlib http.server.
  Product detail also has a **"Message the seller on WhatsApp"** click-to-chat
  button (`wa.me` deep link built client-side from the seller's phone `contact` +
  the customer's typed question + the product URL — Instagram-ad style; pure
  frontend, no bot/Telegram connection; hidden when `contact` isn't a usable
  phone). AI chat is kept alongside. `wa_digits()` normalizes the phone;
  product-URL base derives from the request `Host` header.
- `chat.py` — per-product chat (Groq, stateless, description-scoped). **Phase F**:
  writes each answered question to `chat_logs` (best-effort, persistence only —
  still no history fed into the call; the call stays stateless).
- `insights.py` — **Phase F done.** The batch clustering job (`run_batch`): one
  bounded Groq call per product over `chat_logs` → pending `product_nudges`;
  seller `apply`/`dismiss`; never auto-edits.
- `approvals.py` — **Phase K.** THE single approve/reject decision path (stdlib):
  `pending_queue`, `approve(id, category=…)`, `reject(id)`. Guards: a product
  leaves 'pending' only from a still-'pending' row; **can't go live uncategorized**
  (approve may set a category via governed `categorize.assign_category` — unknown →
  queued, never auto-created); best-effort **Telegram notice to the seller** on any
  decision (via `telegram_api`, only if linked). CLI `--list/--approve/--reject` is
  how a human shell — or **Hermes, when no human admin is around** — decides.
- `auth.py` — **Phase H** (+ **Phase K** audit). Seller web auth (PBKDF2 + sessions).
  `set_credentials` now **audits every password set** into `credential_events`
  ('created' first time, 'changed' after; `actor` = seller/admin) — best-effort,
  never blocks the change; passwords stay one-way hashed (never shown). Adds
  `get_login`, `credential_history`, `all_credential_events` for the admin Users page.
- `telegram_api.py` — **Phase K.** Stdlib Telegram HTTP primitives (token load,
  `api`, `send_message`, `get_file`, `download_file`) shared by `telegram_bot.py`
  (poller) and `approvals.py` (seller notices). Split out so the stdlib-only core
  can send messages without importing `telegram_bot` (which pulls google.genai).
- `review.py` — **Phase K.** Admin review queue (own port 8001, supervised by
  `run_review.sh`). Gated by a shared **`PEERSHOP_ADMIN_PASSWORD`** (env/.env →
  signed HttpOnly cookie). Each card shows photo **gallery · department · price ·
  category · seller**; promote/reject/category route through `approvals.py` — the
  SAME path as the CLI/Hermes — so guards + seller notification are identical.
  Promotion still human-gated; never auto-publishes. Also serves an admin **Users
  page** (`/users`): every seller's data (account, login, contact, departments,
  product counts, Telegram-link, created, last login) + **password-change history**
  (from `credential_events` — passwords are never shown) + a per-seller **password
  reset** (`/users/<id>/reset`, audited as actor='admin', notifies the seller).
  **Admin category override (decision):** the review card's category dropdown lists
  **ALL categories across every department** (grouped by `<optgroup>`); assigning one
  from another department **moves the product into that department** (via
  `categorize.assign_category_by_id`, which rewrites `department_id`+`category_id`
  and clears subcategory). Admin-only — the seller-facing self-select stays
  department-scoped/governed. The picker is a **single searchable combobox** (type
  to filter categories letter-by-letter; hidden field carries the chosen category
  id). If nothing fits, the admin types a new name in the SAME box and clicks
  **Add category** (with a confirm) → `categorize.admin_add_category` **creates** it
  under the product's department and assigns it (admin privilege — direct create,
  not queued, since the admin is the approver; sellers' unknown names still queue).
  **Add category** stays disabled unless the typed name matches NO existing
  category (client-side check against a `CATSET` of all category names) — an
  existing name must be picked from the list, not re-added.
  An **'others' catch-all category** (under the `others` department, seeded in
  `schema.sql`) is always available so nothing need stay stuck uncategorized.
- `seller_actions.py` — **Phase G.** THE shared seller-behalf write layer (stdlib):
  `post_product` (always pending), `remove_product` (soft → 'removed'),
  `update_price` (price-only, stays live), `get_my_products`. Ownership-checked;
  reuses categorize/attributes. `intake.py` now routes its product write through
  here (one write path).
- `conversation.py` — **Phase G.** Daily conversational window + turn-cap
  bookkeeping (no LLM/Telegram): `get_or_open_window` (active/opened/exhausted;
  one window per local-midnight reset; `turns_cap` default 15 per-window),
  `record_turn`/`window_history` (bounded, per-window), `set_pending_action`,
  `prune_expired_messages`.
- `auth.py` — **Phase H.** Seller web auth (stdlib): PBKDF2-HMAC-SHA256 password
  hashing (per-seller salt, constant-time compare), and server-side sessions
  (`seller_sessions`, token + CSRF, 7-day expiry, revocable). `serve.py` adds
  `/seller/signup|login|logout|dashboard` + price/remove POSTs (cookie
  HttpOnly+SameSite=Strict, CSRF token per session). Dashboard actions call
  `seller_actions`. Plain HTTP on LAN (no TLS) — a dev posture, flagged.
- `telegram_link.py` — **Phase I.** Telegram↔seller identity (stdlib): one-time
  link codes minted inside an authed dashboard session (`create_link_code`),
  redeemed via `/start <code>` to bind the Telegram-authenticated `from.id`→seller
  (`redeem_link_code`, single-use/expiring), and `resolve_seller` (the per-message
  security lookup — never trusts message text). Tables `seller_telegram`,
  `telegram_link_codes`.
- `telegram_bot.py` — **Phase I** (+ **Phase J** photos). Stdlib long-polling
  connector (urllib) for the shared PeerShop seller-control bot: getUpdates loop →
  resolve sender→seller (or prompt to link) → route. Routing: `/start <code>` link;
  `/post` → the guided wizard; `/cancel`; mid-wizard photos (getFile + download to
  `media/products/`) and text → `post_wizard`; everything else →
  `bot.handle_message(seller_id, text, turns_cap=100)`. Robust (offset advances
  even on handler error). Own token `PEERSHOP_TELEGRAM_TOKEN` (NOT Hermes's).
  Supervised by `run_bot.sh` (`stop` = process-group kill, no `pkill` pattern).
  CLI `--getme`/`--once`. Per-seller auth so seller_1 can't touch seller_2's data.
- `post_wizard.py` — **Phase J.** The Telegram `/post` product-upload wizard — a
  deterministic, **NO-LLM** state machine: name → (department, only if the seller
  sells in >1) → price → **up to 4 photos** → description → `seller_actions.post_product`
  (always `status='pending'`). State is one `post_drafts` row per seller (survives
  a bot restart), fully decoupled from the daily conversational allowance so it
  ALWAYS works. Photos land in `product_photos` (position 0 mirrored into
  `products.image_path`). Stdlib only; `db_path=None` resolves at call time.
- `bot.py` — **Phase G.** Telegram message handler (`handle_message`; Hermes
  hands off, no polling). Slash commands (`/post /remove /price /mystore /help`,
  deterministic, no LLM, always available) + natural-language path (Gemini
  `gemini-3.1-flash-lite` parses message + this-window history into a STRUCTURED
  intent; Python resolves against the seller's own products by EXACT match,
  clarifies on ambiguity, requires explicit yes/no before remove). All actions
  via `seller_actions`.
- `seed_demo.py` — dev demo data.

## Phases (A–H) — stop after each for review

Prior work (fashion schema, categorize, serve, review, chat, intake, describe)
is DONE and is now **superseded/extended** by this plan. Do not skip ahead.

- **Phase A — Schema restructure (DO FIRST, then stop).** departments,
  dept-scoped categories, subcategories, `pending_categories` /
  `pending_subcategories` approval queues, the (freeform, ungoverned) attributes
  mechanism, seller↔department **many-to-many** support, **`seller_attribute_history`**
  (decision 3), and **`chat_logs`** (decision 4 — created now even though Phase F
  uses it later; no harm having it ready). Show the schema (and the finalized
  starter taxonomy) before seeding anything else.
- **Phase B — categorize.py** → two-step, department-aware category then
  subcategory; narrow/bounded; governed (unmatched → pending queue, no
  auto-create).
- **Phase C — Seller onboarding (DONE)** — shop-photo department classification +
  seller confirmation → multi-department profile, **plus a manual fallback: the
  seller can skip AI classification and self-select department(s)** (decision 1).
  **Shop photos are persisted to disk and recorded in `seller_photos` (one-to-many)
  and reused by the Phase E profile page — never discarded after classification.**
  Departments are the fixed closed five: an unknown name is rejected, not queued.
  Implemented in `onboarding.py`.
  **Model:** this classification uses **`gemini-3.1-pro-preview`** (the
  higher-quality multimodal model), *not* the cheap `gemini-3.1-flash-lite` used
  for per-product image-id. Reasoning: this call happens **once per seller** at
  signup, so it's a one-time cost where a correct department read matters more
  than per-call price — a deliberate quality-over-cost tradeoff, the inverse of
  the per-product image-id call which is high-volume and cost-sensitive.
- **Phase D — Per-product listing / refinement loop (DONE).** In `intake.py`:
  AI-draft path (`ingest_photo`) + first-class **manual path** (`ingest_manual`,
  0 Gemini); bounded refinement (`refine_description`, `REFINE_CAP=3`, counted in
  `products.refine_rounds`) ending in approve-as-is or hand-edit
  (`set_manual_description`, which locks further AI). Department resolved from the
  seller's profile (a product is scoped to one of their departments). Taxonomy:
  seller self-select wins, else the governed AI two-step; **subcategory** included.
  **`seller_attribute_history` prefill** and freeform value storage live in
  `attributes.py` (decision 3, plain SQL, zero AI). Everything still lands
  `status='pending'`. `describe.py` gained a bounded, text-only `refine()`.
- **Phase E — Seller profile pages (DONE).** In `serve.py`: `/seller/<id>`
  Facebook-style page — shop photos (from `seller_photos`, served via
  `/seller-photo/<id>`), department tags + contact/Meesho, live products grouped
  by **department → category**, a **subcategory `<select>` dropdown** per category,
  and **attribute filter pills** (freeform values) on category drill-down. All
  filtering is server-side via query params (`dept`/`cat`/`sub`/`af=name:value`),
  stdlib only. Cross-seller browse kept; product cards + detail now link to the
  seller's profile.
- **Phase F — Listing Intelligence (DONE — final phase).** `chat.py` logs each
  answered question to `chat_logs` (persistence only, chat stays stateless).
  `insights.py` `run_batch()` is the **periodic batch job** (admin-triggered/
  nightly, never per-message): **one bounded free-tier Groq call per eligible
  product** clusters recurring questions into the top 1–2 themes the description
  doesn't cover, stored as **pending `product_nudges`**. The seller decides
  (`apply_nudge`/`dismiss_nudge`); the listing is **never auto-edited**, and a
  dismissed/seen theme isn't re-nudged. Nudges cascade away on account close.
- **Phase G — Seller Telegram bot + shared action layer (DONE — post-plan).**
  Foundation `seller_actions.py` is the single seller-behalf write path (post /
  soft-remove / update_price / get_my_products; intake reconciled through it).
  `conversation.py` grants a **daily conversational allowance** (one window per
  local-midnight reset, `turns_cap` 15, ephemeral per-window history).
  `bot.py` `handle_message` (Hermes handoff, no polling): with an active window,
  **Gemini** (`gemini-3.1-flash-lite`) parses the message + window history into a
  **structured intent** — the LLM never acts; Python requires an EXACT match on
  the seller's own products, clarifies on ambiguity, and requires explicit
  confirmation before a destructive remove. When the allowance is spent (or
  always), deterministic **slash commands** (`/post /remove /price /mystore
  /help`, no LLM) call the same `seller_actions`. Bot posts/edits still land
  `status='pending'` — never bypass admin review. Schema added:
  `conversation_windows` (+`pending_action`), `conversation_messages`,
  `products.price`, and the `'removed'` status.
- **Phase H — Seller web auth + dashboard (H1 DONE; H2 pending).** `auth.py`
  (PBKDF2 password hashing + revocable server-side sessions with CSRF) and new
  `serve.py` routes: `/seller/signup`, `/seller/login`, `/seller/logout`,
  `/seller/dashboard` (view listings + edit price + soft-remove, via
  `seller_actions`). Cookie is HttpOnly + SameSite=Strict; ownership enforced by
  session, not form input. A "Seller? Log in…" link is in the public header.
  Demo sellers seeded with logins (asha/meera/ravi, pw `demo1234`). Plain HTTP on
  LAN (no TLS), by decision — a dev posture, not production. **H2 DONE:** web
  product add (stdlib multipart upload), replace-photo, edit-price, remove — all
  via `seller_actions` (adds `update_image`; content edits → pending). Uploaded
  images in `media/products/`, served via authed `/seller/product/<id>/image`.
  Every photo control offers **two sources — "From device" and "📷 Camera"**
  (mobile capture via the HTML `capture` attribute); both feed one `image` field,
  so the backend is unchanged.
  **Signup captures `owner_name`** (buyer recognition; profile shows "Run by …")
  and relabels the contact field. **Departments at signup are governed:** the 5
  launch departments are the only pickable ones; a seller may instead type an
  **"Others"** free-text line, which a semantic **Groq matcher** maps to a launch
  department at ≥80% confidence (e.g. jeans→apparels, shown to the seller) — else
  it goes to the real **`others`** bucket + a **`pending_departments`** request
  (buyers see "Others" until an admin approves it, which creates a real department
  and re-tags the seller + their products). A seller who picks nothing lands in
  the **`uncategorized`** bucket. Admin approve/reject via
  `onboarding.approve_department`/`reject_department` (+ CLI). Nothing public is
  silently created — same governed model as `pending_categories`.
- **Phase I — Seller Telegram control bot (DONE, LIVE).** A shared PeerShop bot
  (@peershop_bot, own token) lets a LINKED seller manage their store by chatting
  NL or slash commands; robust per-seller auth (web-login-gated link codes →
  `seller_telegram` binding → identity from Telegram's `from.id`, never message
  text). `telegram_bot.py` (poller) + `telegram_link.py` (identity) + `run_bot.sh`.
- **Phase J — Telegram `/post` product-upload wizard (DONE).** A deterministic,
  **no-LLM** guided flow: `/post` → name → (department if the seller sells in >1)
  → price → **up to 4 photos** → description → posts `status='pending'`.
  `post_wizard.py` (state machine + `post_drafts`), photo download in
  `telegram_bot.py`, `product_photos` storage, and a **gallery** on the product
  detail page in `serve.py` (`/image/<id>/<n>`; falls back to the single
  `image_path` for old products). `/cancel` aborts. **The web add-product form
  also accepts up to 4 photos** (4 device+camera pickers → repeated multipart
  `image` parts). `seller_actions.post_product` gained a `photos=` param and is
  now the ONE place `product_photos` rows are written (Telegram + web both route
  through it); `update_image` (replace-photo) resets the gallery to the one new
  image so it stays consistent.
- **Phase K — Approval flow (DONE).** The pending→live gate, unified so the human
  admin and Hermes use ONE decision path. `approvals.py` (approve/reject/queue +
  guards + governed category-set + best-effort seller Telegram notice) with a CLI
  (`--list/--approve/--reject`) — **Hermes approves listings via this CLI/import
  when no human admin is present.** `review.py` is the human web queue (port 8001,
  `run_review.sh`), gated by shared **`PEERSHOP_ADMIN_PASSWORD`**, cards show
  gallery/department/price/category, all actions route through `approvals.py`.
  `telegram_api.py` splits the Telegram HTTP out of `telegram_bot.py` so the
  stdlib core can notify sellers. Promotion stays human/agent-gated — never
  auto-publishes; uncategorized products can't go live. **Admin visibility:** the
  review app also has a **Users page** (all seller data + password-change audit via
  `credential_events` + admin password reset). Passwords are one-way hashed and
  **never displayed** — only change history (when/by whom) and a reset action.

## Conventions

- Language: Python 3. DB: SQLite in this directory.
- Core stays stdlib-only; heavy deps confined to `.venv/` (intake/describe).
- Keep AI prompts small and bounded; free-tier providers for narrow text calls.
- **Status vocabulary:** `pending` / `live` / `rejected` / `removed`. Products
  always land `pending`; only a human admin promotes to `live`. `removed` is a
  seller-initiated SOFT delete (never a hard DELETE). Content edits revert a
  listing to `pending`; a price-only edit does not.
- **One write path:** all seller-behalf product writes go through
  `seller_actions.py` (post/remove/update_price). The Telegram bot and intake
  both call it; nothing else writes to `products` on a seller's behalf.
- **Governance first:** never auto-create taxonomy (categories/subcategories)
  from seller/AI input — queue for approval. **Attribute values are exempt:**
  freeform text, sanitization only, never queued (decision 2).
- **Account-closure deletion:** closing a seller account deletes all their
  `seller_attribute_history` and `chat_logs` rows entirely (decisions 3 & 4;
  enforced by `ON DELETE CASCADE`).
- When in doubt about scope, prefer the lean option and ask before adding
  infrastructure or inventing taxonomy.
