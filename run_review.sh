#!/usr/bin/env bash
# PeerShop review-queue supervisor.
#
# Keeps review.py (the admin approval queue, port 8001) running: if it exits it is
# restarted, and each start/exit is timestamped in peershop_review.log. A separate
# supervised process alongside run_server.sh / run_bot.sh — they share only
# peershop.db. Launch DETACHED so it survives the shell/session:
#
#   setsid nohup bash run_review.sh >/dev/null 2>&1 &
#
# Stop with:  bash run_review.sh stop
#
# Requires PEERSHOP_ADMIN_PASSWORD (env or /root/.hermes/.env) for anyone to log in.

cd "$(dirname "$0")" || exit 1
LOG="peershop_review.log"
PIDFILE="peershop_review.pid"

if [ "$1" = "stop" ]; then
    # setsid → the supervisor is its own process-group LEADER (PGID == its PID);
    # killing the group takes down supervisor + child with no `pkill -f` pattern
    # (which could match the invoking shell).
    if [ -f "$PIDFILE" ]; then
        SPID="$(cat "$PIDFILE")"
        kill -- -"$SPID" 2>/dev/null || kill "$SPID" 2>/dev/null
    fi
    rm -f "$PIDFILE"
    echo "[$(date)] review supervisor stopped" >> "$LOG"
    exit 0
fi

echo $$ > "$PIDFILE"
echo "[$(date)] review supervisor start (pid $$)" >> "$LOG"

fails=0
while true; do
    python3 -u review.py >> "$LOG" 2>&1
    code=$?
    fails=$((fails + 1))
    echo "[$(date)] review.py exited (code $code) — restart #$fails" >> "$LOG"
    sleep 2
done
