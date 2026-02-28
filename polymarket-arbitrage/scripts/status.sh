#!/bin/bash
# Quick status check for polymarket-arbitrage scanner

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# Check if running
PID=$(pgrep -f "python3 -u src/main.py")

if [ -z "$PID" ]; then
    echo "❌ Scanner not running"
    exit 1
fi

echo "✅ Scanner running (PID: $PID)"
echo ""

# Get recent stats from logs
if [ -f "logs/scanner.log" ]; then
    echo "=== Last 5 Minutes Activity ==="
    tail -20 logs/scanner.log | grep -E "OPPORTUNITY|markets|Cycle"
else
    echo "⚠️  Logs not yet created"
fi

echo ""

# Check database
if [ -f "data/polymarket_trades.db" ]; then
    echo "=== Opportunities Detected ==="
    sqlite3 data/polymarket_trades.db "SELECT COUNT(*) FROM dry_run_opportunities;" 2>/dev/null || echo "0"
else
    echo "⚠️  Database not yet initialized (normal for dry-run with no opportunities)"
fi

echo ""
echo "Mode: $(grep "dry_run:" config/config.yaml | awk '{print $NF}')"
echo "Bankroll: $(grep "bankroll_usdc:" config/config.yaml | awk '{print $NF}')"
