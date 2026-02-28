#!/bin/bash

# Start Gabagool scanner in background

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${PROJECT_DIR}/logs"
PID_FILE="${LOG_DIR}/scanner.pid"

mkdir -p "${LOG_DIR}"

# Check if already running
if [ -f "${PID_FILE}" ]; then
    OLD_PID=$(cat "${PID_FILE}")
    if kill -0 "${OLD_PID}" 2>/dev/null; then
        echo "Scanner already running (PID: ${OLD_PID})"
        exit 0
    fi
fi

echo "🚀 Starting Gabagool scanner..."
cd "${PROJECT_DIR}"

# Start with correct Python path for user-installed packages
nohup env PYTHONPATH="/home/node/.local/lib/python3.11/site-packages:${PYTHONPATH}" python3 -u src/main.py >> "${LOG_DIR}/scanner.log" 2>&1 &
NEW_PID=$!

echo "${NEW_PID}" > "${PID_FILE}"
echo "✅ Scanner started (PID: ${NEW_PID})"
echo "📋 Logs: tail -f ${LOG_DIR}/scanner.log"
