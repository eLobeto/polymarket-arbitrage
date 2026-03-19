# kalshi-pm-arb

Direction-neutral arbitrage bot exploiting pricing discrepancies between **Kalshi** and **Polymarket** on identical intraday crypto candle markets.

Ported from `cross-candle-arb` into a clean production project with:
- Config loaded from `config/.env` (no hardcoded credentials)
- Kalshi PEM loaded from file path (not inline string)
- Double-fork daemon pattern
- Deployed on Frankfurt EC2 (3.73.101.112)

## Strategy

Both venues offer binary markets on the same question: *"Will BTC/ETH/SOL/XRP close higher or lower than its open in the next 15 minutes?"* When the combined cost of both sides < $1.00, a risk-free profit is locked in regardless of outcome.

```
PM UP @ 55¢ + Kalshi NO @ 38¢ = 93¢ combined → lock 7¢/share profit
```

## Project Structure

```
kalshi-pm-arb/
  src/
    main.py           # Main loop + --daemon flag
    config.py         # Settings + credentials from config/.env
    kalshi_auth.py    # RSA-PSS signing (key from file path)
    kalshi_markets.py # Discover active Kalshi 15m markets
    pm_markets.py     # Discover active PM 5m/15m markets
    matcher.py        # Find arb windows
    executor.py       # Execute both legs
    balance_monitor.py # Combined drawdown watchdog
    redeemer.py       # Auto-redeem winning PM positions
    notifier.py       # Telegram alerts
    price_feed.py     # WS price feeds (Kalshi + PM)
    daemon.py         # Double-fork daemonization
  config/
    .env              # Credentials (DO NOT COMMIT)
    kalshi_private.pem # RSA private key
  logs/               # scanner.log, scanner.pid
  scripts/
    start.sh          # Start as background daemon
    stop.sh           # Stop daemon
  requirements.txt
```

## Deployment

```bash
# SSH to EC2
ssh -i polymarket-arb.pem ubuntu@3.73.101.112

# Install deps
cd ~/kalshi-pm-arb
pip3 install -r requirements.txt --quiet

# Start (paper mode by default — LIVE_TRADING=false in config/.env)
bash scripts/start.sh

# Monitor
tail -f logs/scanner.log

# Stop
bash scripts/stop.sh
```

## Configuration

Edit `config/.env` to change settings:

```env
LIVE_TRADING=false     # Set to true for live trading
```

Key thresholds (in `src/config.py`):
- `LIVE_STAKE_USD = 40.0` — $40/leg per trade
- `MIN_ARB_CENTS = 10.0` — min 10¢ profit to trigger entry
- `MAX_PAIR_COST = 97` — combined cost < 97¢

## Auth

- **Polymarket:** EOA signature, `chain_id=137` (Polygon). API creds in `.env`.
- **Kalshi:** RSA private key at `KALSHI_PRIVATE_KEY_PATH`.
- **Redemption:** Calls `CTF.redeemPositions()` on Polygon via web3.
