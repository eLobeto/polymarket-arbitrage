#!/bin/bash
# stop.sh — Stop the kalshi-pm-arb bot

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PIDFILE="$PROJECT_DIR/logs/scanner.pid"

if [ ! -f "$PIDFILE" ]; then
    echo "Bot is not running (no PID file)"
    exit 0
fi

PID=$(cat "$PIDFILE")
if kill -0 "$PID" 2>/dev/null; then
    echo "Stopping bot (PID $PID)..."
    kill "$PID"
    sleep 2
    if kill -0 "$PID" 2>/dev/null; then
        echo "Force killing..."
        kill -9 "$PID"
    fi
    rm -f "$PIDFILE"
    echo "Bot stopped"
else
    echo "Bot is not running (stale PID $PID)"
    rm -f "$PIDFILE"
fi
