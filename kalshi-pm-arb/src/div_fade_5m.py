"""div_fade_5m.py — Paper-trade signal logger for Divergence Fade on PM 5-minute markets.

Same oracle divergence signal as the 15m strategy (CF Benchmarks vs Chainlink), but
targets PM's btc/eth-updown-5m-{ts} series which refreshes every 5 minutes.

Paper-trade only — no live execution. Logs to div_fade_signals_5m.jsonl for
data collection and range analysis. The div_fade_monitor handles outcome tracking.

Signal logic:
  - CF Benchmarks (Kalshi) leads Chainlink (PM) by > threshold
  - We log a simulated buy of PM 5m UP/DN token
  - Monitor checks PM midpoint after candle close to determine win/loss

Dedup: one signal per asset per 5-minute candle (keyed by candle_start_ts).
Min time: skip if < 60s remaining in the current 5m candle (too late to be useful).
"""
import json
import logging
import threading
import time
from pathlib import Path

import requests

from config import (
    ORACLE_MAX_DIVERGENCE_USD,
    DIV_FADE_STAKE_USD,
    DIV_FADE_MIN_PRICE_CENTS,
    DIV_FADE_LIVE_SIGNALS,
    DIV_FADE_ENABLED,
    DIV_FADE_SKIP_DIV_RANGE,
    DIV_FADE_MIN_SIGNAL_PRICE,
)

log = logging.getLogger("div_fade_5m")

_SIGNALS_LOG = Path(__file__).parent.parent / "logs" / "div_fade_signals_5m.jsonl"
_SIM_STAKE_USD = DIV_FADE_STAKE_USD

_PM_GAMMA_URL  = "https://gamma-api.polymarket.com/markets"
_PM_MIDPOINT_URL = "https://clob.polymarket.com/midpoint"
_PM_OB_URL     = "https://clob.polymarket.com/book"

_CANDLE_SECS   = 300        # 5 minutes
_MIN_SECS_LEFT = 60         # skip if < 60s remaining in candle
_OB_TOLERANCE  = 0.15       # 15% price tolerance for OB depth check

def _should_trade_live(asset: str, signal: str) -> bool:
    """Return True if this (asset, signal) combo is configured for live 5m trading."""
    key = f"{asset}_5m_{signal}"
    return DIV_FADE_ENABLED and DIV_FADE_LIVE_SIGNALS.get(key, False)


# ── Deduplication ─────────────────────────────────────────────────────────────
_logged_candles: set[str] = set()
_lock = threading.Lock()


# ── PM 5m market lookup ───────────────────────────────────────────────────────

_market_cache: dict[str, dict] = {}   # slug → market dict (TTL = 5m candle)

def _candle_start_ts() -> int:
    """Unix timestamp of the currently-open 5m PM candle."""
    return (int(time.time()) // _CANDLE_SECS) * _CANDLE_SECS


def _get_pm_5m_market(asset: str) -> dict | None:
    """Fetch the currently-open PM 5m market for the given asset.

    Returns a dict with keys: up_token_id, dn_token_id, up_price_cents,
    dn_price_cents, slug, candle_start_ts, candle_end_ts.
    Caches for the duration of the 5m candle.
    """
    ts = _candle_start_ts()
    slug = f"{asset.lower()}-updown-5m-{ts}"

    if slug in _market_cache:
        return _market_cache[slug]

    try:
        r = requests.get(_PM_GAMMA_URL, params={"slug": slug}, timeout=5)
        if not r.ok or not r.json():
            log.debug("[DIV_FADE_5M] No PM 5m market found for %s (slug=%s)", asset, slug)
            return None

        m = r.json()[0]
        token_ids = json.loads(m.get("clobTokenIds", "[]"))
        if len(token_ids) < 2:
            return None

        # UP token is index 0, DN token is index 1 (standard PM binary)
        up_token_id = token_ids[0]
        dn_token_id = token_ids[1]

        # Get current midpoint prices
        up_price = _get_midpoint_cents(up_token_id)
        dn_price = _get_midpoint_cents(dn_token_id)

        result = {
            "slug":           slug,
            "candle_start_ts": ts,
            "candle_end_ts":  ts + _CANDLE_SECS,
            "up_token_id":    up_token_id,
            "dn_token_id":    dn_token_id,
            "up_price_cents": up_price,
            "dn_price_cents": dn_price,
            "liquidity":      float(m.get("liquidity") or 0),
        }
        _market_cache[slug] = result
        return result

    except Exception as e:
        log.debug("[DIV_FADE_5M] Market lookup failed for %s: %s", asset, e)
        return None


def _get_midpoint_cents(token_id: str) -> float | None:
    """Fetch PM CLOB midpoint price in cents."""
    try:
        r = requests.get(_PM_MIDPOINT_URL, params={"token_id": token_id}, timeout=4)
        if r.ok:
            mid = r.json().get("mid")
            if mid is not None:
                return round(float(mid) * 100, 2)
    except Exception:
        pass
    return None


# ── Orderbook depth ───────────────────────────────────────────────────────────

def _fetch_ob_depth(token_id: str, signal_price_cents: float) -> dict:
    """Snapshot PM orderbook ask depth within tolerance of signal price."""
    result = {
        "ob_ask_levels": None, "ob_depth_shares": None,
        "ob_avg_fill_cents": None, "ob_fillable_usd": None,
        "realistic_stake_usd": None, "realistic_profit_usd": None,
        "ob_error": None,
    }
    if not token_id:
        result["ob_error"] = "no_token_id"
        return result
    try:
        r = requests.get(_PM_OB_URL, params={"token_id": token_id}, timeout=5)
        if not r.ok:
            result["ob_error"] = f"http_{r.status_code}"
            return result

        book  = r.json()
        asks  = book.get("asks", [])
        price_ceil = signal_price_cents * (1 + _OB_TOLERANCE)

        total_shares = total_cost = 0.0
        levels = 0
        for level in sorted(asks, key=lambda x: float(x.get("price", 999))):
            ask_p = float(level.get("price", 999)) * 100   # to cents
            ask_s = float(level.get("size", 0))
            if ask_p > price_ceil:
                break
            total_shares += ask_s
            total_cost   += ask_s * ask_p / 100
            levels += 1

        if total_shares == 0:
            result["ob_error"] = "empty_book"
            return result

        avg_fill = (total_cost / total_shares) * 100
        fillable = min(total_cost, _SIM_STAKE_USD)
        shares_fillable = (fillable * 100) / avg_fill if avg_fill > 0 else 0
        profit = round(shares_fillable * (100 - avg_fill) / 100, 2)

        result.update({
            "ob_ask_levels":       levels,
            "ob_depth_shares":     round(total_shares, 2),
            "ob_avg_fill_cents":   round(avg_fill, 2),
            "ob_fillable_usd":     round(total_cost, 2),
            "realistic_stake_usd": round(fillable, 2),
            "realistic_profit_usd": profit,
        })
    except Exception as e:
        result["ob_error"] = str(e)[:60]
    return result


# ── Public API ────────────────────────────────────────────────────────────────

def maybe_log_5m_signal(
    asset: str,
    kalshi_strike: float,
    cl_now: float,
    minutes_left_15m: float,
    oracle_velocity: float | None = None,
    spot_obi: float | None = None,
) -> None:
    """Called by matcher alongside the 15m logger when oracle divergence fires.

    Finds the currently-open PM 5m market, checks timing, logs a paper signal.

    Args:
        asset:             "BTC" or "ETH"
        kalshi_strike:     CF Benchmarks price (from Kalshi floor_strike)
        cl_now:            Current Chainlink price
        minutes_left_15m:  Minutes left in the Kalshi 15m candle (for context only)
    """
    divergence = kalshi_strike - cl_now   # + = CF leads up, - = CF leads down

    # Threshold check (same as 15m)
    threshold = ORACLE_MAX_DIVERGENCE_USD.get(asset, 999)
    if abs(divergence) < threshold:
        return   # below threshold — not logged (matcher already checked, but safety net)

    # Find current 5m candle
    ts       = _candle_start_ts()
    secs_left = (ts + _CANDLE_SECS) - int(time.time())
    if secs_left < _MIN_SECS_LEFT:
        log.debug("[DIV_FADE_5M] Skip %s — only %ds left in 5m candle", asset, secs_left)
        return

    minutes_left_5m = round(secs_left / 60, 1)
    candle_key = f"{asset}:{ts}"

    with _lock:
        if candle_key in _logged_candles:
            return
        _logged_candles.add(candle_key)

    # Fetch PM 5m market
    market = _get_pm_5m_market(asset)
    if market is None:
        log.debug("[DIV_FADE_5M] No active 5m market for %s", asset)
        return

    # Direction
    if divergence > 0:
        signal   = "PM_UP"
        pm_price = market.get("up_price_cents")
        token_id = market.get("up_token_id", "")
    else:
        signal   = "PM_DN"
        pm_price = market.get("dn_price_cents")
        token_id = market.get("dn_token_id", "")

    if not pm_price or pm_price <= 0:
        log.debug("[DIV_FADE_5M] No PM price for %s %s", asset, signal)
        return

    if pm_price < DIV_FADE_MIN_PRICE_CENTS:
        log.debug("[DIV_FADE_5M] Skip %s %s — price %.1f¢ < min %.1f¢", asset, signal, pm_price, DIV_FADE_MIN_PRICE_CENTS)
        return

    # ── Divergence dead-band filter ────────────────────────────────────────────
    _skip_range = DIV_FADE_SKIP_DIV_RANGE.get(f"{asset}_5m_{signal}")
    if _skip_range:
        _lo, _hi = _skip_range
        if _lo <= abs(divergence) < _hi:
            log.debug(
                "[DIV_FADE_5M] Skip %s %s — div $%.0f in dead-band ($%.0f–$%.0f)",
                asset, signal, abs(divergence), _lo, _hi,
            )
            return

    # Sim P&L
    shares       = (_SIM_STAKE_USD * 100) / pm_price
    would_profit = round(shares * (100 - pm_price) / 100, 2)
    would_loss   = round(_SIM_STAKE_USD, 2)

    # OB depth
    ob = _fetch_ob_depth(token_id, pm_price)

    entry = {
        "ts":                   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ts_unix":              int(time.time()),
        "asset":                asset,
        "candle_start_ts":      ts,
        "candle_end_ts":        ts + _CANDLE_SECS,
        "candle_minutes_left":  minutes_left_5m,
        "kalshi_strike":        round(kalshi_strike, 2),
        "cl_now":               round(cl_now, 2),
        "divergence":           round(divergence, 2),
        "abs_divergence":       round(abs(divergence), 2),
        "signal":               signal,
        "pm_price_cents":       round(pm_price, 1),
        "pm_token_id":          token_id,
        "market_slug":          market.get("slug", ""),
        "market_liquidity_usd": market.get("liquidity", 0),
        "minutes_left_15m":     round(minutes_left_15m, 1),
        # ── Sim ──────────────────────────────────────────────────────────
        "would_stake_usd":      _SIM_STAKE_USD,
        "would_profit_usd":     would_profit,
        "would_loss_usd":       would_loss,
        # ── OB depth ─────────────────────────────────────────────────────
        "ob_ask_levels":        ob["ob_ask_levels"],
        "ob_depth_shares":      ob["ob_depth_shares"],
        "ob_avg_fill_cents":    ob["ob_avg_fill_cents"],
        "ob_fillable_usd":      ob["ob_fillable_usd"],
        "realistic_stake_usd":  ob["realistic_stake_usd"],
        "realistic_profit_usd": ob["realistic_profit_usd"],
        "ob_error":             ob["ob_error"],
        # ── Oracle velocity ($/s: + widening, - narrowing, None=no history) ──
        "oracle_velocity":      oracle_velocity,
        # ── Spot OBI: -1.0 (pure sellers) → 0 (balanced) → +1.0 (pure buyers) ──
        # High +OBI = buyers dominating Binance → adverse for PM_DN fades.
        # Collect for threshold calibration; conservative hard gate applied at live entry.
        "spot_obi":             spot_obi,
        # ── Outcome (filled by monitor) ───────────────────────────────────
        "outcome":              None,
    }

    try:
        _SIGNALS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _SIGNALS_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")

        mode = "LIVE" if _should_trade_live(asset, signal) else "PAPER"
        ob_note = (
            f"ob=${ob['ob_fillable_usd']:.0f}/{_SIM_STAKE_USD:.0f} ({ob['ob_depth_shares']:.0f}sh)"
            if ob["ob_fillable_usd"] is not None
            else f"ob_err={ob['ob_error']}"
        )
        log.info(
            "[DIV_FADE_5M] 📋 %s %s %s | CF=$%.2f CL=$%.2f (div=%+.2f)"
            " | PM@%.1f¢ | sim +$%.2f/-$%.2f | %s | %.1fm left (5m candle)",
            mode, asset, signal,
            kalshi_strike, cl_now, divergence,
            pm_price, would_profit, would_loss,
            ob_note, minutes_left_5m,
        )
    except Exception as exc:
        log.warning("[DIV_FADE_5M] Failed to write signal: %s", exc)
        return

    # ── Live execution (when signal configured live in DIV_FADE_LIVE_SIGNALS) ─
    if _should_trade_live(asset, signal) and token_id:
        # ── Market-confirmation gate: pm_price must confirm direction ──────────
        # Analysis (94 signals): divergence magnitude doesn't predict outcome.
        # pm_price does: <57¢ → 44-56% WR; ≥57¢ → 81% WR.
        # At 50¢, oracle says DN but market disagrees → adverse selection kills edge.
        _min_signal_price = DIV_FADE_MIN_SIGNAL_PRICE.get(f"{asset}_5m_{signal}", 0.0)
        if _min_signal_price and pm_price < _min_signal_price:
            log.info(
                "[DIV_FADE_5M] ⛔ Skip live %s %s — pm_price %.1f¢ < min %.1f¢ "
                "(market not confirming direction — adverse selection zone)",
                asset, signal, pm_price, _min_signal_price,
            )
        else:
            try:
                from div_fade_logger import _execute_live_fade
                _execute_live_fade(
                    token_id=token_id,
                    signal=signal,
                    signal_price_cents=pm_price,
                    asset=asset,
                    candle_end_ts=ts + _CANDLE_SECS,
                    kal_ticker="",   # no Kalshi ticker for 5m signals
                    divergence=divergence,
                )
            except Exception as exc:
                log.error("[DIV_FADE_5M] Live execution failed: %s", exc)
