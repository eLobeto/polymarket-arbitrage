# MEMORY.backup.md - Detailed Historical Archive
_Archived detailed version (Feb 2026). Contains full design decisions, filter reasoning, backtest timelines, and methodology. Reference for revisiting decisions or deep methodology dives._

---

## 🦅 Ticker Watch — Detailed Filter Architecture

### ✅ Confirmed Positive-EV Setups (Historical Baseline)
60d backtest, 10 tickers, fixed target:
- **Bear Flag 15min:** 33 signals | 33.3% WR | +4.47R avg win | **+0.825R EV**
- **Bull Flag 5min:** 68 signals | 17.6% WR | +3.26R avg win | **+0.608R EV** (DISABLED — turned negative later)

### 🛡️ Safety & Filters — Design Decisions

**1. Key Levels (PDH/PDL/PWH/PWL):**
- Suppress: Target within 0.5% of level (likely stall)
- Amplify: Entry breaks level by <0.5% (momentum confirmation)

**2. 200 SMA Battleground:**
- Suppress: Entry within 1.5% of 200 SMA (chop zone)
- Exception: Clean breakout (entry <0.5% from SMA) = AMPLIFIED
- Backtest validated: correctly blocked a -1R loss

**3. Supply/Demand Zones (SMC):**
- Algorithm: Relative range (Base < 80% Impulse)
- Daily: Max 3% width, expire after 3 touches
- Intraday: Max 1.5% width, expire after 1 touch
- Action: Suppress if entry/target inside opposite zone; Amplify if breaking out
- **EXCEPTION — IB Bull:** S/D zone SUPPRESS removed. Combined backtest showed suppressed IB Bull trades had +0.846R EV vs +0.438R baseline — filter was killing winners. IB Bull now AMPLIFY-only.

**⚠️ REMOVED: Target-Proximity Suppression**
- Originally: suppress if target within 0.5% of PDL/PWL/round number
- Backtest (30 trades, 2yr): suppressed cohort had +1.15R EV, 54% WR — we were killing winners
- Bear flags blow through prior lows rather than stalling — NOT a valid suppress signal
- Kept: 200 SMA battleground suppress + all amplify logic

**4. Vol Ratio + Bar Quality (Bear Flag Only):**
- Vol ratio ≥ 1.0 → suppress: High flag volume = buyers defending = flag integrity low
  - Backtest: vol<0.8 → +1.09R EV | vol≥1.0 → -0.15R EV
- Bar quality ≥ 0.70 → suppress: Extended signal bar = move already overdone
  - Backtest: bar_q≥0.70 → -0.49R EV | bar_q 0.4-0.7 → +1.11R EV

### 📊 Final Filter Backtest Results (30 trades, 2yr Alpaca SIP)
| Cohort | N | WR | EV |
|--------|---|----|----|
| Baseline | 30 | 40% | +0.631R |
| **Filtered (all filters)** | **18** | **55.6%** | **+1.256R** |
| Suppressed (skipped) | 12 | 17% | -0.305R |
| Amplified only | 3 | 67% | +2.377R |

**+0.625R EV improvement per trade vs baseline. Win rate 40% → 55.6%.**

### 🔬 Options Backtesting (Next Phase)
Black-Scholes simulation on 2yr Alpaca data — parameter grid:
- Strikes: ATM / 1-OTM / 2-OTM
- Expiries: 0-DTE / 1-DTE / Weekly
- Stop levels: -40% / -50% / -60% premium
- 162 combos × 2 signal types → find optimal contract spec

### 📊 Combined Full Backtest Results
_Intraday: 2yr Alpaca SIP (10 tickers) | Swing: 5yr yfinance (59 tickers) | All filters | No lookahead_

| Pattern | Signals | Resolved | Baseline EV | Filtered EV | EV Δ | Status |
|---------|---------|----------|------------|------------|------|--------|
| Bear Flag 15min | 30 | 30 | +0.631R | **+1.256R** | **+0.625R** | ✅ Trade |
| Bull Flag 5min | 191 | 191 | -0.081R | -0.105R | -0.024R | 🚫 Disabled |
| Bull Flag 15min | 124 | 124 | +0.255R | +0.264R | +0.009R | ✅ Trade |
| IB Bull | 1027 | 921 | +0.438R | +0.432R | -0.006R | ✅ Trade |
| IB Bear | 521 | 444 | +0.198R | +0.203R | +0.005R | ✅ Trade |
| VCP | 70 | 41 | +0.394R | +0.428R | +0.034R | 📋 Watch (60d hold → +0.541R EV) |

**Key findings:**
- Bear Flag filters = +0.625R EV lift — bar quality suppresses -0.486R EV losers; KL suppresses -1.000R EV; amplified trades +2.377R EV
- Bull Flag 5min is broken — negative EV baseline; suppressed cohort outperforms kept cohort; amplify logic misfires. Disabled live trading.
- IB Bull S/D zone suppress removed — suppressed IB Bull trades had +0.846R EV. IB Bull now AMPLIFY-only for S/D zones.
- VCP Key Level amplify works — amplified VCPs: 55.6% WR, +0.667R EV vs +0.394R baseline

### 🛠️ Live Scanner Implementation
- **1-min bar stop detection:** `fetch_latest_bars_multi()` fetches intrabar low/high for all open positions every loop — stops fire on any intrabar breach, not just bar close
- **Close order race condition fix:** 60s min-hold guard + auto-cancel entry order if 40310000 error (pending BUY conflicts with SELL)
- **News Telegram alerts suppressed:** `NEWS_TELEGRAM_ALERTS = False` — news scanner still feeds amplify/suppress logic
- **Alpaca Fill Reconciliation:** P&L tracked from actual Alpaca fills. `reconcile_alpaca_fills()` sets `fill_entry_price`, `fill_exit_price`, `actual_options_pnl`, `pnl_r`
- **Live paper P&L sample:** 6 real trades closed — 50% WR, avg win +1.07R, avg loss -0.13R, EV +0.47R/trade. Total +$11,573 one day. DB balance $26,687 (started $15k).
- **Scanner auto-restart:** NOT configured — need systemd or Docker `--restart=unless-stopped` before live capital

---

## 🎯 Swing Trade Patterns — 20-Year Deep Dive

Backtested 2006–2026 (20yr Schwab) + 2021–2025 (5yr yfinance), 59-ticker universe, next-day open entry, 60d max hold.

**20-Year Key Findings:**
- **IB Bull: regime-independent** — positive EV in ALL 21 years (2006–2026) including GFC 2008 (+0.514R), COVID 2020 (+0.378R), Bear 2022 (+0.391R). No macro filter needed.
- **IB Bear SPY filter REMOVED:** Tested SPY > 50 EMA suppress. Backtest showed suppressed trades had HIGHER EV — IB Bear shows stronger edge when a stock is weak vs. a bull market (relative weakness). IB Bear trades in ALL regimes without filter.
- **VCP SPY 200 SMA filter added:** Skip when SPY < 200 SMA. Crashes kill VCP: 2008 (-0.50R), 2011 (-0.75R), 2020 (-0.34R). Bull market only.
- **VCP ATR stop switched:** Stop is now `entry - 0.5×ATR(14)` instead of last pivot low. ATR experiment (20yr, 59 tickers, no filters) showed +0.490R EV improvement (+0.177R→+0.667R). Pivot low kept as fallback if ATR unavailable (<15 bars).

**Filters:** 50 EMA trend, 20≤ADX≤50, IB width 0.3–3%, vol compression <0.8×avg + VCP SPY macro filter

**VCP Timeout Analysis:** 41% of VCP trades timed out @ 30d. Late wins:losses = 4:1 (avg day 51). Extending hold 30→60d: +37% EV (+0.394R→+0.541R). Day-30 soft stop added: exit longs >2% below entry at day 30 (late-loss price avg was -2.9% at day 30 vs +6% for late wins).

---

## 🔓 ORB-FVG Strategy — Backtest Deep Dive

**Pattern:** Opening Range Breakout + Fair Value Gap Retest (Variation C: SPY-aligned)

**Backtest findings (6 months, 938 trades, Var B):**
- R:R sweep: WR barely drops as target extends (30% @1:1 → 21% @4:1) — fat-tail distribution
- 4:1 is first R:R to go aggregate-positive: EV -0.032R → +0.055R
- 2:1 would be WORSE (-0.178R); breakeven WR=33% >> actual WR=27%
- OR-width targets tested and REJECTED: WR drops 24%→16%, EV -0.032R→-0.377R (FVG retests fire mid-session after OR move already happened; full OR_width too far)

**Full backtest (all 4 variations × 2 universes × 2 R:R):**
| Config | N | WR | EV |
|--------|---|----|----|
| Var A (pure breakout) — any config | 975 | 3-8% | -0.67 to -0.86R ❌ (garbage) |
| Var B Full 3:1 | 938 | 24% | -0.032R ⚠️ |
| Var B Full 4:1 | 938 | 21% | +0.055R ✅ |
| Var B ORB-only 3:1 | 503 | 28% | +0.105R ✅ |
| Var B ORB-only 4:1 | 503 | 24% | +0.193R ✅ |
| Var C Full 3:1 | 858 | 25% | +0.016R ✅ |
| Var C Full 4:1 | 858 | 22% | +0.113R ✅ |
| **Var C ORB-only 3:1** | **464** | **30%** | **+0.198R ✅** |
| **Var C ORB-only 4:1 ← LIVE** | **464** | **26%** | **+0.293R ✅** |
| Var D ORB-only 4:1 | 335 | 23% | +0.164R ✅ |

---

## 🌦️ Kalshi Weather — Full Architecture

**Data Sources (4):** Open-Meteo (6 NWP models), NOAA/NWS (settlement source), Tomorrow.io (proprietary), ECMWF (ensemble)
**Signal:** 38 forecast points per city per day (30 GEFS ensemble + 6 deterministic + NWS + Tomorrow.io)
**Edge Detection:** Compare ensemble probability distribution to Kalshi implied odds
**Execution:** Paper trading with Kelly criterion sizing

**Key Design Decisions:**
- **Settlement Source:** NWS Climatological Report (CF6) — must match forecasts to exact station
- **Band Markets:** 2°F wide buckets. Probability = CDF(high+0.5) - CDF(low-0.5). 90% of liquidity here.
- **Threshold Markets:** "Above X°F" — use normal CDF for probability
- **Bias Corrections (Critical):**
  - NYC (KNYC): +1.1°F (model runs cold vs Central Park)
  - Denver (KDEN): +1.1°F
  - Chicago (KORD): +3.0°F (worst bias — avoid band markets, thresholds only)
- **Optimal Model Std:** 2.75°F for NYC (found via Brier score calibration)

**Trading Rules:**
- Bankroll: $500 (paper)
- Max Single Bet: 20%
- Max Daily Exposure: 40%
- Min Edge: 5%
- Kelly Fraction: 25% (conservative)
- Cron: Daily 7:00 AM MT scan + auto-execute

---

## 📉 Kalshi CPI — Full Component Breakdown

**Architecture:** Reconstruct CPI basket from high-frequency proxy data to predict official BLS release before it happens.

| Component | Weight | Model | Data Source | R² | Status |
|-----------|--------|-------|-------------|-----|--------|
| Energy (Gas) | 3.5% | Linear Regression (Pump Price → CPI Gas) | EIA API (weekly gas prices) | 0.93 | ✅ Live |
| Shelter | 35% | Autoregressive (lag 1-3, 6) | FRED API (CUSR0000SAH1) | 0.61 | ✅ Live |
| Food | 13% | Linear Regression (Commodity Futures → CPI Food, 3mo lag) | Yahoo Finance (Corn/Wheat/Soy/Cattle/Hogs/Sugar/Coffee) + FRED (CUSR0000SAF11) | 0.92 | ✅ Live |
| Used Cars | 2-3% | Transmission (Manheim → CPI, 2mo lag) | Manheim Scraper | — | ✅ Live |
| M2 Regime | Bias | Regime Classification (YoY growth buckets) | FRED API (M2SL) | N/A | ✅ Live |

**Key Design Decisions:**
- **Shelter Lag (Risk 9/10):** CPI Shelter lags Zillow ZORI by 6-12 months. This lag is VARIABLE. Using autoregressive model on official data as workaround until Zillow data source is secured.
- **Food Lag:** Commodity futures lead CPI Food by ~3 months (supply chain delay). R²=0.92 confirms.
- **M2 as Regime Filter:** Don't use M2 as a direct predictor (lag too long/variable). Use it to set a Bayesian prior: >8% YoY = inflationary bias, <2% = disinflationary bias.
- **Gas is the Fast Signal:** Updated weekly by EIA. Moves CPI the most in the short term despite small weight.
- **Wholesale ≠ Retail (Used Cars, Risk 7/10):** Manheim is wholesale. CPI is retail. Dealer margins fluctuate.

**Target Markets (Kalshi):**
- `KXCPIYOY` — YoY inflation. Top liquidity (76k total vol)
- `KXCPI` — MoM CPI change. 44k total vol
- `KXCPICORE` — Core CPI. 7.4k vol
- Sub-indices: `KXCPIGAS`, `KXCPIUSEDCAR`, `KXCPIFOOD`, `KXCPISHELTER`

---

## 💡 Research & Experimentation

**Universe Expansion Research:**
- Liquidity filter: 75 candidates → 29 passed
- Train/test backtest: 58 combos tested, 0 validated, 1 overfit
- Key finding: Best combo = none (test EV +0.000R)
- Results: agent-research/universe_expansion/

**Parked Ideas:**
- **TSA Passenger Numbers:** Kalshi killed the series. Dead.
- **Movie Box Office:** Too fragmented on Kalshi. Low liquidity.
- **CPI on Polymarket:** No daily weather markets. Overlap possible but less liquid than Kalshi.

---

_This archive preserves historical decision rationale, filter evolution, and backtest methodology. Reference for revisiting design choices or understanding why certain approaches were rejected._