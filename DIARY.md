# PeerShop Dev — Project Diary

**Last updated:** 2026-08-08
**Repository:** /root/git/peershop.git (shared with peershop prod)
**Branch:** main · 4 commits (identical to prod)
**This is the development environment** — not live, LAN-only

---

## What this project is

The **development environment** for PeerShop. Identical codebase as /root/projects/peershop (shared git remote). Isolated by:
- Directory: /root/projects/peershop-dev
- Ports: web :8100, admin :8101
- Database: sanitized copy of prod DB (masked phone numbers, reset passwords)
- No Telegram bot (no second token)
- No public URL

All feature work happens here. Changes are committed + pushed to the shared git bare repo, then promoted to prod via `deploy.sh`.

---

## Spec documents

Same as peershop prod — see /root/projects/peershop/CLAUDE.md and /root/projects/peershop/DEVELOPMENT.md. No separate dev-specific spec.

---

## Services (dev — systemd on CT 101)

| Service | Port | Description |
|---|---|---|
| peershop-dev-web | 0.0.0.0:8100 | Customer site (LAN only) |
| peershop-dev-admin | 0.0.0.0:8101 | Admin review panel (LAN only) |

Access at: http://192.168.0.155:8100 (site) / :8101 (admin)

---

## How it works

Identical pipeline to peershop prod — see /root/projects/peershop/DIARY.md for the full flow. In brief:

```
Seller submits (web form, no Telegram bot in dev)
    → intake.py → AI drafts → seller confirms → admin approves (review.py :8101)
    → live at http://192.168.0.155:8100
```

All changes tested here first, then promoted to prod via deploy.sh.

## Workflow

```
Edit in /root/projects/peershop-dev
    │
    ▼
Test at http://192.168.0.155:8100
    │
    ▼
git add -A && git commit -m "..." && git push
    │
    ▼
bash /root/projects/peershop/deploy.sh   ← promotes to prod
```

Restart dev after edits:
```bash
systemctl restart peershop-dev-web peershop-dev-admin
```

Refresh dev data from prod:
```bash
systemctl stop peershop-dev-web peershop-dev-admin
cp /root/projects/peershop/peershop.db /root/projects/peershop-dev/peershop.db
cd /root/projects/peershop-dev && python3 sanitize_dev_db.py peershop.db
systemctl start peershop-dev-web peershop-dev-admin
```

---

## Completion status

Same as peershop prod — Phase A–F not started. This env exists to develop those phases safely before promoting to prod.

---

## End goal met?

Same answer as peershop prod. This environment exists to build the Phase A–F restructure without risking live data.

---

## Git status

```
Branch:   main
Remote:   /root/git/peershop.git (shared bare repo)
Commits:  4 (identical to prod)
Dirty:    clean
```

---

## Operational notes

- Never run sanitize_dev_db.py against prod database — it has a hard guard
- Dev AI calls use the same API keys as prod — costs real money
- No Telegram bot in dev — test Telegram flows manually or mock them
- Schema changes: test the ALTER TABLE migration here before applying to prod
