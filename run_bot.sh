#!/usr/bin/env bash
# PeerShop Telegram bot supervisor (Phase I).
#
# Keeps telegram_bot.py (the seller-control long-poll connector) running: if it
# exits it is restarted, and each start/exit is timestamped in peershop_bot.log.
# A SEPARATE supervised process from run_server.sh — they share only peershop.db.
# Launch DETACHED so it survives the shell/session:
#
#   setsid nohup bash run_bot.sh >/dev/null 2>&1 &
#
# Stop with:  bash run_bot.sh stop
#
# Requires PEERSHOP_TELEGRAM_TOKEN (a dedicated BotFather bot, NOT Hermes's) in
# the environment or /root/.hermes/.env; without it telegram_bot.py exits and the
# supervisor will log the error and retry.

cd "$(dirname "$0")" || exit 1
LOG="peershop_bot.log"
PIDFILE="peershop_bot.pid"

if [ "$1" = "stop" ]; then
    # Launched via `setsid`, so the supervisor is its own process-group LEADER
    # (PGID == its PID). Killing the whole group takes down the supervisor AND its
    # python child in one shot — no `pkill -f` pattern (which could match the
    # invoking shell if its argv happened to contain the pattern).
    if [ -f "$PIDFILE" ]; then
        SPID="$(cat "$PIDFILE")"
        kill -- -"$SPID" 2>/dev/null || kill "$SPID" 2>/dev/null
    fi
    rm -f "$PIDFILE"
    echo "[$(date)] bot supervisor stopped" >> "$LOG"
    exit 0
fi

echo $$ > "$PIDFILE"
echo "[$(date)] bot supervisor start (pid $$)" >> "$LOG"

fails=0
while true; do
    python3 -u telegram_bot.py >> "$LOG" 2>&1
    code=$?
    fails=$((fails + 1))
    echo "[$(date)] telegram_bot.py exited (code $code) — restart #$fails" >> "$LOG"
    sleep 2
done
