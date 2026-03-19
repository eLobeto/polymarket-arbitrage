#!/bin/bash
# start.sh — Start the kalshi-pm-arb bot as a background daemon

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

PIDFILE="$PROJECT_DIR/logs/scanner.pid"
LOGFILE="$PROJECT_DIR/logs/scanner.log"

# Check if already running
if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Bot is already running (PID $PID)"
        exit 0
    else
        echo "Stale PID file found — removing"
        rm -f "$PIDFILE"
    fi
fi

mkdir -p "$PROJECT_DIR/logs"

echo "Starting kalshi-pm-arb bot..."
"$PROJECT_DIR/.venv/bin/python3" src/main.py --daemon

# Give daemon a moment to write PID
sleep 1

if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    echo "Bot started (PID $PID) — logging to $LOGFILE"
else
    echo "Warning: PID file not found — check $LOGFILE for errors"
fi
