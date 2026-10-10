# PeerShop → Google Cloud migration notes

This container setup was built with a GCP lift-and-shift in mind. Two realistic paths:

## Option A: Single GCE VM (closest to current setup, least change)
- `e2-small` or `e2-medium` Compute Engine VM, Debian/Ubuntu, Docker + Docker Compose installed.
- Copy this whole `/opt/peershop` directory (minus `.venv`, `__pycache__` — see `.dockerignore`) to the VM.
- Copy `/etc/peershop/peershop.env` values into a new `.env` on the VM (see `.env.example`).
- `docker compose up -d --build`.
- Point the existing Cloudflare Tunnel at the new VM's internal setup (cloudflared runs
  as a container here too — update `CLOUDFLARE_TUNNEL_TOKEN` if you create a fresh tunnel,
  or keep the same tunnel and just redeploy `cloudflared` pointing at the new box).
- SQLite-on-a-Docker-volume is perfectly fine here — single writer, no concurrency issues,
  identical to how it runs today on the Pi.
- Persistent disk: GCE's default boot disk already persists across reboots (unlike Cloud
  Run), so the named Docker volumes (`peershop_db`, `peershop_media`) survive naturally.
  For extra safety, consider also snapshotting the disk or syncing `peershop.db` + `media/`
  to a GCS bucket on a cron (same pattern as the old Pi↔CT101 sync job).

## Option B: Cloud Run (serverless, more re-architecture)
- **Do NOT lift-and-shift SQLite here.** Cloud Run instances are ephemeral and can scale to
  multiple concurrent instances — a SQLite file is not safe for concurrent writers and
  isn't persisted across instance restarts/cold starts at all.
- Would require migrating `peershop.db` to **Cloud SQL (Postgres)** and `media/` to a
  **GCS bucket**, then updating `approvals.py`, `attributes.py`, and every other module
  with `DB_PATH = os.path.join(HERE, "peershop.db")` to use a Postgres connection string
  instead of `sqlite3.connect`. This is a real code change, not just an infra change.
- Only worth it if you need autoscaling / pay-per-request economics. For a project this
  size, Option A is almost certainly the right call first.

## What's already handled in this Docker setup for either path
- Multi-stage build, non-root container user, `linux/amd64` explicit target (this dev
  box is ARM; GCE/Cloud Run are x86_64 — built for the target, not the source host).
- Three services (web/admin/bot) from one image via `PEERSHOP_ROLE`, matching the
  current systemd service split 1:1 — makes the migration mentally easy to verify
  ("did all 4 things that were running on the Pi come back up on GCP").
- `/health` endpoint wired into `HEALTHCHECK` and compose healthcheck — reuses the
  check the old `deploy.sh` already used (`curl -sf http://localhost:8000/health`).
- `.env` based secrets, never baked into the image — same secrets (Gemini key, Telegram
  token, admin password, Cloudflare tunnel token) just need copying across.

## What to double check before cutover
- [ ] Confirm `rembg`/`onnxruntime` actually download/cache their model weights at
      first use (they're not vendored in `requirements.txt`) — give the VM outbound
      internet access on first boot, or pre-warm the cache into the image.
- [ ] Decide: keep Cloudflare Tunnel (works identically from any host) or switch to a
      GCP HTTPS Load Balancer + Cloud DNS (more "native" GCP, more setup).
- [ ] `bot_offset` (Telegram long-poll offset) — currently a bare file in the repo root,
      not yet wired into the Docker volumes. Add it to the `peershop_data` volume if you
      want zero duplicate-message risk across a bot container restart during migration.
- [ ] Decommission the Pi-side systemd services (`peershop-web/admin/bot/cloudflared`)
      only AFTER the GCP container stack is confirmed healthy — don't run both against
      the same Telegram bot token / Cloudflare tunnel simultaneously (will conflict).
