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
                    ROLLBACK_BLACKLIST_SECS,
                    DIV_COLLAPSE_STAKE_USD, DIV_COLLAPSE_THRESHOLD,
                    DIV_COLLAPSE_PEAK_FACTOR, DIV_COLLAPSE_WINDOW_SECS,
                    DIV_COLLAPSE_MAX_GAP_RATIO)
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

# ── Open arb guard ────────────────────────────────────────────────────────────
import json as _json
import os as _os

_OPEN_ARBS_FILE = _os.path.join(
    _os.path.dirname(_os.path.abspath(__file__)), "..", "logs", "open_arbs.json"
)

def _is_ticker_already_open(kal_ticker: str) -> bool:
    """Return True if kal_ticker already has an open arb position tracked.

    Prevents Divergence Collapse from double-entering a candle that was
    already filled by the normal arb path.
    """
    try:
        with open(_OPEN_ARBS_FILE) as f:
            open_arbs = _json.load(f)
        return any(
            v.get("kal_ticker") == kal_ticker
            for v in open_arbs.values()
        )
    except Exception:
        return False  # fail open — don't block on file error


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

# ── Divergence history for collapse detection (Strategy #3) ──────────────────
# Records blocked divergence readings per asset. When divergence has dropped
# ≥DIV_COLLAPSE_THRESHOLD from its peak within DIV_COLLAPSE_WINDOW_SECS,
# we allow entry at a reduced stake — the oracle gap is closing fast.
_divergence_history: dict[str, list] = {}   # asset → [(timestamp, divergence)]
_div_hist_lock = threading.Lock()

# Tracks which (asset:candle_end_ts) pairs have already sent a collapse alert.
_collapse_alerted: set[str] = set()


def _record_and_check_collapse(asset: str, divergence: float) -> bool:
    """Record a blocked divergence and check if a collapse has occurred.

    Returns True if divergence has dropped ≥DIV_COLLAPSE_THRESHOLD from the
    recent peak (within DIV_COLLAPSE_WINDOW_SECS seconds), signalling that
    oracle convergence is in progress and middling risk is falling.
    """
    now = time.time()
    cutoff = now - DIV_COLLAPSE_WINDOW_SECS

    with _div_hist_lock:
        hist = _divergence_history.setdefault(asset, [])
        hist.append((now, divergence))
        hist[:] = [(t, d) for t, d in hist if t >= cutoff]  # prune old entries

        if len(hist) < 3:
            return False  # need at least 3 readings before declaring a collapse

        max_div = max(d for _, d in hist)
        oracle_max = ORACLE_MAX_DIVERGENCE_USD.get(asset, float("inf"))
        if max_div < oracle_max:
            return False  # peak was never in blocked territory — not a convergence event

        drop_pct = (max_div - divergence) / max_div if max_div > 0 else 0
        # Matches original 51b88aa logic: ≥35% drop AND peak ≥1.5× current.
        # Added DIV_COLLAPSE_MAX_GAP_RATIO check: the gap must have collapsed
        # to within a reasonable multiple of the normal threshold to be safe.
        is_collapse = (
            drop_pct >= DIV_COLLAPSE_THRESHOLD
            and max_div >= divergence * DIV_COLLAPSE_PEAK_FACTOR
            and divergence <= oracle_max * DIV_COLLAPSE_MAX_GAP_RATIO
        )

        if is_collapse:
            log.info(
                "[DIV_COLLAPSE] %s: div $%.2f dropped %.0f%% from peak $%.2f "
                "(≥%d%% required, peak/now=%.1f×) — allowing entry at $%.0f/leg",
                asset, divergence, drop_pct * 100, max_div,
                int(DIV_COLLAPSE_THRESHOLD * 100),
                max_div / divergence if divergence > 0 else 0,
                DIV_COLLAPSE_STAKE_USD,
            )
        return is_collapse


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
            # BOTH sides lose ("middling"). Skip when divergence > threshold.
            kalshi_strike = km.get("floor_strike", 0)
            if not _check_oracle_divergence(km["asset"], kalshi_strike):
                _cl, _ = _get_oracle_price(km["asset"])  # spot-first, cached
                _divergence = abs(kalshi_strike - _cl) if _cl is not None else None

                # Strategy #3: Divergence Collapse — allow entry at reduced stake
                # when oracle gap has been rapidly shrinking (≥30% drop in 60s).
                _is_collapse = (
                    _divergence is not None
                    and _record_and_check_collapse(km["asset"], _divergence)
                )

                if not _is_collapse:
                    # Normal oracle block — log div_fade signal (dry-run) and skip.
                    if _cl is not None:
                        _min_left = min(km["minutes_left"], pm["minutes_left"])
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
                        )
                        # Also log paper signal for PM 5m market (parallel data collection)
                        div_fade_5m.maybe_log_5m_signal(
                            asset=km["asset"],
                            kalshi_strike=kalshi_strike,
                            cl_now=_cl,
                            minutes_left_15m=_min_left,
                        )
                    continue
                # Guard: skip if normal arb already filled this candle.
                # Prevents double-entry when arb fills first, then divergence
                # collapses on the same candle minutes later.
                if _is_ticker_already_open(km.get("ticker", "")):
                    log.debug(
                        "[DIV_COLLAPSE] Skipping %s — already in open_arbs",
                        km.get("ticker", "?"),
                    )
                    continue

                # Collapse detected — fall through to normal arb logic below,
                # but windows will be tagged with entry_mode + stake_override_usd.
            else:
                _is_collapse = False

            # ── Dynamic candle movement check ─────────────────────────────
            # Snapshot CL price at candle open to separate two signals:
            #   divergence = abs(CL_open - CF_open)  → oracle disagreement
            #   candle_move = abs(CL_now - CL_open)  → actual price movement
            #   min_move = max(divergence + margin, floor)
            # Tight oracles ($2 apart) + $10 margin → need $12 BTC move.
            # Wide oracles ($30 apart) + $10 margin → need $40 BTC move.
            #
            # SKIPPED for div_collapse entries: the collapse itself (oracle gap
            # shrinking ≥35% in 60s) is the signal — CL_at_open was cached when
            # divergence was large, so min_move ends up artificially high relative
            # to the now-collapsed gap. Pre-flight price check + Kalshi depth gate
            # are the correct downstream guards for collapse entries.
            _margin = CANDLE_MOVE_MARGIN.get(km["asset"])
            if _margin is not None and kalshi_strike > 0 and not _is_collapse:
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
            elif _is_collapse:
                log.info(
                    "Candle flat check SKIPPED (div_collapse mode) — %s",
                    km["asset"])

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
                }

                if _is_collapse:
                    _window["entry_mode"]       = "div_collapse"
                    _window["stake_override_usd"] = DIV_COLLAPSE_STAKE_USD
                    # Alert once per candle when a live collapse window is found
                    _alert_key = f"{km['asset']}:{km['candle_end_ts']}"
                    if _alert_key not in _collapse_alerted:
                        _collapse_alerted.add(_alert_key)
                        try:
                            import notifier as _ntf_col
                            _ntf_col._send(
                                f"⚡ <b>DIV COLLAPSE WINDOW</b> — {km['asset']}\n"
                                f"Oracle gap shrinking ≥{int(DIV_COLLAPSE_THRESHOLD*100)}% "
                                f"in {DIV_COLLAPSE_WINDOW_SECS}s\n"
                                f"Arb found: PM {pm_side} @ {pm_price:.0f}¢ + "
                                f"Kal {kal_side} @ {kal_price:.0f}¢ = {combined:.0f}¢\n"
                                f"Stake: <b>${DIV_COLLAPSE_STAKE_USD:.0f}/leg</b> "
                                f"(vs normal ${150:.0f})\n"
                                f"{min(km['minutes_left'], pm['minutes_left']):.1f}m left"
                            )
                        except Exception as _ntf_e:
                            log.warning("Collapse alert failed: %s", _ntf_e)

                windows.append(_window)

    windows.sort(key=lambda x: x["profit_cents"], reverse=True)
    if windows:
        log.info("Arb windows: %d found (best: %s %s %.1f¢ profit)",
                 len(windows), windows[0]["asset"], windows[0]["direction"],
                 windows[0]["profit_cents"])
    return windows
