# PeerShop Dev — Project Diary

**Last updated:** 2026-10-11
**Repository:** https://github.com/DhrubojyotiDey/Peershop.git (shared with prod)
**Branch:** main
**This is the development environment** — not live, LAN-only

---

## What this project is

The **development environment** for PeerShop, currently on **CT101**
(`/root/projects/peershop-dev`). Migrated here (back) from a Raspberry Pi 4
(`/opt/peershop`) on 2026-10-10/11 once CT101's maintenance window ended — see
"History" below for the full back-and-forth.

- Ports: web :8100, admin :8101
- Database: `peershop.db` — real seller/product data restored from CT101's old
  prod-mirror backups on 2026-10-11 (11 departments, 30 categories, 11
  sellers, 35 products — NOT the empty/sanitized shell it briefly had on Pi4)
- No Telegram bot running here — `PEERSHOP_TELEGRAM_TOKEN` lives on the real
  production host only (see `docker-compose.yml`/`docker/GCP_MIGRATION.md`);
  running it here too would 409-conflict with prod's poller on the same token

All feature work happens here, gets committed + pushed to GitHub, then
promoted to prod separately.

---

## Spec documents

- `CLAUDE.md` — architecture / module map
- `DEVELOPMENT.md` — prod/dev environment + promotion workflow
- `.hermes/plans/2026-10-10_000000-peershop-self-service-bot-onboarding.md`
  (on the Hermes agent that authored it — not committed to this repo; recover
  via that agent's `.hermes/plans/` if needed) — the self-service bot +
  multimodal ingestion plan, see "Current feature work" below.

---

## Services (dev — systemd on CT101)

| Service | Port | Description |
|---|---|---|
| peershop-dev-web | 0.0.0.0:8100 | Customer site (LAN only) |
| peershop-dev-admin | 0.0.0.0:8101 | Admin review panel (LAN only) |

Access at: http://192.168.0.156:8100 (site) / :8101 (admin) — **note the IP
changed from .155 to .156** at some point (DHCP), update any old bookmarks.

Run via a dedicated venv (`.venv/`, system Python 3.12, NOT Hermes's own
bundled interpreter) rather than Docker: CT101 is an unprivileged Proxmox LXC
container and Docker image builds fail here (`apparmor_parser: Access denied`)
until `lxc.apparmor.profile=unconfined` is set on the Proxmox host + CT101 is
rebooted — deferred, needs Proxmox host access this Hermes instance doesn't
have. The Docker Compose files (`docker-compose.yml` + `.dev.yml`) are still
the source of truth for prod; this venv+systemd setup is a CT101-only
workaround, not a replacement.

---

## How it works

```
Seller submits (web form, or the bot's self-service onboarding + forwarded
bursts — see below)
    → intake.py → AI drafts → seller confirms (or, for bot bursts, the
      debounce worker auto-drafts) → admin approves (review.py :8101)
    → live at http://192.168.0.156:8100
```

## Current feature work: self-service bot onboarding + bulk multimodal ingestion

Goal: any Telegram user, new or existing, messages PeerShop's ONE shared bot;
it creates their shop conversationally if needed, then they forward bursts of
raw photos/text from supplier chats and the bot debounces, AI-drafts, and
lists them under their OWN seller account as `pending` — admin review
unchanged. Extends the existing bot stack (`bot.py`/`telegram_link.py`/
`intake.py`) rather than a parallel system.

| Task | What | Status |
|---|---|---|
| 1 | Self-service shop creation (`telegram_link.create_shop`) | ✅ |
| 2 | Auto-generated web login for bot-created shops | ✅ |
| 3 | Conversational onboarding (name → contact → pincode) | ✅ |
| 4 | Forwarded-burst detection → staged into `bot_raw_ingest` | ✅ |
| 5 | Debounce worker — quiet-window bundling (`debounce_worker.py`) | ✅ (2026-10-11) |
| 6 | Bundle → AI-draft (`intake.ingest_photo_noninteractive`) → pending product | ✅ (2026-10-11) |

Tasks 5-6 added 2026-10-11 on CT101 (ported back from Pi4's Tasks 1-4 work).
30 new tests (`test_debounce_worker.py`, `test_bundle_processing.py`), full
regression suite green (11 files, 177 assertions, all token-free — no real
Gemini/Groq calls in any test).

**Known gaps / not yet done:**
- **No live end-to-end validation** — CT101's dev has no Telegram bot token
  wired up (by design, see above), so Tasks 5-6 are unit/integration-tested
  but never exercised against a real forwarded Telegram message here. Needs
  either a disposable test bot token on a host that runs `telegram_bot.py`,
  or deployment to wherever the real bot runs.
- **`debounce_worker.py` has no supervising process yet on CT101** (no
  systemd unit) — there's nothing for it to pick up without a live bot anyway.
- **Admin push notification** (`PEERSHOP_ADMIN_TELEGRAM_ID`) is new, opt-in,
  and unset by default — needs a real admin Telegram user id configured
  wherever this runs live.
- **Text-only bundles / sellers with no department** are flagged
  (`needs_photo` / `needs_department` on the `bot_raw_ingest` rows) rather
  than silently dropped, but nothing currently surfaces those flags to a human
  — no dashboard/alert reads them yet.
- **`forward_origin` field shape** (Task 4) was confirmed against Telegram's
  docs, not a captured real forwarded-message payload — worth doing before
  this goes live with real traffic.
- **Omnichannel Postgres stack** (`/opt/peershop/omnichannel/` on Pi4,
  separate single-admin curation pipeline) — the plan's (A) keep-separate vs
  (B) retire-and-merge question was NEVER answered by the user. Still fully
  separate, untouched, not decommissioned.
- **Multi-product-per-burst splitting** — v1 is strictly one bundle = one
  product; a burst of 5 different items still becomes one (likely wrong)
  product. Documented fast-follow, not started.

## History

- Originally developed on CT101.
- 2026-10 (exact date lost to compaction): migrated to a Raspberry Pi 4
  (`/opt/peershop`, Dockerized, prod→GCP + dev→Pi4-local) while CT101
  underwent maintenance (disk/A2A/gateway stability work).
- 2026-10-10/11: pulled back to CT101 via A2A collaboration with the Pi4
  Hermes agent once CT101's maintenance finished. Pulled: code (git, matches
  Pi4's HEAD exactly), secrets (`.env`), DB, media, `bot_offset`. Pi4's Docker
  deployment model couldn't be replicated on CT101 (AppArmor/LXC blocker, see
  above) — fell back to venv+systemd instead.
- 2026-10-11: dev DB had its sellers/products restored from CT101's own old
  prod-mirror backup (it arrived back from Pi4 as an empty/sanitized shell —
  0 sellers, 0 products — despite docker-compose.dev.yml's comment claiming
  it used "the real production snapshot").

## Workflow

```
Edit in /root/projects/peershop-dev
    │
    ▼
Test: for f in test_*.py; do .venv/bin/python3 "$f"; done
    │
    ▼
git add -A && git commit -m "..." && git push
```

Restart dev after edits:
```bash
systemctl restart peershop-dev-web peershop-dev-admin
```

---

## Operational notes

- Never run `sanitize_dev_db.py` against prod database — it has a hard guard
- Dev AI calls use the same API keys as prod — costs real money
- No Telegram bot in dev — test Telegram flows via the test suite's
  monkeypatched `telegram_api`, not a real bot
- Schema changes: test the ALTER TABLE migration here before applying to prod
- `intake.py`'s heavy pipeline (rembg/Gemini) needs the project's own
  `.venv` — `./.venv/bin/python3 intake.py ...`, not the bare systemd
  services' interpreter assumptions from before this venv existed
