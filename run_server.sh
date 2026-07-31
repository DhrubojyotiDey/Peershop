#!/usr/bin/env bash
# PeerShop server supervisor.
#
# Keeps serve.py running: if it ever exits (crash, OOM kill, etc.) it is
# restarted automatically, and every start/exit is timestamped in
# peershop_server.log. Launch it DETACHED so it survives the shell/session that
# started it:
#
#   setsid nohup bash run_server.sh >/dev/null 2>&1 &
#
# Stop it with:  ./run_server.sh stop
#
# This is the "robust backend" answer: the app stays stdlib http.server (per the
# project's design), and durability comes from supervision + detachment, not a
# heavier web framework (which would be killed by the same teardown anyway).

cd "$(dirname "$0")" || exit 1
LOG="peershop_server.log"
PIDFILE="peershop_server.pid"
PORT="${PEERSHOP_PORT:-8000}"

if [ "$1" = "stop" ]; then
    [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null
    # also stop any serve.py bound to the port
    for p in $(ss -ltnp 2>/dev/null | grep ":$PORT" | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u); do
        kill "$p" 2>/dev/null
    done
    rm -f "$PIDFILE"
    echo "[$(date)] supervisor stopped" >> "$LOG"
    exit 0
fi

echo $$ > "$PIDFILE"
echo "[$(date)] supervisor start (port $PORT, supervisor pid $$)" >> "$LOG"

# Restart loop with light backoff so a fast-failing start doesn't spin hot.
fails=0
while true; do
    PEERSHOP_PORT="$PORT" python3 serve.py >> "$LOG" 2>&1
    code=$?
    fails=$((fails + 1))
    echo "[$(date)] serve.py exited (code $code) — restart #$fails" >> "$LOG"
    sleep 2
done
