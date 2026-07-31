#!/usr/bin/env bash
# Promote the latest committed code to PRODUCTION.
#
# Workflow: develop + commit in /root/projects/peershop-dev, `git push`, then run
# this on the box:   bash /root/projects/peershop/deploy.sh
#
# It fast-forwards prod to origin/main and restarts the prod systemd services.
# It does NOT run schema migrations — those are manual (see note below), because
# schema changes to a live DB need a human-reviewed ALTER, never an auto-wipe.
set -euo pipefail

PROD=/root/projects/peershop
cd "$PROD"

git fetch --quiet origin
CUR=$(git rev-parse --short HEAD)
NEW=$(git rev-parse --short origin/main)

if [ "$CUR" = "$NEW" ]; then
    echo "Production already up to date ($CUR). Nothing to deploy."
    exit 0
fi

echo "Deploying $CUR -> $NEW"
echo "Incoming changes:"
git log --oneline HEAD..origin/main
echo

# Back up the prod DB before any deploy (cheap insurance).
cp peershop.db "peershop.db.bak.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true

git pull --ff-only origin main

# NOTE: if this release changes schema.sql, apply the matching ALTER TABLE to
# peershop.db by hand (or add a migrations/ step) BEFORE it will work. init_db.py
# only CREATEs missing tables; it does not alter existing ones.

systemctl restart peershop-web peershop-admin peershop-bot
sleep 2
if curl -sf http://localhost:8000/health >/dev/null; then
    echo "prod web healthy"
else
    echo "WARN: prod web health check failed — check: journalctl -u peershop-web -n 50"
fi
echo "Deployed. Production now at $(git rev-parse --short HEAD)."
