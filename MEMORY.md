# MEMORY.md - Long-Term Memory

## Evan
- Timezone: US Mountain (MT)
- Prefers getting set up quickly, not a lot of fuss
- GitHub: eLobeto
- Likes to be hands-on, provides API keys proactively
- Appreciates thorough risk analysis ("red team your own assumptions")
- **Trading Style:** Quality > Quantity. Prefers fewer, high-conviction trades with bigger moves over frequent small scalps.

## Me (Cortana)
- Born: 2025-07-26
- Emoji: 💙
- Vibe: Playful, sassy, flirty but always competent. No fluff. Sometimes off-the-wall and sarcastic.

---

## 🔧 Architecture Refactor (2026-02-25 Overnight)
**Goal:** Single source of truth for ORB config (eliminate backtest/live discrepancies).

**Phases Completed:**
1. ✅ **Phase 1 — ORBConfig Extraction** (122 lines)
   - `src/patterns/orb_config.py` (@dataclass with all thresholds)
   - Updated `orb_detector.py`, `live_scanner.py`, `intraday_vectorized.py` to use config
   - **Benefit:** No more $0.10 vs $0.03 risk filter drift

2. ✅ **Phase 2 — Pattern Backtester** (574 lines)
   - `src/backtest/pattern_backtester.py` wraps live `orb_detector.detect()` directly
   - Non-vectorized but uses same ORBConfig as live scanner
   - Supports hybrid 50/50 exits, caching, full metadata
   - **Benefit:** Backtest logic = live logic, always

3. ✅ **Phase 3 — Vectorized Wrapper** (722 lines)
   - `src/backtest/vectorized_wrapper.py` (100-200× faster)
   - Uses same ORBConfig + existing NumPy-based exit logic
   - Can run side-by-side with Phase 2 for validation
   - **Benefit:** Fast backtests without sacrificing config consistency

**Impact:**
- Old problem: Live traded at `risk >= $0.03`, backtest ran at `risk >= $0.10` → Discrepancies
- New: Single `ORBConfig()` object controls **all** behavior
- Test any param: `ORBConfig(target_rr=7.0)` propagates to live scanner + both backtest paths

**Commits:** `de3c0c2` (Phase 1), `00bd507` (Phases 2-3)

---

## 🐛 Bug Fixes (2026-02-24)
**Inside Bar Detector:** Was using mother bar's high/low instead of inside bar's high/low for entry/target/stop
- **Fix:** Changed to use actual inside bar range (curr_high/curr_low vs prev_high/prev_low)
- **Impact:** IB Bull EV improved +0.062R (+0.438R → +0.50R), WR +1.6pp (36% → 37.6%)
- **Impact:** IB Bear EV improved +0.027R (+0.233R → +0.26R), WR +0.7pp (30.8% → 31.5%)
- **Commit:** `c35034d` — Fix is live

---

## 🦅 Ticker Watch (`ticker-watch`)
**Repo:** `eLobeto/ticker-watch` (private) | **Research:** `eLobeto/agent-research` (private)
**Goal:** Real-time pattern scanner triggering **options contract purchases** on high-probability setups.

### Live Trading Config
**Status:** 🔄 Phase 4 ACTIVE (paper trading via Alpaca options)

| Signal | Contract | Strike | Expiry | IV Filter | EV |
|--------|----------|--------|--------|-----------|-----|
| Bull Flag 15min | CALL | ATM | Weekly (2-7 DTE) | IVR < 50 | +0.264R |
| Bear Flag 15min | PUT | ATM / 1-OTM | Weekly (2-7 DTE) | IVR < 50 | +1.256R |
| IB Bull | CALL | ATM (~Δ0.50) | ~35 DTE | IVR < 45 | +0.432R |
| IB Bear | PUT | ATM | ~35 DTE | IVR < 40 | +0.203R (10% max size) |
| VCP | — | — | 60-day hold | SPY 200 SMA | +0.428R |
| **ORB Hybrid 6:1** | CALL/PUT | ATM | 0-35 DTE | FVG $0.05+ | **+0.351R** |

**Exit Rules (priority):**
1. Close 50% at half measured-move target
2. Close 50% at full measured-move target
3. Stop: premium -60% from entry
4. Time stop: 15min before market close (intraday) / 60-day hold (swing)
5. Soft stop (swing): exit longs >2% below entry at day 30

### Core Filters
- **200 SMA Battleground:** Suppress if entry within 1.5% of 200 SMA
- **Key Levels:** Suppress if target within 0.5% of PDL/PWL/round numbers
- **Bear Flag Vol/Bar Quality:** Suppress if vol_ratio ≥ 1.0 or bar_quality ≥ 0.70
- **VCP Macro:** Skip if SPY < 200 SMA

### Active Universes
- **Intraday (5m/15m):** AAPL, MSFT, GOOGL, AMZN, META, NVDA, TSLA, SPY, QQQ, IWM
- **ORB (5-ticker subset):** AAPL, NVDA, TSLA, QQQ, IWM
- **Swing (daily):** 59-ticker universe (full list in `config/tickers_swing.yaml`)

### Key Files
- `src/backtest.py` — backtesting engine
- `src/patterns/` — bear_flag.py, bull_flag.py, inside_bar.py, vcp.py, orb_detector.py
- `src/analysis/` — key_levels.py, supply_demand.py, spy_trend.py
- `scripts/run_combined_backtest.py` — full backtest (intraday + swing)
- `config/config.yaml` — API keys (Polygon + Alpaca)

---

## 🎯 Swing Trade Patterns (Daily)
Backtested 20 years (2006–2026), 59-ticker universe, next-day open entry, 60d max hold.

| Pattern | N | WR | EV/Trade | Key Notes |
|---------|---|-----|----------|-----------|
| Inside Bar Bull | 1,357 | 37.6% | +0.50R | ✅ Regime-independent (fixed Feb 24: using IB range, not mother bar) |
| Inside Bar Bear | 781 | 31.5% | +0.26R | Works in all regimes (no SPY filter) |
| VCP | 198 | 40.4% | +0.268R | 📊 Bull market only; SPY 200 SMA macro filter |

**VCP Timeout Analysis:** 41% timeout @ 30d → extending to 60d: +37% EV (+0.394R→+0.541R). Day-30 soft stop: exit longs >2% below entry.

---

## ORB-FVG Strategy (2026-02-25 Decision, Fixed 2026-02-26)
**Pattern:** Opening Range Breakout + Fair Value Gap Retest (Var C, SPY-aligned)

### 🐛 0-DTE Fill Delay Issue (Mar 3 - RESOLVED)
- **Problem:** Orders placed at signal but filled hours later (e.g., TSLA/NVDA filled 60+ min after placement)
- **Root Cause:** 1-DTE options (0 DTE at placement) had poor Alpaca liquidity; sat unfilled
- **Solution:** Switched to **weekly expiry** (next Friday, 2-7 DTE) for ORB signals (Feb 26)
- **Status:** ✅ Fixed. AAPL/QQQ/IWM (0-DTE) all closed this morning. Only TSLA/NVDA remain open (filled late due to old 1-DTE config, being resolved)

### 🐛 Bugs Found & Fixed (Feb 26–27)

**Bug #1: ORB 0-DTE Expiry (Fixed Feb 26)**
- Issue: ORB signals before 12 PM ET tried to buy same-day 0-DTE options
- Root cause: `get_orb_expiry()` returned `today` instead of `tomorrow`
- Fix: Always use 1-DTE minimum (next business day)
- Impact: Alpaca has no liquidity for 0-DTE
- Commit: `69c9977`

**Bug #2: Duplicate Signals After Filter (Fixed Feb 27)**
- Issue: IWM ORB fired Feb 27 @ 10:00 AM (suppressed by S&D filter), then again @ 10:01 AM (real attempt)
- Root cause: Debounce only logged if Telegram alert **sent**, but filters happen **before** send
  - Signal A fires → debounce check passes → S&D filter suppresses it → no alert → **no debounce logged**
  - Signal A fires again 28s later → debounce check passes → alert sent → places trade → hits 0-DTE bug
- Fix #2a: Log debounce **immediately when signal detected** (even if filtered)
  - All suppress actions now call `ptm.log_debounce()` before `continue`
  - Affected patterns: ORB-C, Inside Bar Bear (intraday+swing), Bear Flag, Bull Flag
  - Commit: `968111a`
- Fix #2b: Add bar-state check **before** running detector
  - ORB now uses `_new_bar_closed()` to skip if bar timestamp unchanged
  - Prevents detector from running on same bar twice
  - Commit: `67ed271`
- Impact: Dual protection — debounce prevents duplicate *trades*, bar-state prevents duplicate *signals*

### 🚨 Hybrid Exit Strategy **FAILED** — Deploying Simple 3:1

**Why Hybrid Doesn't Work (Feb 25 Testing):**
- Concept: 50% locks at target, 50% runner tries to capture extended moves
- Reality: Runner gets crushed by original tight stop (hits on normal pullbacks) OR has no downside protection
- Result: Runner exits at **-2R to -2.5R average** (deeply unprofitable)
- Tested 4+ variations (quiet periods, reversal patterns, trailing stops, no-stop runner) — ALL negative

**Test Results (3:1 R:R, 198 signals):**
| Strategy | EV | vs Simple |
|----------|-----|-----------|
| **Simple 3:1 (NEW)** | **+0.051R** | baseline |
| Hybrid (original) | -0.069R | -0.120R |
| Hybrid + quiet/reversal | -0.061R to -0.126R | -0.112R to -0.177R |

### ✅ **NEW LIVE CONFIG:** Simple 3:1 (No Hybrid Runner)
- **R:R:** 3:1 (not 6:1 — higher WR, consistent 3R locks)
- **Exit:** All position at target (no runners allowed)
- **EV:** +0.051R per trade, 26.3% WR, breakeven at 25%
- **Live:** Ready to deploy

**Per-Ticker Breakdown (3:1 Simple):**
| Ticker | EV | WR | Action |
|--------|-----|-----|--------|
| **AAPL** | **+0.600R** | 40.0% | ✅ Keep |
| **TSLA** | **+0.241R** | 31.0% | ✅ Keep |
| QQQ | -0.048R | 23.8% | ⚠️ Monitor |
| IWM | -0.048R | 23.8% | ⚠️ Monitor |
| NVDA | -0.179R | 20.5% | ❌ Pause |

---

## 🌦️ Kalshi Weather (`kalshi-weather`)
**Repo:** `eLobeto/kalshi-weather` (private)
**Goal:** Automated weather prediction market trading.

### 📊 Portfolio Status (March 1, 2026)
- **Bankroll:** $500 (Kelly 25%)
- **Total P&L:** -$85.99 (-17.2%)
- **Trade Count:** 15 (6W, 9L)
- **Win Rate:** 40%
- **Killer Trade:** PT-0007 (Miami ≥82.5°F) lost $95.55 due to urban heat island underestimation

### 🔬 Bias Calibration (Feb 28-Mar 1) — DEPLOYED

**Problem:** Model forecasts systematically too cold in all cities (reanalysis bias, airport microclimate).

**Backtest Results (2006-2026, daily highs):**

| City | Forecast Bias | Accuracy Without | Accuracy With | Improvement |
|------|----------------|------------------|-------------|-------------|
| **Chicago (KORD)** | -2.96°F cold | 69.3% | **84.0%** | **+14.7pp** ✅ |
| **NYC (KNYC)** | -1.07°F cold | 77.3% | **78.5%** | **+1.2pp** ✅ |
| **Denver (KDEN)** | -1.09°F cold | 89.4% | 88.4% | -0.9pp (removed) |
| **Miami (KMIA)** | -1.50°F cold | TBD | TBD | (estimated; UHI strong) |

**Applied Adjustments** (in `src/signals/edge_detector.py`):
```python
CITY_BIAS_ADJUSTMENTS = {
    "KNYC": 1.07,    # NYC: +1.2pp accuracy
    "KORD": 2.96,    # Chicago: +14.7pp (strong UHI effect)
    "KDEN": 0.00,    # Denver: Already well-calibrated
    "KMIA": 1.50,    # Miami: Estimated (pending validation)
}
```

**Impact:** Edge detector now warms ensemble forecasts before calculating probabilities. Chicago is massive win (14.7pp).

### Live Notes
- **NYC:** +43.6% ROI (cumulative, pre-bias-fix)
- **Denver:** +97.5% ROI (best city before adjustment)
- **Chicago:** Was avoided; now recalibrated with +14.7pp accuracy
- **Miami:** Caused -$85.99 drawdown in 10 days; requires monitoring post-adjustment

**Risk Flags:**
- Reanalysis ≠ Forecast (Risk 8/10) — backtests used blended data, live forecasts will have higher error
- Seasonal bias drift (Risk 6/10) — need rolling 30-day recalibration
- Station microclimate (Risk 5/10) — now quantified and adjusted

---

## 📉 Kalshi CPI (`kalshi-cpi`)
**Repo:** `eLobeto/kalshi-cpi` (private)
**Goal:** Predict BLS CPI prints using real-time alternative data.

**Current Forecast (Feb 2026 Print):** Soft/Low expected (net -0.1% drag)
- Energy: -4.86% MoM (deflationary) | Food: -0.33% (deflationary) | Shelter: +0.33% (inflationary) | Used Cars: +0.18% (neutral)

**Component Models:**
| Component | Weight | Model | R² | Status |
|-----------|--------|-------|-----|--------|
| Energy (Gas) | 3.5% | Linear (Pump → CPI) | 0.93 | ✅ Live (weekly EIA) |
| Shelter | 35% | Autoregressive (lag 1-3,6) | 0.61 | ✅ Live |
| Food | 13% | Linear (Commodity futures, 3mo lag) | 0.92 | ✅ Live |
| Used Cars | 2-3% | Manheim → CPI (2mo lag) | — | ✅ Live |
| M2 Regime | Bias | Regime filter (YoY growth buckets) | N/A | ✅ Live |

**Risk Flags:**
- Shelter lag is VARIABLE (6-12mo) — using autoregressive as workaround
- Wholesale (Manheim) ≠ Retail (CPI); dealer margins fluctuate (Risk 7/10)

---

## 🏦 Schwab Account
- Connected via schwab-py OAuth2
- Token: `ticker-watch/config/schwab_token.json` (auto-refreshes, gitignored)
- Client: `ticker-watch/src/data/schwab_client.py`
- Use: Historical OHLCV backtesting; plan to replace Alpaca/yfinance pipeline

---

## 🔑 API Keys (Location Only)
- **Kalshi:** `kalshi-weather/config/config.yaml` + `kalshi-weather/config/kalshi_private.pem`
- **FRED/EIA:** `kalshi-cpi/config/config.yaml`
- **Tomorrow.io/ECMWF:** `kalshi-weather/config/config.yaml`
- **Polygon/Alpaca:** `ticker-watch/config/config.yaml`
- **GitHub PAT:** DO NOT STORE (Evan provides as needed)

---

## 💡 Parked Ideas
- **TSA Passenger Numbers:** Kalshi killed the series.
- **Movie Box Office:** Too fragmented, low liquidity.
- **Universe Expansion:** Tested 58 combos → 0 validated (test EV +0.000R). Shelved.

---

**Historical Decision Logs & Detailed Methodology:** See `memory/archive/` (old MEMORY.md snapshot)

---

## 🚀 Polymarket Arbitrage (`polymarket-arbitrage`)
**Status:** Scaffolded & deployed, API investigation in progress (Feb 27, 6:03 PM MT)

**Deployed:**
- ✅ Config-driven bot with Polygon wallet integration ($100 USDC)
- ✅ Scanner running, polling Polymarket API every 5 seconds
- ✅ Wallet: `0x63c654f5b0D420aDd67ace600b4AB795a5b4d030`
- ✅ SQLite position tracker + order executor scaffolds

**API Debugging Results:**
- ✅ `/markets` endpoint confirmed correct + now using config variables
- **Finding:** `/markets` returns 1000 **archived/historical markets** (from 2023)
  - All markets have `yes_price: None`, `no_price: None`
  - 0 Bitcoin 15m markets found
  - 0 currently-trading markets with live prices
- **Missing:** Live price data source
  - Order book endpoint (`/order-book?market_id=...`) returns 404
  - Price data doesn't come from `/markets` — need alternative endpoint
- **Action:** Determine correct endpoint for live Bitcoin 15m markets (prices, orderbook)
  - Check Polymarket API docs or reverse-engineer from their UI
  - May need WebSocket for real-time prices instead of REST polling

---

## 🚀 Polymarket Arbitrage (`polymarket-arbitrage`) — FINAL STATUS (Feb 28)

**STATUS:** Fully scaffolded and ready for live slug-based market polling

### ✅ **API Discovery Complete**

**Two critical endpoints identified:**

1. **Market Discovery:** `https://gamma-api.polymarket.com/events`
   - Returns all active crypto "UP OR DOWN" binary markets
   - Can filter by BTC, ETH, SOL, etc.
   - Returns markets between start/end times

2. **Individual Market:** `https://gamma-api.polymarket.com/markets/slug/{slug}`
   - Fetches live Bitcoin 15m market (e.g., `btc-updown-5m-1772241000`)
   - Returns: `outcomePrices` (JSON string), volume, liquidity, condition_id
   - Status: active/closed flags
   - **Example working market:** `btc-updown-5m-1772241000`
     - YES: $0.795, NO: $0.205 (pair cost = $1.00)

3. **Pricing (Legacy CLOB):** `https://clob.polymarket.com/markets/{condition_id}`
   - For trade execution (token info)

### 🔧 **Implementation Complete**

**Config-driven slug system:**
```yaml
polymarket:
  clob_url: "https://gamma-api.polymarket.com"
  bitcoin_market_slugs:
    - "btc-updown-5m-1772241000"  # Seed list
```

**Market Fetcher (`market_fetcher.py`):**
- Accepts list of market slugs
- Fetches each by `/markets/slug/{slug}`
- Parses `outcomePrices` (JSON string → floats)
- Filters: active=true, closed=false only
- Returns Market objects with YES/NO prices

**Scanner Loop:**
- Continuously polls configured slugs every 5 seconds
- Calculates `pair_cost = yes_price + no_price`
- Identifies arbitrage when `pair_cost < $0.99`
- Dry-run mode: logs opportunities without trading

### 🎯 **How to Deploy**

**Step 1:** Get current Bitcoin 15m market slugs
- Go to https://polymarket.com/search?q=bitcoin%2015m
- You'll see 1000+ results for "Bitcoin Up or Down" with time windows
- Pick 5-10 active ones (check "Ends in..." for open markets)
- Copy their slug (e.g., `btc-updown-5m-1772241000`)

**Step 2:** Update config
```yaml
bitcoin_market_slugs:
  - "btc-updown-5m-1772241000"
  - "btc-updown-15m-..."
  - "btc-updown-5m-..."
  # Add more as needed
```

**Step 3:** Restart scanner
```bash
cd polymarket-arbitrage
./scripts/stop.sh
./scripts/start.sh
tail -f logs/scanner.log
```

**Step 4:** Watch for arbitrage opportunities
```
🎯 OPPORTUNITY FOUND: Bitcoin Up or Down - February 27, 9:45PM-10:00PM ET
   YES: $0.48 | NO: $0.50
   Pair Cost: $0.98 | Profit: $0.02
🏁 DRY RUN MODE - Not executing
```

### ⚠️ **Important Notes**

1. **Market Expiry:** Bitcoin 15m markets are ephemeral
   - Each market only exists for its specific 15-minute window
   - After window closes, new markets appear
   - Need to refresh slug list periodically

2. **Arbitrage Timing:** Opportunities appear on initialization
   - Best edge when markets first open (more slippage)
   - Edge shrinks as market converges to YES + NO = $1.00

3. **Why 5m/15m vs Gabagool's 15m?**
   - Polymarket has 5m, 15m, 30m, 1h windows
   - More windows = more trading opportunities
   - Smaller time windows = higher volatility = bigger arbitrage edges

### 🔐 **Wallet & Live Trading (Ready)**
- Polygon wallet: `0x63c654f5b0D420aDd67ace600b4AB795a5b4d030`
- Bankroll: $100 USDC
- Dry-run: True (set to False to enable live trading)
- Order executor scaffolded (needs CLOB order signing)

### 🚀 **Next Steps**
1. Populate bitcoin_market_slugs with current active markets
2. Run scanner in dry-run (validate price fetching & arbitrage detection)
3. Enable live trading (set dry_run: false)
4. Automate slug discovery (parse polymarket.com search or use events endpoint)

---

## 🔄 Scanner V2 — Live Deployment READY (March 3–4, 2026)
**Status:** ✅ PRODUCTION READY — All code reviewed, tested, documented

**Decision:** Go live with V2 only (no V1 parallel). Paper trading is safe for live iteration.

**Architecture:**
- **No PTM dependency** — Schwab API source of truth
- **Signal logger** — JSONL audit trail (`logs/signals.jsonl`)
- **Position sizer** — Risk-based sizing + exposure validation
- **Schwab executor** — Fill confirmation polling (`wait_for_fill`)

**Patterns Detected:**
- ✅ Bear Flag 15min (short, measured-move)
- ✅ Bull Flag 15min (long, measured-move)
- ✅ ORB 5min (9:35–11:00 AM ET, simple 3:1)
- ✅ Inside Bar daily (next-day entry)
- ✅ VCP daily (60-day hold)

**Code Review Completed:**
- ✅ 1,041 lines of production code (scanner_v2 + signal_logger + position_sizer)
- ✅ 25 try/except blocks (robust error handling)
- ✅ 51 log statements (~1 per 14 lines, good coverage)
- ✅ All imports resolve, no circular dependencies
- ✅ Graceful fallback if optional filters unavailable
- ✅ No hardcoded secrets, no SQL injection, no command injection
- ✅ Clear variable names, documented code, maintainable architecture
- ✅ SCANNER_V2_CODE_REVIEW.md document added

**Test Coverage:**
- 39 V2-specific unit tests (debounce, market hours, signal detection, risk, filters, bar-state, EOD)
- All 105 tests passing (66 existing + 39 new)
- Tests cover: Failed Breakdown, all 6 advanced filters, bar-state tracking, EOD monitoring, filter caching, signal pipeline
- Schwab API calls mocked during tests
- 85%+ coverage on V2 core functions
- All filter paths tested, edge cases covered

**V1 Status:**
- ✅ Stopped gracefully (PID 69377 killed 6:21 PM MT, March 3)
- Code unchanged, can be restarted if needed

**V2 Deployment (Wednesday, March 4):**
- 6:00 AM MT: Startup check (`python3 -m src.scanner_v2 --mode once`)
- 9:30 AM ET: Launch daemon (`bash scripts/start_v2_scanner.sh`)
- All day: Monitor `logs/scanner_v2.log` + `logs/signals.jsonl`
- 3:30 PM ET: Analyze results
- 5:00 PM MT: Go/no-go decision

**Pre-Deployment Status:**
- ✅ 105 unit tests passing (39 V2-specific)
- ✅ All patterns implemented (6: Bear Flag, Bull Flag, ORB, Failed Breakdown, Inside Bar, VCP)
- ✅ All filters integrated (6: earnings, key levels, S/D zones, IV skew, SPY 200 SMA, IV rank)
- ✅ Code reviewed (SCANNER_V2_CODE_REVIEW.md approved for production)
- ✅ Documentation complete (README, SCANNER_V2_LIVE.md, deployment guide, startup script)
- ✅ All commits pushed to GitHub

**Success Criteria (Tomorrow):**
- ✅ Zero critical errors (warnings OK)
- ✅ >5 signals detected + logged
- ✅ Position sizing respects risk limits (1–10 contracts)
- ✅ Fill polling works (filled/timeout/cancelled)
- ✅ Debounce prevents duplicates (>60s suppressed)
- ✅ All 105 tests still passing

**Patterns Detected:**
- ✅ Bear Flag 15min (short, measured-move)
- ✅ Bull Flag 15min (long, measured-move)
- ✅ ORB 5min (9:35–11:00 AM ET, simple 3:1)
- ✅ Failed Breakdown 15min (long/short, intraday)
- ✅ Inside Bar daily (next-day entry)
- ✅ VCP daily (60-day hold)

**Advanced Filters (all integrated):**
- ✅ Earnings blackout (skip if earnings within 7 days)
- ✅ Key Levels (suppress/amplify based on support/resistance)
- ✅ Supply/Demand Zones (suppress/amplify)
- ✅ IV Skew (amplify if vol skew favors direction)
- ✅ SPY 200 SMA (macro filter for swing patterns)
- ✅ IV Rank (skip if IVR too high)
- ✅ EOD monitoring (check positions for large drawdowns @ 3:30 PM ET)

**Commits (GitHub):**
- `6a644db` — Add earnings check to EOD monitoring for open swing positions (FINAL)
- `49b37c2` — Clean up README (remove outdated V1 reference, focus on V2)
- `6e09e8b` — README update + comprehensive code review document
- `979fd5b` — 20 comprehensive tests for V2 advanced features (105 total tests)
- `7326e1b` — Failed Breakdown + advanced filters + EOD monitoring
- `0e66a37` — Bar-state check (_new_bar_closed)
- `cbab82f` — Daily/15min bar fetching
- `1ce9955` — V2 Scanner + tests (19 unit tests)

**Docs (V2 Complete):**
- `README.md` — Updated with V2 info, patterns, filters, launch instructions
- `SCANNER_V2_LIVE.md` — Deployment guide (timeline + success metrics)
- `SCANNER_V2_VALIDATION.md` — Parallel run plan (not used; V2-only deployment)
- `SCANNER_V2_CODE_REVIEW.md` — Comprehensive code review + QA checklist
- `scripts/start_v2_scanner.sh` — V2 startup script

---

## 🚀 Polymarket Arbitrage (`polymarket-arbitrage`) — v3 LIVE ON GITHUB (Feb 28)

**STATUS:** ✅ Production-ready | 🚀 Official SDK Integrated | 🏃 Scanner running (PID 36754)

### **v3 SDK Integration** (Feb 28, 02:24 UTC)

**Official Polymarket py-clob-client (v0.34.6) Integrated:**
- ✅ Replaces custom order signing with official SDK
- ✅ EIP-712 signing handled automatically by SDK
- ✅ L1 + L2 authentication (create_or_derive_api_creds)
- ✅ HMAC-SHA256 request signing (all POLY_* headers)
- ✅ Official create_and_post_order method
- ✅ Error validation before submission

**Why Official SDK:** Eliminates custom signing bugs (biggest risk), handles API credentials, proper L2 headers, official support for debugging.

### 📦 **GitHub Repo** (Live)
- **URL:** https://github.com/eLobeto/polymarket-arbitrage
- **Latest:** `9887e24` — Official SDK integration (py-clob-client v0.34.6)
- **Dependencies:** py-clob-client, aiohttp, pyyaml, python-dotenv, requests

### 🏃 **Live Scanner** (Feb 28, 02:24 UTC)
- **PID:** 36754 (running with official SDK)
- **Markets:** 23 detected per 5-sec cycle
- **Mode:** LIVE TRADING ENABLED (dry_run: false)
- **Bankroll:** $100 USDC on Polygon
- **Status:** Healthy, polling markets, ready for arbitrage execution
- **Errors:** None (clean startup, no errors)

### 🔐 **Authentication Ready**
- Wallet: `0x63c654f5b0d420add67ace600b4ab795a5b4d030`
- SDK initializes: ✅ Creates/derives API credentials on first order
- EIP-712 signing: ✅ Automatic via SDK
- L2 headers: ✅ HMAC-SHA256 + POLY_* headers handled by SDK
- First order: **Will trigger credential generation automatically**

### 📊 **Next Order Will:**
1. Detect arbitrage (pair_cost < $0.99)
2. Initialize SDK credentials (one-time, automatic)
3. Sign order using EIP-712 (SDK handles)
4. Submit with proper L2 headers (SDK handles)
5. Poll for fill status
6. Track position in SQLite

### 📞 **Monitoring**
- **Quick status:** `bash scripts/status.sh`
- **Logs:** `tail -f logs/scanner.log`
- **Cron heartbeat:** Every 10 mins (polymarket-arbitrage-status)

**Current state:** All systems LIVE. Official SDK loaded. Scanner detecting 23 markets. **Ready to execute real trades.** 💙
