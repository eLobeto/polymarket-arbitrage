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

## ORB-FVG Strategy (2026-02-25 Decision)
**Pattern:** Opening Range Breakout + Fair Value Gap Retest (Var C, SPY-aligned)

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

**Live:** Paper trading, $500 bankroll, Kelly 25% sizing
- **NYC:** +43.6% ROI, Brier 0.071
- **Denver:** +97.5% ROI, best city (MAE 2.06°F)
- **Chicago:** Avoid band markets (Std 4.5°F, bias +3.0°F); thresholds only

**Risk Flags:**
- Reanalysis ≠ Forecast (Risk 8/10) — backtested on historical, live error will be higher
- Seasonal bias drift (Risk 6/10) — need rolling 30-day recalibration
- Station microclimate (Risk 4/10) — 1-2°F urban heat island effect

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
