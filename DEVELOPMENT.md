# PeerShop — production / development environments

Two environments on this one host, isolated by **directory + ports + database**.
Code history is shared through a bare git repo; data and media are per-environment.

## Layout

| | Production | Development |
|---|---|---|
| Directory | `/root/projects/peershop` | `/root/projects/peershop-dev` |
| Web (serve.py) | `127.0.0.1:8000` (loopback, behind Cloudflare tunnel) | `0.0.0.0:8100` (LAN only) |
| Admin (review.py) | `0.0.0.0:8001` (LAN) | `0.0.0.0:8101` (LAN) |
| Database | `peershop.db` (real data) | `peershop.db` (sanitized copy) |
| Telegram bot | `@peershop_bot` (peershop-bot) | off (no second token) |
| Public URL | https://shop.peershop.in | none — LAN/SSH only |
| systemd units | `peershop-web` · `peershop-admin` · `peershop-bot` | `peershop-dev-web` · `peershop-dev-admin` |

Shared git origin (bare): `/root/git/peershop.git`. Both dirs are clones of it.
Secrets/config: `/root/.hermes/.env` (shared; not in git).

## Develop → promote workflow

1. **Edit + test in dev** (`/root/projects/peershop-dev`). Reach it at
   `http://192.168.0.155:8100` (site) / `:8101` (admin). Restart after edits:
   `systemctl restart peershop-dev-web peershop-dev-admin`.
2. **Commit + push** from dev: `git add -A && git commit -m "…" && git push`.
3. **Promote to prod**: `bash /root/projects/peershop/deploy.sh` — fast-forwards
   prod to `origin/main`, backs up the prod DB, restarts prod services, health-checks.

## Refresh dev data from prod (when you want realistic data again)

```
systemctl stop peershop-dev-web peershop-dev-admin
cp /root/projects/peershop/peershop.db /root/projects/peershop-dev/peershop.db
cp -r /root/projects/peershop/media/. /root/projects/peershop-dev/media/
cd /root/projects/peershop-dev && python3 sanitize_dev_db.py peershop.db
systemctl start peershop-dev-web peershop-dev-admin
```

`sanitize_dev_db.py` masks phone numbers, resets **every seller login to
`devpass1`**, wipes telegram/session/audit/chat rows, and repoints media paths to
the dev dir. It refuses to run against the prod database.

## Schema changes

`deploy.sh` does NOT auto-migrate. If a release changes `schema.sql`, apply the
matching `ALTER TABLE` to the prod `peershop.db` by hand (after its automatic
backup) before/at deploy — `init_db.py` only creates missing tables, never alters
existing ones.

## Notes

- Dev is deliberately **not** internet-exposed (no cloudflared ingress). If it
  ever needs a public preview, add `dev.peershop.in` behind Cloudflare Access.
- Dev shares the prod admin password and LLM API keys (from `.env`) — dev AI
  calls cost real money, so avoid bulk AI runs in dev.
