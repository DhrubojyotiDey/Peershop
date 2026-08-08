# PeerShop — Project Diary

**Last updated:** 2026-08-08
**Repository:** /root/git/peershop.git (local bare repo)
**Branch:** main · 4 commits
**Live URL:** https://shop.peershop.in (via Cloudflare tunnel)
**Dev environment:** /root/projects/peershop-dev

---

## What this project is

A **hyperlocal multi-vendor marketplace** for neighbor sellers. Sellers list products across 9 departments (electronics, home & kitchen, apparels, wellness, grocery, toys & baby, automotive & industrial, books & media, bags & luggage). AI drafts listings and suggests taxonomy; humans confirm and approve. Every product has its own scoped per-product chat assistant.

Started as a fashion-only catalog. Expanded to the multi-department vision. The current codebase still partially reflects the old flat/fashion model — the Phase A–F plan restructures it.

---

## How it works

```
Seller submits product
  via Telegram bot (@peershop_bot)
    or web form
         │
         ▼
   intake.py ──► AI drafts description (Groq/OpenRouter, free tier)
                ──► AI suggests department/category/subcategory
                         │
                    Seller confirms
                         │
                    Admin approves (review.py :8001)
                         │
                    Product goes live
                         │
                  Customer visits shop.peershop.in
                    ──► per-product chat (scoped to that product's description)
```

Stack: Python stdlib only for core (serve.py, review.py, categorize.py, chat.py, llm.py). SQLite database. Heavy deps (rembg, opencv, google-generativeai) in .venv/, used only by intake/describe.

---

## Architecture & spec documents

| Document | Path | Notes |
|---|---|---|
| Project overview + build philosophy | CLAUDE.md | Primary spec — read this first |
| Prod/dev environments | DEVELOPMENT.md | Deployment, port map, promote workflow |
| Schema | schema.sql | SQLite schema |

No separate PRD.md or Architecture.md — CLAUDE.md is the authoritative spec.

---

## File structure

```
peershop/
  serve.py           — customer-facing web server (:8000 prod / :8100 dev)
  review.py          — admin approval interface (:8001 prod / :8101 dev)
  intake.py          — product ingestion pipeline
  telegram_bot.py    — Telegram seller interface (@peershop_bot)
  llm.py             — LLM calls (Groq default, OpenRouter alt)
  categorize.py      — AI taxonomy suggestion
  attributes.py      — per-category attribute handling
  post_wizard.py     — seller product wizard
  schema.sql         — SQLite schema (departments, categories, subcategories, products)
  init_db.py         — create tables (idempotent, never alters)
  deploy.sh          — promote dev → prod (fast-forward + backup + restart)
  sanitize_dev_db.py — mask prod data for dev use
  run_server.sh      — start serve.py
  run_bot.sh         — start telegram bot
  peershop.db        — SQLite database (prod data — NOT in git)
  CLAUDE.md          — primary spec
  DEVELOPMENT.md     — environment docs
```

---

## Services (prod — systemd on CT 101)

| Service | Port | Description |
|---|---|---|
| peershop-web | 127.0.0.1:8000 | Customer site (Cloudflare tunnel → public) |
| peershop-admin | 0.0.0.0:8001 | Admin review panel (LAN only) |
| peershop-bot | — | Telegram seller bot |

Dev environment runs on :8100/:8101 at /root/projects/peershop-dev.

---

## Completion status

### Phase A–F (from CLAUDE.md)

| Phase | Description | Status |
|---|---|---|
| A | Multi-department schema restructure | ⬜ Not started |
| B | Seller onboarding (multi-department support) | ⬜ Not started |
| C | Governed taxonomy (pending-approval queue) | ⬜ Not started |
| D | AI description refinement loop (2–3 rounds, bounded) | ⬜ Not started |
| E | Product listing + customer shop | ⬜ Not started |
| F | Per-product chat (scoped) | ⬜ Not started |

The current codebase is **pre-Phase A** — it has the old flat/fashion model that predates the multi-department vision. It is live and serving real traffic at shop.peershop.in, but the taxonomy and seller flows need restructuring.

---

## End goal met?

**No — the multi-department vision is not implemented.**

What exists:
- Working product pipeline (intake → approve → serve) from the old fashion-catalog era
- Live Telegram bot for seller submission
- Admin review interface
- Basic AI categorization
- Prod/dev environment separation with promote workflow

What's needed for the stated end goal:
- Phase A–F restructure (multi-department schema, governed taxonomy, per-product chat)
- Meesho optional storefront link
- Attributes system for product variants

The site is live and functional for the old model, but the product vision has evolved past what's built.

---

## Pending upgrades

- Phase A: schema restructure for multi-department model
- Phase B–F: full feature build per CLAUDE.md
- Background removal (rembg) for product photos — installed but not integrated into Phase E
- Dev→prod promote workflow is working; use it after each phase

---

## Git status

```
Branch:   main
Remote:   /root/git/peershop.git (local bare repo — not on GitHub)
Commits:  4
Latest:   aa082f5 Add DEVELOPMENT.md: prod/dev environment + promotion workflow
Dirty:    clean

Promote to prod:   bash /root/projects/peershop/deploy.sh
Push:              git push (from /root/projects/peershop-dev)
```

---

## Important operational notes

- `peershop.db` is NOT in git — real seller data, never commit
- `.env` at /root/.hermes/.env — shared between prod and dev, never commit
- Dev shares prod API keys — avoid bulk AI runs in dev (costs real money)
- Schema changes: `deploy.sh` does NOT auto-migrate — apply ALTER TABLE manually to prod DB before deploying
- `sanitize_dev_db.py` must be run after copying prod DB to dev — masks phone numbers, resets passwords to devpass1
