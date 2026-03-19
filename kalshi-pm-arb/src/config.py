"""config.py — Cross-platform candle arb bot settings."""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from config/ directory relative to project root
_ENV_PATH = Path(__file__).parent.parent / "config" / ".env"
load_dotenv(_ENV_PATH)

# Assets
ASSETS = ["BTC", "ETH"]

# Arb thresholds
MIN_ARB_CENTS    = 12.0    # min profit in cents (headroom for Kalshi drift between legs)
MAX_PAIR_COST    = 97      # combined cost must be under this (cents)
MIN_SIDE_CENTS   = 10.0   # skip if either side < 10¢
MAX_SIDE_CENTS   = 90.0   # skip if either side > 90¢

# Execution
LIVE_STAKE_USD          = 150.0  # $150/leg
WINDOW_MIN_MINUTES      = 2.0   # skip if candle closes within this many minutes
POLL_INTERVAL_SECS          = 3    # active hours (13:00-23:00 UTC)
POLL_INTERVAL_OVERNIGHT_SECS = 5  # 23:00-13:00 UTC
MARKET_REFRESH_SECS     = 60
# COOLDOWN_SECS removed — cooldown expiry is now candle_end_ts (resets at candle boundary, not fixed duration)
MAX_DIRECTIONAL_USD     = 180.0  # halt if open one-sided PM exposure exceeds this
DIRECTIONAL_MIN_CENTS   = 12.0   # min signal strength for depth-gate PM-only entry
DIRECTIONAL_MAX_PER_SIDE = 2     # max concurrent UP or DOWN directional positions

# ── Volatility scaling ───────────────────────────────────────────────────────
VOL_SCALING_ENABLED     = True
MOMENTUM_FILTER_ENABLED = True
MOMENTUM_MAX_PCT        = 0.20   # % move threshold
MOMENTUM_ARB_REDUCTION  = 0.50   # reduce arb stake 50% when PM is against Binance
PM_MAX_SLIPPAGE_CENTS   = 3.5    # max acceptable PM VWAP slippage above signal (was 2.0)
DIRECTIONAL_STAKE_USD   = 20.0   # per-trade stake for directionals

# ── Candle timing ─────────────────────────────────────────────────────────────
CANDLE_OPEN_SKIP_MINUTES = 3.0   # skip first N min of candle (Kalshi books empty at open)

# ── Oracle divergence protection ──────────────────────────────────────────────
# Kalshi uses CF Benchmarks; PM uses Chainlink. If their candle-open strikes
# diverge by more than this, the "arb" is a mirage — a small candle move can
# make you lose BOTH sides. Set per-asset in USD.
ORACLE_MAX_DIVERGENCE_USD = {
    "BTC": 25.0,    # BTC ~$80k → $25 is ~0.03% (tightened from $50 to cut middling risk)
    "ETH": 5.0,     # ETH ~$2.1k → $5 is ~0.24% (raised from $3 — <$5 div is -EV)
    "SOL": 0.50,    # SOL ~$130 → $0.50 is ~0.38%
    "XRP": 0.005,   # XRP ~$2.3 → $0.005 is ~0.22%
}

# ── Dead zone thresholds (direction-level middle risk guard) ──────────────────
# The dead zone is |Kalshi_strike - PM_candle_open| — the price range where
# BOTH legs lose (one settles just below/above its own strike).
# If dead_zone > threshold → skip that direction.
# Applied PER DIRECTION within a pair, not to the whole market.
DEAD_ZONE_MAX_USD = {
    "BTC": 25.0,   # same scale as ORACLE_MAX_DIVERGENCE_USD by design
    "ETH": 5.0,
}

# ── Oracle override ceiling (zero-dead-zone directions only) ─────────────────
# When oracle_divergence > ORACLE_MAX_DIVERGENCE_USD but a direction has
# dead_zone == 0, the direction is still allowed IF divergence is below this
# ceiling. Above this ceiling it's extreme enough to block even "safe" directions.
ORACLE_ALLOW_ZERO_DZ_USD = {
    "BTC": 75.0,   # allow zero-DZ direction up to $75 total CF/CL divergence
    "ETH": 20.0,
}

# ── Minimum candle movement (dynamic) ────────────────────────────────────────
# min_move = observed_oracle_divergence + CANDLE_MOVE_MARGIN, floored at
# CANDLE_MOVE_FLOOR. Oracle divergence is measured at candle open by caching
# the Chainlink price alongside the Kalshi floor_strike (CF Benchmarks open).
# This separates "how much oracles disagree" from "how much price moved."
CANDLE_MOVE_MARGIN = {
    "BTC": 10.0,    # need $10 beyond observed divergence
    "ETH": 2.0,     # need $2 beyond observed divergence
}
CANDLE_MOVE_FLOOR = {
    "BTC": 10.0,    # absolute minimum $10 move
    "ETH": 1.50,    # absolute minimum $1.50 move
}

# ── Divergence Fade Live Trading (Strategy #1) ───────────────────────────────
# ETH goes live; BTC still logged for data but not traded.
# Fires a directional PM bet when CF Benchmarks leads Chainlink by > threshold,
# betting that Chainlink reprices within the 15-minute candle window.
DIV_FADE_ENABLED         = True
DIV_FADE_STAKE_USD       = 100.0     # $100 cap per trade
DIV_FADE_MAX_PRICE_CENTS = 60.0      # skip if PM already repriced above 60¢
DIV_FADE_MIN_PRICE_CENTS = 45.0      # skip if PM price < 45¢ (kills low-edge signals)

# ── Per-signal go-live controls ───────────────────────────────────────────────
# Each key is "ASSET_TF_SIGNAL". Set True only after meeting go-live thresholds.
# Go-live threshold: DIV_FADE_GO_LIVE_MIN_RESOLVED signals resolved at > DIV_FADE_GO_LIVE_MIN_WR.
# All False = paper-only. Flip individual signals when data validates edge.
DIV_FADE_LIVE_SIGNALS: dict[str, bool] = {
    "BTC_15m_PM_UP": False,
    "BTC_15m_PM_DN": False,
    "ETH_15m_PM_UP": False,
    "ETH_15m_PM_DN": False,
    "BTC_5m_PM_UP":  False,
    "BTC_5m_PM_DN":  False,
    "ETH_5m_PM_UP":  False,
    "ETH_5m_PM_DN":  False,
}
DIV_FADE_GO_LIVE_MIN_RESOLVED = 50    # minimum resolved outcomes required
DIV_FADE_GO_LIVE_MIN_WR       = 0.50  # minimum win rate required

# (Divergence Collapse Entry / Strategy #3 removed — see git history)

# ── Rollback churn prevention ────────────────────────────────────────────────
# After a PM rollback on a given asset+candle, blacklist that candle for this
# many seconds. Prevents the bot from re-entering the same thin-book arb and
# burning money on repeated buy/sell slippage.
ROLLBACK_BLACKLIST_SECS = 900  # 15 min — effectively the rest of the candle

# ── Correlated entry cap (Risk #5) ────────────────────────────────────────────
# BTC and ETH signals are direction-neutral per market but driven by the same
# macro move. If both assets fire an arb within this window, only the first
# (highest-profit) entry is taken. The second is blocked to cap worst-case
# capital deployed into a single vol spike.
# Time-windowed (not a hard global cap) — independent signals that happen to
# fire 60+ seconds apart are treated as uncorrelated and both allowed.
CORRELATED_ENTRY_WINDOW_SECS = 30

# ── Kalshi maker orders ───────────────────────────────────────────────────────
KALSHI_MAKER_MODE              = True
MAKER_CANCEL_MINS_BEFORE_CLOSE = 5.0
MAKER_MIN_MINS_LEFT            = 7.0  # Minimum candle time remaining to use maker mode
MAKER_MAX_PRICE_DRIFT_CENTS    = 1.5   # Cancel if signal price drifts >1.5c
MAKER_MAX_PENDING_USD          = 300.0 # Max total PM value unhedged waiting for maker fills
MAKER_POLL_INTERVAL_SECS       = 5.0   # Frequency for maker status checks

# ── Auto-rebalance: PM USDC.e ↔ Kalshi ──────────────────────────────────────
REBALANCE_ENABLED       = False   # disabled until bridge is configured
REBALANCE_TRIGGER_USD   = 150.0
REBALANCE_TARGET_USD    = 350.0
REBALANCE_PM_FLOOR_USD  = 150.0
REBALANCE_MIN_AMOUNT    = 50.0

PM_REBALANCE_TRIGGER_USD  = 150.0
PM_REBALANCE_TARGET_USD   = 500.0
PM_REBALANCE_KALSHI_FLOOR = 300.0
PM_REBALANCE_MIN_AMOUNT   = 50.0

# API endpoints
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_WS_URL   = "wss://api.elections.kalshi.com/trade-api/ws/v2"
PM_CLOB_URL     = "https://clob.polymarket.com"
PM_WS_URL       = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_API_URL   = "https://gamma-api.polymarket.com"

# Credentials (loaded from config/.env)
KALSHI_KEY_ID            = os.getenv("KALSHI_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH  = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
PM_PRIVATE_KEY           = os.getenv("PM_PRIVATE_KEY", "")
PM_API_KEY               = os.getenv("PM_API_KEY", "")
PM_API_SECRET            = os.getenv("PM_API_SECRET", "")
PM_API_PASSPHRASE        = os.getenv("PM_API_PASSPHRASE", "")
PM_FUNDER                = os.getenv("PM_FUNDER", "")
TG_TOKEN                 = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID               = os.getenv("TELEGRAM_CHAT_ID", "")

LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() == "true"
