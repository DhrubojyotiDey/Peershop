# PeerShop

![Python](https://img.shields.io/badge/python-3.12-blue) ![SQLite](https://img.shields.io/badge/database-SQLite-lightgrey) ![License](https://img.shields.io/badge/license-private-red) ![Status](https://img.shields.io/badge/status-live-brightgreen) ![AI](https://img.shields.io/badge/AI-Groq%20%7C%20Gemini-orange)

---

## What is PeerShop

PeerShop is a **hyperlocal, multi-department, multi-vendor marketplace** where neighbor sellers run shops and customers browse from within their own locality. It is live at [shop.peershop.in](https://shop.peershop.in).

Key characteristics:

- **Hyperlocal.** Sellers are local neighborhood shops. Buyers browse what's available near them — not a national catalog.
- **Multi-department.** Sellers are grouped across **9 customer-oriented departments** (Electronics, Home & Kitchen, Apparels, Wellness, Grocery, Toys & Baby, Automotive & Industrial, Books & Media, Bags & Luggage). A single seller can belong to multiple departments (e.g. a general store selling groceries *and* electronics).
- **Multi-vendor.** Each seller has their own profile page with their shop photos, their products grouped by department → category, and subcategory/attribute filters.
- **AI-assisted listings with human approval.** When a seller posts a product photo, AI drafts the title, description, category, and subcategory suggestions. The seller confirms; the admin promotes. Nothing goes live automatically — every listing passes a human approval gate.
- **Per-product scoped chat.** Each live product has its own lightweight customer chat assistant that answers *only* from that product's description. No hallucinated cross-product answers.
- **Governed taxonomy.** New categories/subcategories are never auto-created from seller input — they enter a pending-approval queue for an admin to decide. The closed taxonomy is consistent for all sellers at all times.

---

## Architecture

### Tech stack

| Layer | Technology |
|---|---|
| Core language | Python 3.12 — **stdlib only** for all server/review/chat modules |
| Database | SQLite (`peershop.db`) — single file, no ORM |
| Heavy pipeline deps | `.venv/` — `rembg`, `opencv`, `google-generativeai` (intake/describe only) |
| Web framework | None — raw `http.server` (stdlib) |
| AI / LLM | Groq free-tier (`llama-3.1-8b-instant`) for text; Gemini (`gemini-3.1-flash-lite` / `gemini-3.1-pro-preview`) for image and copy |
| Telegram | Long-poll via raw `urllib` — no third-party Telegram library |

### Service map

| Service | File | Port | Purpose |
|---|---|---|---|
| Public listing | `serve.py` | **:8000** | Buyer-facing catalog — browse, filter, per-product chat |
| Admin review queue | `review.py` | **:8001** | Human admin promotes / rejects pending listings |
| Telegram seller bot | `telegram_bot.py` | — | Long-poll transport; sellers post via Telegram |

Production runs on `127.0.0.1:8000` behind a Cloudflare tunnel (public) and `0.0.0.0:8001` (LAN-only admin). A parallel dev environment runs on `:8100`/`:8101`.

### Key modules

| Module | Role |
|---|---|
| `schema.sql` / `init_db.py` | Full schema + seeded taxonomy; `init_db.py --show` prints the live department tree |
| `serve.py` | Public HTTP server — listings, product images, chat endpoint, seller dashboard |
| `review.py` | Admin review UI — password-gated, HMAC-signed cookie, delegates to `approvals.py` |
| `approvals.py` | Canonical decision path for `pending → live / rejected`; enforces governance invariants |
| `telegram_bot.py` | Long-poll transport; downloads photos, routes messages to `bot.py` |
| `bot.py` | Bot "brain" — slash commands + NL intent parsing (Gemini intent model) |
| `intake.py` | Photo intake bridge — background removal + AI or manual description |
| `post_wizard.py` | Stateful `/post` step-by-step listing wizard (survives bot restarts) |
| `describe.py` | Two-step Gemini describe pipeline: cheap image-id → quality copy |
| `categorize.py` | Two-step AI categorization (category → subcategory) against closed taxonomy |
| `llm.py` | Single stateless LLM call wrapper — Groq or OpenRouter |
| `auth.py` | PBKDF2-HMAC-SHA256 seller passwords + 7-day web sessions |
| `conversation.py` | Daily NL turn-cap bookkeeping per seller (15 turns/day default) |
| `seller_actions.py` | Single authoritative write path for all seller-initiated product mutations |
| `onboarding.py` | Seller onboarding — AI shop-photo department suggestion or manual self-select |
| `chat.py` | Per-product customer chat — scoped to one product's description, stateless |
| `insights.py` | Batch question-clustering (Phase F) — nightly, one bounded Groq call per product |
| `telegram_api.py` | Outbound Telegram notifications (approve/reject/nudge) |
| `telegram_link.py` | One-time link codes for web ↔ Telegram account binding (Phase I) |
| `attributes.py` | Reads/writes freeform product attributes; prefills from seller history |
| `deploy.sh` | Prod promotion: fast-forward + DB backup + service restart + health check |
| `sanitize_dev_db.py` | Masks PII in a prod DB copy for safe dev use |

---

## Departments & Taxonomy

PeerShop uses a **three-level governed hierarchy**: `department → category → subcategory`, plus lightweight per-category attributes.

### The 9 departments (customer-oriented umbrella genres)

| Department | Categories |
|---|---|
| **Electronics** | mobiles, computers, tv, electronics |
| **Home & Kitchen** | home, kitchen, appliances, pets |
| **Apparels** | men's fashion, women's fashion, kids' fashion |
| **Wellness** | beauty, health, sports, fitness |
| **Grocery** | grocery |
| **Toys & Baby** | toys, baby products |
| **Automotive & Industrial** | car, motorbike, industrial |
| **Books & Media** | books, movies, music, video games |
| **Bags & Luggage** | bags, luggage |
| *(system buckets — not selectable by sellers)* | **others**, **uncategorized** |

> **Design intent:** Grouped by where a *customer* looks — e.g. appliances live under Home & Kitchen, not Electronics (a shopper hunts a washing machine under "home"). Umbrellas merge related lines (men's/women's/kids' → Apparels; beauty/health/sports → Wellness); things that don't merge accurately are kept separate.

### Governed taxonomy rules

- **No silent auto-creation.** If a seller's item matches no existing category or subcategory, it lands in `pending_categories` / `pending_subcategories` for manual admin approval — never instantly created or usable.
- **Subcategories** are scoped to exactly one category. None are seeded by default — they grow via the governed pending queue.
- **Attributes** are the *exception* to governance: attribute values are freeform text (sanitization only), never queued. Only categories and subcategories go through the approval queue.
- **Attribute-value prefill:** a `seller_attribute_history` table records each value a seller has used. When a seller lists a new product, the form pre-fills the seller's most-used past values for that category/attribute — zero AI calls, plain SQL lookup.

---

## Seller Flows

### Onboarding

1. Seller signs up via the Telegram bot or web.
2. **Two symmetric paths in (AI is never the only way):**
   - **AI path:** seller uploads a shop/storefront photo; Gemini (`gemini-3.1-pro-preview`) analyzes it and *suggests* department(s). Seller confirms or adjusts.
   - **Manual path:** seller skips AI classification and self-selects department(s) directly — a first-class path, not a fallback.
3. If AI returns no confident result (bad photo, API error), the flow routes automatically to the manual self-select prompt — no dead-ends.
4. Seller profile is created tagged with confirmed department(s) (one or many). At least one department is required.
5. Shop photos are persisted to `seller_photos` table — durable assets for the profile page, not throwaway inputs.

### Per-product listing

1. Seller sends product photo(s) to the bot (or uploads via web dashboard).
2. Background removal always runs (local `rembg` — no API cost).
3. **Two description paths (parallel, switchable at any point):**
   - **AI-draft path:** Gemini two-step describe (cheap image-id → quality copy) → AI suggests category + subcategory → bot sends draft to seller → seller can request changes (bounded refinement: **capped at 2–3 rounds**) → after cap, seller hand-edits or approves as-is.
   - **Manual path:** seller writes their own title + description from the first message — zero AI describe calls, zero refinement.
4. Category-scoped attribute values are pre-filled from the seller's own history (plain SQL, no AI).
5. Seller approves → product enters as **`status='pending'`**, unconditionally.
6. Admin reviews in `review.py` (:8001) and promotes to **`status='live'`** or rejects — **no auto-publish**.

### Seller web dashboard

Sellers can also manage listings via the web (`/seller/dashboard`):
- Add a product (stdlib multipart photo upload — no bg-removal/AI, that's the Telegram path)
- Edit price (stays live), replace photo (reverts to pending), soft-remove
- All actions are ownership- and CSRF-checked

---

## Phases Completed

All phases A through K are complete and running in production.

| Phase | Description | Status |
|---|---|---|
| **A** | Multi-department schema restructure — governed `departments → categories → subcategories` hierarchy, composite FKs, pending-approval queues | ✅ Done |
| **B** | Governed taxonomy enforcement — no auto-create; `pending_categories` / `pending_subcategories` queue; `categorize.py` two-step AI against closed list | ✅ Done |
| **C** | Seller onboarding — AI shop-photo department suggestion (Gemini pro) + manual self-select path; `seller_photos` persistence; `onboarding.py` | ✅ Done |
| **D** | AI description refinement loop — Gemini two-step describe (`describe.py`); bounded 2–3 round refinement cap; manual-description parallel path | ✅ Done |
| **E** | Seller profile page — per-seller public page with shop photos, products grouped by dept → category, subcategory filters, attribute pills | ✅ Done |
| **F** | Listing intelligence — `chat_logs` table; periodic batch question-clustering (`insights.py`); seller nudge (never auto-edits listing) | ✅ Done |
| **G** | Conversational window — daily NL turn budget per seller (default 15); `conversation.py` + `conversation_windows` table; slash commands always available | ✅ Done |
| **H** | Seller web login + dashboard — PBKDF2 auth (`auth.py`), 7-day sessions, `/seller/dashboard` with full product lifecycle (add/edit/remove) | ✅ Done |
| **I** | Telegram account linking — one-time link codes (`telegram_link.py`), web ↔ Telegram binding, `seller_telegram` table | ✅ Done |
| **J** | `/post` wizard — stateful step-by-step Telegram listing wizard (`post_wizard.py`), persisted in `post_drafts` table, survives bot restarts | ✅ Done |
| **K** | Hardening, search, hyperlocal features — admin takedown, per-IP rate limiting on chat, search/filter, PIN-code hyperlocal scoping | ✅ Done |

---

## Running Locally

### Prerequisites

- Python 3.12
- `gh` CLI (for GitHub ops, optional)
- Copy `/root/.hermes/.env` or create a local `.env` file (see Environment Variables below)

### 1. Initialise the database

```bash
python3 init_db.py
# To verify the taxonomy tree:
python3 init_db.py --show
```

### 2. Run the public listing server

```bash
bash run_server.sh
# Starts serve.py on :8000
```

### 3. Run the Telegram seller bot

```bash
bash run_bot.sh
# Starts telegram_bot.py long-poll loop
```

### 4. Run the admin review queue

```bash
bash run_review.sh
# Starts review.py on :8001 (LAN-only recommended)
```

### Promote dev → prod

```bash
bash deploy.sh
# Fast-forwards prod, backs up DB, restarts systemd services, health-checks
```

---

## Environment Variables

All secrets live in `/root/.hermes/.env` (shared between prod and dev) and are **never committed to git**. Create a local `.env` file at the project root (already gitignored) to override for local development.

| Variable | Purpose |
|---|---|
| `GROQ_API_KEY` | Groq free-tier API key — used by `llm.py` for categorization and question-clustering |
| `GEMINI_API_KEY` | Google Gemini API key — used by `describe.py` and `onboarding.py` |
| `PEERSHOP_TELEGRAM_TOKEN` | Telegram bot token for the seller bot (`telegram_bot.py`) |
| `PEERSHOP_ADMIN_PASSWORD` | Password for the admin review interface (`review.py`) |
| `PEERSHOP_HOST` | Bind host for `serve.py` (default: `127.0.0.1` in prod, `0.0.0.0` in dev) |
| `PEERSHOP_PORT` | Port for `serve.py` (default: `8000`) |
| `PEERSHOP_REVIEW_PORT` | Port for `review.py` (default: `8001`) |
| `PEERSHOP_PROVIDER` | LLM provider for `llm.py`: `groq` (default) or `openrouter` |

> ⚠️ **Never commit `.env`, API keys, or database files.** The `.gitignore` in this repo excludes all of them. The real database (`peershop.db`) contains live seller and customer PII and must never leave the server.

**Local `.env` example** (for development — safe to create, gitignored):

```env
GROQ_API_KEY=gsk_...
GEMINI_API_KEY=AI...
PEERSHOP_TELEGRAM_TOKEN=...
PEERSHOP_ADMIN_PASSWORD=devpass1
PEERSHOP_HOST=0.0.0.0
PEERSHOP_PORT=8100
PEERSHOP_REVIEW_PORT=8101
```

---

## Roadmap — Autonomous Admin Agent (Next Phase)

The next major milestone is an **autonomous admin agent** that lives permanently
on the server and acts as a continuous, policy-driven moderator — replacing the
need for a human admin to be present at all times.

### What it does

The agent monitors all inbound activity in real time:

- New product listings submitted by sellers (status='pending')
- Seller account registrations awaiting department assignment
- Pending category / subcategory approval requests
- Post-publish listings flagged by customer reports or policy rules
- Scheduled nightly insight batches (Phase F chat clustering)

For each event the agent evaluates a **fixed, transparent policy ruleset**
(no opaque LLM judgment — rules are code, not prompts) and takes one of:

| Decision | Action |
|---|---|
| Approve | Promotes listing to status='live' via approvals.py |
| Reject | Sets status='rejected', notifies seller via Telegram |
| Takedown | Sets a live listing back to status='rejected' (post-publish enforcement) |
| Escalate | Flags for human review (does NOT auto-decide on borderline cases) |

### Policy parameters the agent enforces

1. **Completeness check** — title present, description ≥ 30 chars, price > 0, at least one photo, category assigned (not uncategorized).
2. **Category alignment** — product's AI-suggested category matches the seller's declared department(s); mismatches escalate rather than auto-approve.
3. **Price sanity** — price within a configurable floor/ceiling per department (e.g. grocery ₹1–₹10,000; electronics ₹100–₹5,00,000); outliers escalate.
4. **Duplicate detection** — same seller, same title within 24 h → reject as likely duplicate upload.
5. **Prohibited content** — keyword blocklist on title + description; any match → immediate reject + human escalation.
6. **Photo presence** — at least one photo required; listings with no image_path and no product_photos rows → reject.
7. **Seller standing** — seller must be active (not closed/suspended); listings from inactive sellers → reject.
8. **Refine-round cap respected** — products that exceeded the AI refinement cap must have a manual description set; those that don't → escalate.

### What the agent never does autonomously

- Auto-create new taxonomy (categories, subcategories, departments) — always queued.
- Edit a seller's listing text or price.
- Approve a listing flagged by the prohibited-content check.
- Approve a listing whose seller account is unverified or in the others/uncategorized bucket without a manual department assignment.

### Trigger model

The agent runs as a lightweight loop (or cron job) on the server:

```
every N minutes:
  → query pending_queue (approvals.py)
  → for each pending listing: evaluate policy ruleset
  → approve / reject / escalate
  → log decision + reason to agent_decisions table (audit trail)
  → notify seller via Telegram (telegram_api.py) on approve/reject
```

No external agent framework. Pure Python, stdlib only. Decision log is a new
`agent_decisions` table `(id, product_id, decision, reason, ts)` — append-only,
never deleted, for human audit.

### Human override always wins

The human admin review interface (review.py :8001) remains fully functional.
An agent decision can be reversed by a human at any time. The agent is an
assistant to the admin, not a replacement.
