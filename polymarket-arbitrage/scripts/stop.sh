#!/bin/bash

# Stop Gabagool scanner

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${PROJECT_DIR}/logs"
PID_FILE="${LOG_DIR}/scanner.pid"

if [ ! -f "${PID_FILE}" ]; then
    echo "⚠️  Scanner not running (no PID file)"
    exit 0
fi

PID=$(cat "${PID_FILE}")

if kill -0 "${PID}" 2>/dev/null; then
    echo "⏹️  Stopping scanner (PID: ${PID})..."
    kill "${PID}"
    rm "${PID_FILE}"
    echo "✅ Scanner stopped"
else
    echo "⚠️  Scanner not running (stale PID: ${PID})"
    rm "${PID_FILE}"
fi
