# Gabagool Binary Arbitrage Bot

Automated arbitrage scanner for **Bitcoin 15-minute markets on Polymarket**.

## Core Strategy

**Objective:** Lock in guaranteed profit by buying YES and NO shares asymmetrically when the market misprices one side.

**Math:**
```
If: avg_YES + avg_NO < $1.00
Then: Payout = min(qty_YES, qty_NO) = $1.00
Profit = Payout - Cost = $1.00 - (avg_YES + avg_NO)
```

**Example:**
```
Buy 100 YES @ $0.517 = $51.70
Buy 100 NO @ $0.449 = $44.90
Total cost: $96.60 (combined avg: $0.966)
Result: Guaranteed $1.00/pair = $100 payout
Profit: $3.40 per 100 shares
```

---

## Setup (Step-by-Step)

### 1. **Install Dependencies**

```bash
cd /home/node/.openclaw/workspace/polymarket-arbitrage
pip install -r requirements.txt
```

### 2. **Create Wallet & Fund USDC**

**Option A: New Wallet (MetaMask)**
```bash
# Generate a new Ethereum address and private key
# Store securely — you'll need it for trading
```

**Option B: Use Existing Wallet**
```bash
# Export private key from MetaMask:
# Settings → Account Details → Private Key (save securely)
```

**Fund with USDC on Polygon:**
```
1. Log into Coinbase
2. Buy USDC (on mainnet Ethereum)
3. Withdraw directly to Polygon network (supported by Coinbase)
4. Amount: $500–$2k (start small for testing)
```

### 3. **Configure Environment**

```bash
cp .env.example .env
# Edit .env with your private key and wallet address
```

**Critical:** Never commit `.env` or raw private keys to git.

### 4. **Configure Bot Parameters**

Edit `config/config.yaml`:

```yaml
wallet:
  private_key: "${WALLET_PRIVATE_KEY}"  # From .env
  address: "${WALLET_ADDRESS}"

trading:
  bankroll_usdc: 500              # Start with $500
  target_combined_cost: 0.99      # Buy when YES + NO < $0.99
  min_profit_margin: 0.005        # Require >$0.005 guaranteed profit

dev:
  dry_run: true                   # Test mode first!
```

### 5. **Test in Dry-Run Mode**

```bash
# Run scanner without placing real orders
./scripts/start.sh
tail -f logs/scanner.log

# You should see:
# 🎯 OPPORTUNITY FOUND: Bitcoin: 4:00-4:15 PM ET
#    YES: $0.52 | NO: $0.45
#    Pair Cost: $0.97 | Profit: $0.03
# 🏁 DRY RUN MODE - Not executing
```

Let it run for 10–15 minutes to see how many opportunities appear.

### 6. **Provide Coinbase API Key** (Optional, for automation)

Evan will provide the Coinbase API key for automated USDC bridging (future enhancement).

### 7. **Deploy Live Trading**

Once satisfied with dry-run results:

```bash
# Edit config/config.yaml
dev:
  dry_run: false    # ← Enable live trading

./scripts/stop.sh
./scripts/start.sh
```

---

## Project Structure

```
polymarket-arbitrage/
├── config/
│   └── config.yaml              # Configuration (edit this)
├── src/
│   ├── main.py                  # Scanner loop (entry point)
│   ├── market_fetcher.py        # Fetch markets from Polymarket API
│   ├── position_tracker.py      # Track positions & P&L (SQLite)
│   ├── order_executor.py        # Web3 contract calls (place orders)
│   └── config.py                # Load config + env vars
├── scripts/
│   ├── start.sh                 # Start scanner in background
│   └── stop.sh                  # Stop scanner
├── logs/
│   └── scanner.log              # Log file (auto-created)
├── data/
│   └── polymarket_trades.db     # SQLite database (auto-created)
├── requirements.txt             # Python dependencies
├── .env.example                 # Template for environment variables
├── .gitignore                   # Git ignore rules
└── README.md                    # This file
```

---

## Key Files to Understand

### `src/main.py` — Scanner Loop
- Fetches Bitcoin 15min markets every 5 seconds
- Analyzes each market for arbitrage opportunity
- If `pair_cost < target`, execute trade

### `src/market_fetcher.py` — Market Data
- Connects to Polymarket CLOB API
- Fetches live YES/NO prices
- Filters for Bitcoin 15min markets

### `src/position_tracker.py` — Position Management
- SQLite database of trades
- Tracks qty_YES, qty_NO, costs
- Calculates guaranteed profit
- **Reuses logic from ticker-watch's `paper_trade_manager.py`**

### `src/order_executor.py` — Web3 Execution
- Connects to Polygon RPC
- Places orders via smart contract calls
- Manages USDC approvals & gas

---

## Configuration Reference

### Trading Parameters

| Param | Default | Purpose |
|-------|---------|---------|
| `target_combined_cost` | 0.99 | Only buy if YES + NO < this |
| `min_profit_margin` | 0.005 | Min guaranteed profit ($) |
| `bankroll_usdc` | 500 | Total capital available |
| `max_per_trade_pct` | 0.15 | Max 15% of bankroll per trade |
| `poll_interval_sec` | 5 | How often to check markets |

### Gas & Fees

| Item | Cost | Notes |
|------|------|-------|
| Approve USDC | ~$0.01 | First time only |
| Place order | ~$0.01 | Per trade |
| Settle/Claim | ~$0.05 | On resolution |
| Polymarket fee | 2% | Of winnings only |

**Example Trade Profit:**
```
Cost (YES + NO): $0.96
Payout: $1.00
Gross profit: $0.04
Polymarket fee (2%): -$0.0008
Gas costs: -$0.03
Net profit: +$0.0092 per $1 payout (0.92% ROI)
```

---

## Monitoring & Debugging

### View Live Logs

```bash
tail -f logs/scanner.log
```

### Check Position Database

```bash
sqlite3 data/polymarket_trades.db

# Query all open positions
SELECT id, market_title, qty_yes, qty_no, 
       (qty_yes * 0 + cost_yes) / qty_yes as avg_yes,
       (qty_no * 0 + cost_no) / qty_no as avg_no,
       (qty_yes * 0 + cost_yes) / qty_yes + (qty_no * 0 + cost_no) / qty_no as pair_cost
FROM positions WHERE status = 'open';
```

### Common Issues

**"asset not found" errors**
- Smart contract addresses may be outdated
- Verify Polymarket contract addresses via https://clob.polymarket.com/health

**Low liquidity**
- Increase `min_liquidity_usdc` in config if markets are too thin
- Or wait for higher-traffic 15min windows

**High gas costs**
- Reduce `poll_interval_sec` to scan less frequently
- Or batch orders together

---

## Reused Code from ticker-watch

This project borrows architecture from ticker-watch:

| File | Origin | Purpose |
|------|--------|---------|
| `position_tracker.py` | `paper_trade_manager.py` | Position tracking, SQLite schema |
| `src/config.py` | `config.py` | YAML loading, env var substitution |
| Logging structure | — | Same format + Telegram alerts (future) |

**Why separate?** Polymarket and equities options are fundamentally different markets. Keeping separate avoids coupling. Code can be shared via imports if needed later.

---

## Next Steps

1. ✅ **Now:** Scaffold + dry-run testing (you are here)
2. **Tomorrow:** Provide Coinbase API key → integrate funding automation
3. **Week 1:** Run with $100–$500 real USDC
4. **Week 2:** Scale to $1k–$5k if profitable
5. **Future:** Add Ethereum 15min markets, Deribit options arbitrage

---

## Support & Questions

For issues:
1. Check logs: `tail -f logs/scanner.log`
2. Enable debug logging: `logging.level: DEBUG` in config.yaml
3. Verify RPC connection: `python3 -c "from web3 import Web3; print(Web3(Web3.HTTPProvider('https://polygon-rpc.com')).is_connected())"`

---

**Good luck, Evan! 💙**
