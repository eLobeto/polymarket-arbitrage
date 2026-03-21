"""matcher.py — Match Kalshi/PM candle markets and detect arb windows."""
import logging
import time
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import (MIN_ARB_CENTS, MAX_PAIR_COST, MIN_SIDE_CENTS,
                    MAX_SIDE_CENTS, WINDOW_MIN_MINUTES,
                    CANDLE_OPEN_SKIP_MINUTES, ORACLE_MAX_DIVERGENCE_USD,
                    CANDLE_MOVE_MARGIN, CANDLE_MOVE_FLOOR,
                    ROLLBACK_BLACKLIST_SECS, DEAD_ZONE_MAX_USD,
                    ORACLE_ALLOW_ZERO_DZ_USD)
import price_feed
import div_fade_logger
import div_fade_5m

log = logging.getLogger("matcher")

CANDLE_ALIGN_SECS = 300  # candle ends must be within 5 minutes

# ── Rollback blacklist ────────────────────────────────────────────────────────
# After a rollback on asset+candle, blacklist it to prevent churn.
# Key: "ASSET:candle_end_ts" → blacklist expiry timestamp.
_rollback_blacklist: dict[str, float] = {}
_blacklist_lock = threading.Lock()

# ── Middle risk alert dedup ───────────────────────────────────────────────────
# Fire one Telegram alert per asset+candle+direction — not every 3s poll.
# Key: "ASSET:candle_end_ts:direction"
_middle_risk_alerted: set[str] = set()

# ── Oracle divergence velocity tracking ───────────────────────────────────────
# Circular buffer: asset → list of (timestamp, divergence_usd)
# Used to compute cf_cl_velocity ($/s) — positive = widening, negative = narrowing.
# A fast-widening divergence at entry is a stronger middling predictor than
# divergence level alone (see risk memo, Risk #1).
from collections import deque as _deque
_oracle_div_history: dict[str, _deque] = {}   # asset → deque[(ts, div)]
_VELOCITY_WINDOW_SECS = 60                    # look back up to 60s


def _get_oracle_velocity(asset: str, current_div: float) -> float | None:
    """
    Record current divergence and return velocity in $/s over the last 60s.
    Positive = divergence widening (increasing middle risk).
    Negative = divergence narrowing (converging feeds).
    Returns None if < 5s of history available.
    """
    now = time.time()
    if asset not in _oracle_div_history:
        _oracle_div_history[asset] = _deque(maxlen=30)

    hist = _oracle_div_history[asset]

    # Prune entries outside the window
    while hist and (now - hist[0][0]) > _VELOCITY_WINDOW_SECS:
        hist.popleft()

    velocity = None
    if hist:
        oldest_ts, oldest_div = hist[0]
        dt = now - oldest_ts
        if dt >= 5.0:  # need at least 5s of history to compute a meaningful rate
            velocity = round((current_div - oldest_div) / dt, 4)

    hist.append((now, current_div))
    return velocity



def blacklist_candle(asset: str, candle_end_ts: int):
    """Called by executor after a PM rollback. Blocks re-entry on this candle."""
    key = f"{asset}:{candle_end_ts}"
    with _blacklist_lock:
        _rollback_blacklist[key] = time.time() + ROLLBACK_BLACKLIST_SECS
    log.info("Blacklisted candle %s for %ds (rollback occurred)", key, ROLLBACK_BLACKLIST_SECS)


def _is_blacklisted(asset: str, candle_end_ts: int) -> bool:
    """Check if a candle is blacklisted from a previous rollback."""
    key = f"{asset}:{candle_end_ts}"
    with _blacklist_lock:
        expiry = _rollback_blacklist.get(key)
        if expiry is None:
            return False
        if time.time() > expiry:
            del _rollback_blacklist[key]
            return False
        return True



# ── Chainlink oracle cache ────────────────────────────────────────────────────
# Cache per-asset Chainlink prices for 30s to avoid hammering the RPC on every
# matcher cycle (runs every 3-5s).
_chainlink_cache: dict[str, tuple[float, float]] = {}  # asset → (price, timestamp)
_chainlink_lock = threading.Lock()
_CHAINLINK_TTL = 30.0  # seconds

# ── Spot price cache (Binance) ─────────────────────────────────────────────────
# On-chain Chainlink has a 0.5% deviation threshold (~$355 at $71k BTC) and a
# 1hr heartbeat — it can sit stale by $50+ during low-volatility sideways action,
# causing false-positive oracle divergence blocks.
# Binance spot tracks where Chainlink WILL settle within 0.5%, and updates in
# real-time. Use it as the primary oracle comparison; on-chain Chainlink is the
# fallback if Binance is unreachable.
_spot_cache: dict[str, tuple[float, float]] = {}  # asset → (price, timestamp)
_spot_lock  = threading.Lock()
_SPOT_TTL   = 5.0  # seconds — aggressive freshness

_BINANCE_SYMBOLS  = {"BTC": "BTCUSDT",  "ETH": "ETHUSDT"}
_COINBASE_SYMBOLS = {"BTC": "BTC-USD",  "ETH": "ETH-USD"}

# ── Order Book Imbalance (OBI) cache ─────────────────────────────────────────
# ρL = (bid_depth - ask_depth) / (bid_depth + ask_depth) across top L levels
# Range: -1.0 (pure sellers) → 0 (balanced) → +1.0 (pure buyers)
# Used as flow-toxicity gate: high +OBI = buyers dominating → adverse for PM_DN fades
_obi_cache: dict[str, tuple[float, float]] = {}   # asset → (obi, timestamp)
_OBI_TTL   = 5.0   # seconds — matches spot price TTL
_OBI_LEVELS = 10   # top-10 Binance depth levels

def _fetch_obi(asset: str) -> float | None:
    """Fetch Order Book Imbalance from Binance top-10 depth levels.

    Returns ρL in [-1.0, +1.0], or None on API failure.
    Positive = buy-side heavy (bullish pressure).
    Negative = sell-side heavy (bearish pressure / confirms PM_DN signal).
    Cached for 5s to avoid rate limiting.
    """
    now = time.time()
    cached = _obi_cache.get(asset)
    if cached and (now - cached[1]) < _OBI_TTL:
        return cached[0]

    symbol = _BINANCE_SYMBOLS.get(asset)
    if not symbol:
        return None

    try:
        r = requests.get(
            "https://api.binance.com/api/v3/depth",
            params={"symbol": symbol, "limit": _OBI_LEVELS},
            timeout=3,
        )
        if not r.ok:
            return None

        data  = r.json()
        bids  = data.get("bids", [])   # [[price_str, qty_str], ...]
        asks  = data.get("asks", [])

        bid_depth = sum(float(b[1]) for b in bids[:_OBI_LEVELS])
        ask_depth = sum(float(a[1]) for a in asks[:_OBI_LEVELS])
        total = bid_depth + ask_depth
        if total <= 0:
            return None

        obi = round((bid_depth - ask_depth) / total, 4)
        _obi_cache[asset] = (obi, now)
        return obi

    except Exception:
        return None

# ── CL-at-candle-open cache ──────────────────────────────────────────────────
# Snapshots the Chainlink price at the first observation of each candle.
# Key: "ASSET:candle_end_ts" → CL price at open.
# Used to separate oracle divergence from candle movement:
#   divergence = abs(CL_at_open - CF_at_open)   (how much oracles disagree)
#   candle_move = abs(CL_now - CL_at_open)       (how much price actually moved)
_cl_open_cache: dict[str, float] = {}  # "ASSET:candle_end_ts" → CL price

# Chainlink ETH/USD price feeds on Polygon (AggregatorV3)
_CHAINLINK_FEEDS = {
    "ETH": "0xF9680D99D6C9589e2a93a78A04A279e509205945",
    "BTC": "0xc907E116054Ad103354f2D350FD2514433D57F6f",
    # SOL/XRP: no reliable Polygon Chainlink feed — oracle check skipped (fail open)
}

_CHAINLINK_ABI = [
    {"inputs": [], "name": "latestRoundData", "outputs": [
        {"name": "roundId", "type": "uint80"},
        {"name": "answer", "type": "int256"},
        {"name": "startedAt", "type": "uint256"},
        {"name": "updatedAt", "type": "uint256"},
        {"name": "answeredInRound", "type": "uint80"}
    ], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [
        {"name": "", "type": "uint8"}
    ], "stateMutability": "view", "type": "function"},
]

_w3 = None
_decimals_cache: dict[str, int] = {}


def _get_w3():
    global _w3
    if _w3 is None:
        try:
            from web3 import Web3
            _w3 = Web3(Web3.HTTPProvider(
                "https://polygon-bor-rpc.publicnode.com",
                request_kwargs={"timeout": 6}))
        except Exception as e:
            log.warning("Failed to init Web3 for oracle check: %s", e)
    return _w3


def _get_chainlink_price(asset: str) -> float | None:
    """Get latest Chainlink price for an asset (cached 30s)."""
    now = time.time()
    with _chainlink_lock:
        cached = _chainlink_cache.get(asset)
        if cached and (now - cached[1]) < _CHAINLINK_TTL:
            return cached[0]

    feed_addr = _CHAINLINK_FEEDS.get(asset)
    if not feed_addr:
        return None

    w3 = _get_w3()
    if w3 is None:
        return None

    try:
        from web3 import Web3
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(feed_addr),
            abi=_CHAINLINK_ABI)

        if asset not in _decimals_cache:
            _decimals_cache[asset] = contract.functions.decimals().call()

        data = contract.functions.latestRoundData().call()
        price = data[1] / (10 ** _decimals_cache[asset])
        updated_at = data[3]

        # Reject stale prices (>5 min old)
        if now - updated_at > 300:
            log.debug("Chainlink %s price stale (%ds old)", asset, int(now - updated_at))
            return None

        with _chainlink_lock:
            _chainlink_cache[asset] = (price, now)
        return price

    except Exception as e:
        log.debug("Chainlink %s price fetch failed: %s", asset, e)
        return None


def _fetch_spot_price(asset: str) -> float | None:
    """Fetch real-time spot price from Binance (Coinbase as fallback).

    Used as the primary oracle comparison instead of on-chain Chainlink.
    On-chain Chainlink updates only on 0.5% deviation or 1hr heartbeat,
    causing false-positive divergence blocks during low-volatility periods.
    Binance spot is what Chainlink will actually settle near at candle close.
    """
    now = time.time()
    with _spot_lock:
        cached = _spot_cache.get(asset)
        if cached and (now - cached[1]) < _SPOT_TTL:
            return cached[0]

    price = None

    # Try Binance first
    symbol = _BINANCE_SYMBOLS.get(asset)
    if symbol:
        try:
            r = requests.get(
                f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
                timeout=3,
            )
            if r.ok:
                price = float(r.json()["price"])
        except Exception as e:
            log.debug("Binance spot fetch failed for %s: %s", asset, e)

    # Coinbase fallback
    if price is None:
        symbol_cb = _COINBASE_SYMBOLS.get(asset)
        if symbol_cb:
            try:
                r = requests.get(
                    f"https://api.coinbase.com/v2/prices/{symbol_cb}/spot",
                    timeout=3,
                )
                if r.ok:
                    price = float(r.json()["data"]["amount"])
            except Exception as e:
                log.debug("Coinbase spot fetch failed for %s: %s", asset, e)

    if price is not None:
        with _spot_lock:
            _spot_cache[asset] = (price, now)

    return price


# ── Candle open strike proxy ──────────────────────────────────────────────────
# Cache for Binance kline open prices.
# Key: "ASSET:start_ts:interval" → (price, fetch_ts)
_candle_open_cache: dict[str, tuple[float, float]] = {}
_candle_open_lock  = threading.Lock()
_CANDLE_OPEN_TTL   = 3600  # 1 hour


# ── ATR Cache ────────────────────────────────────────────────────────────────
# Cache for per-asset 1-minute ATR (volatility measurement).
# Key: "ASSET" → (atr_value, fetch_ts)
_atr_cache: dict[str, tuple[float, float]] = {}
_atr_lock  = threading.Lock()
_ATR_TTL   = 60.0  # 1 minute


def _fetch_atr(asset: str, period: int = 14) -> float | None:
    """
    Fetch last N 1-min klines from Binance and compute Average True Range.
    Used for dynamic dead-zone scaling (Risk #4).
    """
    now = time.time()
    with _atr_lock:
        cached = _atr_cache.get(asset)
        if cached and (now - cached[1]) < _ATR_TTL:
            return cached[0]

    symbol = _BINANCE_SYMBOLS.get(asset)
    if not symbol:
        return None

    try:
        # Fetch period + 1 klines to get enough data for True Range
        params = {
            "symbol": symbol,
            "interval": "1m",
            "limit": period + 1
        }
        r = requests.get("https://api.binance.com/api/v3/klines", params=params, timeout=5)
        if r.ok:
            klines = r.json()
            if len(klines) < period + 1:
                return None
            
            # Compute True Range for each bar
            tr_list = []
            for i in range(1, len(klines)):
                # klines format: [openTime, open, high, low, close, ...]
                curr_h = float(klines[i][2])
                curr_l = float(klines[i][3])
                prev_c = float(klines[i-1][4])
                
                tr = max(
                    curr_h - curr_l,
                    abs(curr_h - prev_c),
                    abs(curr_l - prev_c)
                )
                tr_list.append(tr)
            
            # Simple ATR (average of True Ranges)
            atr = sum(tr_list) / len(tr_list)
            
            with _atr_lock:
                _atr_cache[asset] = (atr, now)
            return atr
            
    except Exception as e:
        log.debug("ATR fetch failed for %s: %s", asset, e)

    return None


def _fetch_candle_open(asset: str, start_ts: int, interval_sec: int) -> float | None:
    """
    Fetch exact candle open price from Binance klines.
    Used for exact dead-zone calculation.
    """
    cache_key = f"{asset}:{start_ts}:{interval_sec}"
    now = time.time()

    with _candle_open_lock:
        cached = _candle_open_cache.get(cache_key)
        if cached and (now - cached[1]) < _CANDLE_OPEN_TTL:
            return cached[0]

    symbol = _BINANCE_SYMBOLS.get(asset)
    if not symbol:
        return None

    # Map interval_sec to Binance interval string
    interval_str = {300: "5m", 900: "15m", 3600: "1h"}.get(interval_sec, "15m")
    
    price = None
    try:
        # start_ts is in seconds, Binance needs milliseconds
        params = {
            "symbol": symbol,
            "interval": interval_str,
            "startTime": start_ts * 1000,
            "limit": 1
        }
        r = requests.get("https://api.binance.com/api/v3/klines", params=params, timeout=5)
        if r.ok:
            klines = r.json()
            if klines:
                price = float(klines[0][1])  # index 1 = open price
                log.debug(
                    "Candle open %s %s @ %d: $%.2f",
                    asset, interval_str, start_ts, price,
                )
    except Exception as e:
        log.debug("Candle open fetch failed for %s: %s", asset, e)

    if price is not None:
        with _candle_open_lock:
            _candle_open_cache[cache_key] = (price, now)

    return price


def _get_oracle_price(asset: str) -> tuple[float | None, str]:
    """Return the best available oracle price and its source label.

    Priority:
      1. Binance spot (5s TTL) — primary: always fresh, tracks Chainlink settlement
      2. On-chain Chainlink (30s TTL) — fallback: may lag $50+ in low-vol periods

    Returns (price, source) where source is one of: "spot", "chainlink", "none".
    """
    spot = _fetch_spot_price(asset)
    if spot is not None:
        return spot, "spot"

    cl = _get_chainlink_price(asset)
    if cl is not None:
        return cl, "chainlink"

    return None, "none"


def _check_oracle_divergence(asset: str, kalshi_strike: float) -> bool:
    """Return True if oracle divergence is SAFE (within threshold).
    Return False if divergence is too large (skip this arb).
    If we can't check (no feed, error), return True (allow trade)."""
    if kalshi_strike <= 0:
        return True  # no strike data — allow

    max_div = ORACLE_MAX_DIVERGENCE_USD.get(asset)
    if max_div is None:
        return True  # no threshold configured — allow

    oracle_price, oracle_src = _get_oracle_price(asset)
    if oracle_price is None:
        return True  # can't check — allow (fail open)

    divergence = abs(kalshi_strike - oracle_price)
    src_label = "Spot" if oracle_src == "spot" else "CL"
    if divergence > max_div:
        log.info(
            "Oracle divergence BLOCKED %s: Kalshi strike=$%.2f vs %s=$%.2f "
            "(diff=$%.2f > max=$%.2f) [src=%s]",
            asset, kalshi_strike, src_label, oracle_price, divergence, max_div, oracle_src)
        return False

    log.info(
        "Oracle OK %s: Kalshi=$%.2f %s=$%.2f Δ$%.2f (max=$%.2f) [src=%s]",
        asset, kalshi_strike, src_label, oracle_price, divergence, max_div, oracle_src)
    return True


def _prefetch_pm_prices(pm_markets: list[dict]) -> None:
    """Fetch all PM REST midpoints in parallel so matcher uses fresh prices."""
    tokens = [t for m in pm_markets
              for t in [m.get("up_token_id"), m.get("dn_token_id")] if t]
    if not tokens:
        return
    with ThreadPoolExecutor(max_workers=min(len(tokens), 8)) as ex:
        futures = {ex.submit(price_feed.get_pm_price, t): t for t in tokens}
        for f in as_completed(futures):
            f.result()  # results already stored in cache by get_pm_price()


def find_arb_windows(kalshi_markets: list[dict], pm_markets: list[dict]) -> list[dict]:
    """
    For each (Kalshi, PM) pair on same asset + aligned candle window,
    compute cross-platform compression opportunities.

    Two arb directions:
      1. Buy PM UP  + Buy Kalshi NO  → profitable if combined < 100¢
      2. Buy PM DN  + Buy Kalshi YES → profitable if combined < 100¢
    """
    windows = []

    # Fetch all PM REST midpoints in parallel — ensures fresh order book prices,
    # not stale WS last-trade prices which can lag by minutes in directional markets
    _prefetch_pm_prices(pm_markets)

    for km in kalshi_markets:
        for pm in pm_markets:
            if km["asset"] != pm["asset"]:
                continue
            # Must be same timeframe — a 5m PM candle and 15m Kalshi candle share an
            # endpoint but measure different price moves; they are NOT the same event.
            if km["timeframe"] != pm["timeframe"]:
                continue
            # Candle alignment (end times must match within 5 min)
            if abs(km["candle_end_ts"] - pm["candle_end_ts"]) > CANDLE_ALIGN_SECS:
                continue
            # Both must have enough time left
            if min(km["minutes_left"], pm["minutes_left"]) < WINDOW_MIN_MINUTES:
                continue
            # Skip first CANDLE_OPEN_SKIP_MINUTES of candle — Kalshi books empty at open
            tf_mins = 15 if pm["timeframe"] == "15m" else 5
            if pm["minutes_left"] > tf_mins - CANDLE_OPEN_SKIP_MINUTES:
                continue

            # ── Rollback blacklist check ───────────────────────────────────
            # If we already rolled back on this asset+candle, skip it to
            # prevent churn (repeated buy/sell slippage on thin books).
            if _is_blacklisted(km["asset"], km["candle_end_ts"]):
                continue

            # ── Oracle divergence check ───────────────────────────────────
            # Kalshi uses CF Benchmarks; PM uses Chainlink. If their strikes
            # at candle open diverge too much, a small price move can make
            # BOTH sides lose ("middling").
            #
            # Direction-aware: if oracle divergence is high but the dead zone
            # for a specific direction is ZERO (Kalshi strike on the safe side
            # of PM open), that direction carries no structural middle risk and
            # is allowed through. Directions with dead_zone > 0 are still
            # blocked. See dead zone check below in the direction loop.
            kalshi_strike = km.get("floor_strike", 0)
            oracle_blocked = not _check_oracle_divergence(km["asset"], kalshi_strike)
            _oracle_divergence = 0.0  # raw divergence value for tiered check below
            if oracle_blocked:
                # Log div_fade signal (dry-run) regardless of direction decision
                _cl, _ = _get_oracle_price(km["asset"])
                if _cl is not None:
                    _oracle_divergence = abs(kalshi_strike - _cl)
                    _min_left = min(km["minutes_left"], pm["minutes_left"])
                    # Compute velocity + OBI so both loggers can record them
                    _fade_velocity = _get_oracle_velocity(km["asset"], _oracle_divergence)
                    _spot_obi = _fetch_obi(km["asset"])
                    div_fade_logger.maybe_log_fade_signal(
                        asset=km["asset"],
                        kalshi_strike=kalshi_strike,
                        cl_now=_cl,
                        minutes_left=_min_left,
                        candle_end_ts=km["candle_end_ts"],
                        pm_up_price=price_feed.get_pm_price(pm.get("up_token_id")),
                        pm_dn_price=price_feed.get_pm_price(pm.get("dn_token_id")),
                        kal_ticker=km.get("ticker", ""),
                        pm_up_token_id=pm.get("up_token_id", ""),
                        pm_dn_token_id=pm.get("dn_token_id", ""),
                        oracle_velocity=_fade_velocity,
                        spot_obi=_spot_obi,
                    )
                    div_fade_5m.maybe_log_5m_signal(
                        asset=km["asset"],
                        kalshi_strike=kalshi_strike,
                        cl_now=_cl,
                        minutes_left_15m=_min_left,
                        oracle_velocity=_fade_velocity,
                        spot_obi=_spot_obi,
                    )
                # Don't continue — let the direction loop decide per dead zone

            # ── Oracle velocity tracking ───────────────────────────────────
            # Record divergence (even for non-blocked trades) and compute
            # $/s velocity. Positive = widening, negative = narrowing.
            # cf_cl_velocity logged in every window for future backtesting.
            _oracle_velocity: float | None = None
            if kalshi_strike > 0:
                if _oracle_divergence == 0.0:
                    # Non-blocked path — divergence wasn't computed above.
                    # Fetch now (cached, no extra API call within 5s TTL).
                    _cl_vel, _ = _get_oracle_price(km["asset"])
                    if _cl_vel is not None:
                        _oracle_divergence = abs(kalshi_strike - _cl_vel)
                _oracle_velocity = _get_oracle_velocity(km["asset"], _oracle_divergence)

            # ── Dynamic candle movement check ─────────────────────────────
            # Snapshot CL price at candle open to separate two signals:
            #   divergence = abs(CL_open - CF_open)  → oracle disagreement
            #   candle_move = abs(CL_now - CL_open)  → actual price movement
            #   min_move = max(divergence + margin, floor)
            # Tight oracles ($2 apart) + $10 margin → need $12 BTC move.
            # Wide oracles ($30 apart) + $10 margin → need $40 BTC move.
            _margin = CANDLE_MOVE_MARGIN.get(km["asset"])
            if _margin is not None and kalshi_strike > 0:
                cl_price, _ = _get_oracle_price(km["asset"])
                if cl_price is not None:
                    # Cache CL price at first observation of this candle
                    _candle_key = f"{km['asset']}:{km['candle_end_ts']}"
                    if _candle_key not in _cl_open_cache:
                        _cl_open_cache[_candle_key] = cl_price
                        log.info(
                            "Candle open snapshot %s: CL=$%.2f CF=$%.2f div=$%.2f",
                            km["asset"], cl_price, kalshi_strike,
                            abs(cl_price - kalshi_strike))

                    cl_at_open = _cl_open_cache[_candle_key]
                    divergence = abs(cl_at_open - kalshi_strike)
                    candle_move = abs(cl_price - cl_at_open)
                    _floor = CANDLE_MOVE_FLOOR.get(km["asset"], 1.0)
                    min_move = max(divergence + _margin, _floor)

                    if candle_move < min_move:
                        log.info(
                            "Candle too flat %s: move=$%.2f < min=$%.2f "
                            "(div=$%.2f + margin=$%.2f) — skipping",
                            km["asset"], candle_move, min_move,
                            divergence, _margin)
                        continue
                    log.info(
                        "Candle move OK %s: $%.2f (min $%.2f, div=$%.2f)",
                        km["asset"], candle_move, min_move, divergence)

            # Fetch current spot (cached) — used in dead-zone fallback when no kline data
            _spot_now, _ = _get_oracle_price(km["asset"])

            # Fetch PM candle open price (Binance klines) for exact dead-zone calc
            _tf_secs     = {"5m": 300, "15m": 900, "1h": 3600}.get(pm["timeframe"], 900)
            _candle_start = pm["candle_end_ts"] - _tf_secs
            _pm_candle_open = _fetch_candle_open(km["asset"], _candle_start, _tf_secs)

            # Get live prices
            k_prices = price_feed.get_kalshi_price(km["ticker"])
            if k_prices:
                k_yes = k_prices["yes"]
                k_no  = k_prices["no"]
            else:
                k_yes = km["yes_cents"]
                k_no  = km["no_cents"]

            up_token = pm.get("up_token_id")
            dn_token = pm.get("dn_token_id")

            pm_up = price_feed.get_pm_price(up_token) if up_token else pm.get("up_cents")
            pm_dn = price_feed.get_pm_price(dn_token) if dn_token else pm.get("dn_cents")

            if pm_up is None or pm_dn is None:
                pm_up = pm.get("up_cents", 50)
                pm_dn = pm.get("dn_cents", 50)

            # Check both arb directions
            for direction, pm_price, kal_price, pm_side, kal_side, pm_token in [
                ("buy_pm_up_kal_no",  pm_up, k_no,  "UP",   "NO",  up_token),
                ("buy_pm_dn_kal_yes", pm_dn, k_yes, "DOWN", "YES", dn_token),
            ]:
                combined = pm_price + kal_price
                profit   = 100 - combined

                if combined >= MAX_PAIR_COST:
                    continue
                if profit < MIN_ARB_CENTS:
                    continue
                if pm_price < MIN_SIDE_CENTS or pm_price > MAX_SIDE_CENTS:
                    continue
                if kal_price < MIN_SIDE_CENTS or kal_price > MAX_SIDE_CENTS:
                    continue

                # ── Strike alignment / middle risk guard ──────────────────
                # PM UP/DOWN markets settle relative to the candle OPEN price
                # (the PM effective strike). Kalshi has an explicit floor_strike.
                # If these differ, a dead zone exists where BOTH legs lose:
                #   buy_pm_dn_kal_yes: dead zone = [pm_open, kalshi_strike]
                #                      (if pm_open < kalshi_strike)
                #   buy_pm_up_kal_no:  dead zone = [kalshi_strike, pm_open]
                #                      (if pm_open > kalshi_strike)
                # Use exact Binance kline open; fall back to 50¢ proxy if unavailable.
                #
                # Oracle-blocked trades are also resolved here:
                #   dead_zone == 0 → safe direction despite high oracle divergence → allow
                #   dead_zone > 0  → risky direction → block (same as non-oracle-blocked)
                _dead_zone     = None   # set inside guard; None if no strike data
                _oracle_allowed = False  # set True if ORACLE_ALLOW fires
                if kalshi_strike > 0:
                    _max_dz = DEAD_ZONE_MAX_USD.get(km["asset"], 25.0)
                    _pm_up  = pm_price if pm_side == "UP" else (100.0 - pm_price)
                    _kal_yes = kal_price if kal_side == "YES" else (100.0 - kal_price)

                    if _pm_candle_open is not None:
                        # Exact dead zone from Binance kline open
                        if direction == "buy_pm_dn_kal_yes":
                            _dead_zone = max(0.0, kalshi_strike - _pm_candle_open)
                        else:  # buy_pm_up_kal_no
                            _dead_zone = max(0.0, _pm_candle_open - kalshi_strike)
                        _blocked  = _dead_zone > _max_dz
                        _dz_label = f"dead_zone=${_dead_zone:.0f} (max=${_max_dz:.0f}, pm_open=${_pm_candle_open:,.0f})"
                    else:
                        # Fallback: directional proxy (PM_UP vs 50¢)
                        if _spot_now is not None:
                            _kal_above = kalshi_strike > _spot_now
                            _pm_above  = _pm_up < 50.0
                            _blocked   = _kal_above != _pm_above
                        else:
                            _blocked = False
                        _dead_zone = -1.0  # unknown
                        _dz_label  = f"proxy (no kline data), spot=${_spot_now}"

                    # ── Oracle-block direction override ────────────────────
                    # Three tiers when oracle divergence > ORACLE_MAX_DIVERGENCE_USD:
                    #   1. dead_zone > 0: risky direction — hard block always
                    #   2. dead_zone == 0, div < ceiling: safe direction — allow [ORACLE_ALLOW]
                    #   3. dead_zone == 0, div > ceiling: extreme divergence — block [ORACLE_CEILING]
                    if oracle_blocked:
                        _allow_ceiling = ORACLE_ALLOW_ZERO_DZ_USD.get(km["asset"], 75.0)
                        if _dead_zone == 0.0 and _oracle_divergence <= _allow_ceiling:
                            log.info(
                                "Oracle override ALLOWED %s: zero dead-zone direction "
                                "($%d div <= $%d ceiling) — proceeding with caution",
                                km["asset"], _oracle_divergence, _allow_ceiling)
                            _oracle_allowed = True  # flag for entry alert
                        else:
                            _block_reason = "risky direction" if _dead_zone > 0 else \
                                           (f"div=${_oracle_divergence:.0f} > ceiling=${_allow_ceiling:.0f}")
                            log.info(
                                "Oracle override BLOCKED %s: %s [div=$%.0f, dz=$%.0f]",
                                km["asset"], _block_reason, _oracle_divergence, _dead_zone)
                            continue

                    if _blocked:
                        log.info(
                            "Strike alignment BLOCKED %s: %s %s — %s",
                            km["asset"], direction, pm["condition_id"][:12], _dz_label)
                        continue

                _window = {
                    "asset":           km["asset"],
                    "timeframe":       pm["timeframe"],
                    "direction":       direction,
                    "pm_side":         pm_side,
                    "kal_side":        kal_side,
                    "pm_price":        round(pm_price, 2),
                    "kal_price":       round(kal_price, 2),
                    "combined":        round(combined, 2),
                    "profit_cents":    round(profit, 2),
                    "pm_token_id":     pm_token,
                    "kal_ticker":      km["ticker"],
                    "pm_condition_id": pm["condition_id"],
                    "minutes_left":    min(km["minutes_left"], pm["minutes_left"]),
                    "kalshi_market":   km,
                    "pm_market":       pm,
                    # Risk context for entry alert and logging
                    "kalshi_strike":     kalshi_strike,
                    "pm_candle_open":    _pm_candle_open,
                    "dead_zone":         _dead_zone,
                    "oracle_divergence": _oracle_divergence,
                    "oracle_allowed":    _oracle_allowed,
                    "oracle_velocity":   _oracle_velocity,   # $/s: + widening, - narrowing, None=no history
                    "spot_obi":          _fetch_obi(km["asset"]),  # order book imbalance: -1 (sellers) → +1 (buyers)
                }

                windows.append(_window)

    windows.sort(key=lambda x: x["profit_cents"], reverse=True)
    if windows:
        log.info("Arb windows: %d found (best: %s %s %.1f¢ profit)",
                 len(windows), windows[0]["asset"], windows[0]["direction"],
                 windows[0]["profit_cents"])
    return windows
