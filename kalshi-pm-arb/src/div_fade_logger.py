"""div_fade_logger.py — Signal logger + live executor for the Divergence Fade strategy.

Strategy (Option 1 — Structural Convergence):
  Chainlink updates on a price-deviation threshold, not continuously.
  So when CF Benchmarks (Kalshi strike) leads Chainlink by a large amount,
  it means Chainlink *hasn't caught up yet* — not that price reversed.

  Direction:
    CF > CL  →  Chainlink lags behind, will move UP  →  PM UP signal
    CL > CF  →  Chainlink leads CF, will fall back   →  PM DN signal

  This is a structural convergence bet, not a random directional trade.
  We're betting that the oracle gap closes within the 15-minute candle window.

Logging:
  - One signal per asset per candle (deduplicated on candle_end_ts)
  - Logged the FIRST time a candle is blocked for divergence
  - Orderbook depth snapshot taken at signal time (bid/ask depth at signal price)
  - outcome field stays null until filled by a backfill script

Live Trading:
  - DIV_FADE_LIVE_ASSETS controls which assets go live (ETH) vs dry-run only (BTC)
  - Live trades written to logs/div_fade_positions.jsonl
  - Winning tokens redeemed by the existing redeemer.py

Output: logs/div_fade_signals.jsonl (signals), logs/div_fade_positions.jsonl (live trades)
"""
import json
import logging
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger("div_fade")

_SIGNALS_LOG   = Path(__file__).parent.parent / "logs" / "div_fade_signals.jsonl"
_POSITIONS_LOG = Path(__file__).parent.parent / "logs" / "div_fade_positions.jsonl"
_CLOB_BOOK_URL = "https://clob.polymarket.com/book"

# Deduplicate: only log/trade the first block per asset+candle.
# Key: "ASSET:candle_end_ts" — prevents spamming the same signal every 3s poll.
_logged_candles: set[str] = set()
_lock = threading.Lock()

# Pre-populate dedup set from existing positions file so restarts don't re-enter
# candles that were already traded in the same 15-minute window.
def _load_traded_candles() -> set[str]:
    keys: set[str] = set()
    try:
        if _POSITIONS_LOG.exists():
            with _POSITIONS_LOG.open() as f:
                for line in f:
                    rec = json.loads(line.strip())
                    keys.add(f"{rec['asset']}:{rec['candle_end_ts']}")
    except Exception:
        pass
    return keys

_logged_candles = _load_traded_candles()

# Stake size for simulated trades (mirrors LIVE_STAKE_USD in config)
_SIM_STAKE_USD = 150.0

# How far above signal price we'll accept asks (slippage tolerance)
_OB_PRICE_TOLERANCE = 0.15   # 15% above signal price


def _should_trade_live(asset: str, signal: str) -> bool:
    """Return True if this (asset, signal) combo is configured for live 15m trading."""
    try:
        from config import DIV_FADE_ENABLED, DIV_FADE_LIVE_SIGNALS
        key = f"{asset}_15m_{signal}"
        return DIV_FADE_ENABLED and DIV_FADE_LIVE_SIGNALS.get(key, False)
    except Exception:
        return False


def _fetch_ob_depth(token_id: str, signal_price_cents: float) -> dict:
    """Query the PM CLOB orderbook for a token and calculate fillable depth.

    We're buying, so we look at ASKS. Accumulates available shares at prices
    ≤ signal_price_cents * (1 + _OB_PRICE_TOLERANCE).

    Returns dict with:
      ob_ask_levels       — number of ask price levels within tolerance
      ob_depth_shares     — total shares available within tolerance
      ob_avg_fill_cents   — share-weighted average ask price within tolerance
      ob_fillable_usd     — USD cost to buy all available shares within tolerance
      realistic_stake_usd — min(SIM_STAKE_USD, ob_fillable_usd)
      realistic_profit_usd — profit if realistic_stake fills and wins
      ob_error            — error string if fetch failed (else None)
    """
    empty = {
        "ob_ask_levels":        None,
        "ob_depth_shares":      None,
        "ob_avg_fill_cents":    None,
        "ob_fillable_usd":      None,
        "realistic_stake_usd":  None,
        "realistic_profit_usd": None,
        "ob_error":             None,
    }
    if not token_id:
        empty["ob_error"] = "no_token_id"
        return empty

    max_price_cents = signal_price_cents * (1 + _OB_PRICE_TOLERANCE)

    try:
        r = requests.get(_CLOB_BOOK_URL, params={"token_id": token_id}, timeout=3)
        if not r.ok:
            empty["ob_error"] = f"http_{r.status_code}"
            return empty

        book = r.json()
        asks = book.get("asks", [])  # list of {"price": "0.085", "size": "500.0"}

        # Filter asks within tolerance and accumulate
        total_shares = 0.0
        total_cost   = 0.0
        levels        = 0

        for level in sorted(asks, key=lambda x: float(x.get("price", 999))):
            ask_price_cents = float(level.get("price", 1)) * 100
            ask_size        = float(level.get("size", 0))

            if ask_price_cents > max_price_cents:
                break  # beyond tolerance — stop

            total_shares += ask_size
            total_cost   += ask_size * ask_price_cents / 100   # USD cost
            levels        += 1

        if total_shares == 0:
            return {
                "ob_ask_levels":        0,
                "ob_depth_shares":      0.0,
                "ob_avg_fill_cents":    None,
                "ob_fillable_usd":      0.0,
                "realistic_stake_usd":  0.0,
                "realistic_profit_usd": 0.0,
                "ob_error":             None,
            }

        avg_fill_cents   = round((total_cost / total_shares) * 100, 2)
        fillable_usd     = round(total_cost, 2)
        realistic_stake  = round(min(_SIM_STAKE_USD, fillable_usd), 2)

        # Realistic profit: if we fill realistic_stake at avg_fill_cents
        # shares bought = realistic_stake / (avg_fill_cents/100)
        # payout per share = $1.00 (if wins)
        # profit = shares * (1 - avg_fill_cents/100)
        real_shares      = realistic_stake / (avg_fill_cents / 100)
        realistic_profit = round(real_shares * (1 - avg_fill_cents / 100), 2)

        return {
            "ob_ask_levels":        levels,
            "ob_depth_shares":      round(total_shares, 1),
            "ob_avg_fill_cents":    avg_fill_cents,
            "ob_fillable_usd":      fillable_usd,
            "realistic_stake_usd":  realistic_stake,
            "realistic_profit_usd": realistic_profit,
            "ob_error":             None,
        }

    except Exception as exc:
        empty["ob_error"] = str(exc)[:80]
        return empty


def lookup_div_fade_position(token_id: str) -> dict | None:
    """Return the div_fade_positions.jsonl entry matching token_id, or None."""
    pos_log = Path(__file__).parent.parent / "logs" / "div_fade_positions.jsonl"
    if not pos_log.exists():
        return None
    try:
        with pos_log.open() as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if entry.get("token_id") == token_id:
                        return entry
                except Exception:
                    pass
    except Exception:
        pass
    return None


def update_div_fade_outcome(token_id: str, outcome: str, profit_usd: float) -> None:
    """Rewrite div_fade_positions.jsonl, updating outcome + profit_usd for token_id."""
    pos_log = Path(__file__).parent.parent / "logs" / "div_fade_positions.jsonl"
    if not pos_log.exists():
        return
    try:
        lines = pos_log.read_text().splitlines()
        updated = []
        for line in lines:
            try:
                entry = json.loads(line)
                if entry.get("token_id") == token_id:
                    entry["outcome"] = outcome
                    entry["profit_usd"] = round(profit_usd, 4)
                    line = json.dumps(entry)
            except Exception:
                pass
            updated.append(line)
        pos_log.write_text("\n".join(updated) + "\n")
    except Exception as e:
        log.warning("[DIV_FADE] Failed to update outcome for token %s: %s", token_id[:16], e)


def _execute_live_fade(
    token_id: str,
    signal: str,
    signal_price_cents: float,
    asset: str,
    candle_end_ts: int,
    kal_ticker: str,
    divergence: float,
    oracle_velocity: float | None = None,
    spot_obi: float | None = None,
) -> None:
    """Execute a live Polymarket FAK buy for the Divergence Fade strategy.

    Fires at most once per asset+candle (enforced by caller via _logged_candles).
    Logs result to div_fade_positions.jsonl. Winning tokens redeemed by redeemer.py.
    """
    try:
        from config import (
            PM_PRIVATE_KEY, PM_API_KEY, PM_API_SECRET, PM_API_PASSPHRASE,
            PM_FUNDER, PM_CLOB_URL, DIV_FADE_STAKE_USD, DIV_FADE_MAX_PRICE_CENTS,
            DIV_FADE_MIN_PRICE_CENTS,
        )
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderType

        # ── Live price check — bail if PM already repriced ─────────────────
        r = requests.get(f"{PM_CLOB_URL}/midpoint?token_id={token_id}", timeout=4)
        live_cents = float(r.json().get("mid", 0)) * 100
        if live_cents <= 0:
            log.warning("[DIV_FADE] SKIP live %s %s — price check failed", asset, signal)
            return
        if live_cents > DIV_FADE_MAX_PRICE_CENTS:
            log.warning(
                "[DIV_FADE] SKIP live %s %s — PM already repriced %.1f¢ > max %.1f¢",
                asset, signal, live_cents, DIV_FADE_MAX_PRICE_CENTS,
            )
            return
        if live_cents < DIV_FADE_MIN_PRICE_CENTS:
            log.warning(
                "[DIV_FADE] SKIP live %s %s — PM price %.1f¢ < min %.1f¢",
                asset, signal, live_cents, DIV_FADE_MIN_PRICE_CENTS,
            )
            return

        # ── Order book depth check — scale stake to what the book can fill ─
        # Uses live price (not signal price) for the most accurate snapshot.
        ob = _fetch_ob_depth(token_id, live_cents)
        ob_fillable = ob.get("ob_fillable_usd") or 0.0
        MIN_FADE_STAKE = 10.0   # not worth trading below $10

        # ── Stake cap (upstream pm_price gate handles bad zones) ─────────────
        # 50-57¢ zone now blocked upstream by DIV_FADE_MIN_SIGNAL_PRICE — no
        # per-zone stake reduction needed. Full $100 stake on all live entries.
        effective_stake = min(DIV_FADE_STAKE_USD, ob_fillable)
        if effective_stake < MIN_FADE_STAKE:
            log.warning(
                "[DIV_FADE] SKIP live %s %s — book too thin ($%.2f fillable, need ≥$%.0f)",
                asset, signal, ob_fillable, MIN_FADE_STAKE,
            )
            return
        if effective_stake < DIV_FADE_STAKE_USD:
            log.info(
                "[DIV_FADE] Scaling stake $%.0f → $%.2f (book only supports $%.2f within 15%% of %.1f¢)",
                DIV_FADE_STAKE_USD, effective_stake, ob_fillable, live_cents,
            )

        # ── Execute FAK buy (with 1 retry on transient API error) ───────────
        client = ClobClient(
            host=PM_CLOB_URL,
            key=PM_PRIVATE_KEY,
            chain_id=137,
            creds=ApiCreds(
                api_key=PM_API_KEY,
                api_secret=PM_API_SECRET,
                api_passphrase=PM_API_PASSPHRASE,
            ),
            signature_type=0,
            funder=PM_FUNDER,
        )

        result = None
        for attempt in range(1, 3):   # 2 attempts max
            secs_left = candle_end_ts - int(time.time())
            if secs_left < 30:
                log.warning("[DIV_FADE] ABORT retry %s %s — only %ds left in candle", asset, signal, secs_left)
                return
            try:
                order_args = MarketOrderArgs(token_id=token_id, amount=effective_stake, side="BUY")
                signed = client.create_market_order(order_args)
                result = client.post_order(signed, OrderType.FAK)
                break   # success — exit retry loop
            except Exception as api_exc:
                if attempt < 2:
                    log.warning("[DIV_FADE] API error attempt %d/2 (%s) — retrying in 3s", attempt, str(api_exc)[:80])
                    time.sleep(3)
                else:
                    raise   # re-raise on second failure so outer handler logs it

        if result is None or not result.get("success", True) or result.get("status") == "failed":
            log.warning("[DIV_FADE] Live FAK failed: %s", result)
            return

        shares = float(result.get("takingAmount", 0))
        cost   = float(result.get("makingAmount", DIV_FADE_STAKE_USD))
        if shares == 0:
            log.warning("[DIV_FADE] Live FAK returned 0 shares — no fill")
            return

        fill_price_cents = cost / shares * 100

        # ── Log position ────────────────────────────────────────────────────
        position = {
            "ts":                  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "asset":               asset,
            "candle_end_ts":       candle_end_ts,
            "kal_ticker":          kal_ticker,
            "signal":              signal,
            "token_id":            token_id,
            "shares":              round(shares, 4),
            "cost_usd":            round(cost, 4),
            "fill_price_cents":    round(fill_price_cents, 2),
            "signal_price_cents":  round(signal_price_cents, 2),
            "live_price_cents":    round(live_cents, 2),
            "target_stake_usd":    DIV_FADE_STAKE_USD,
            "effective_stake_usd": round(effective_stake, 2),
            "ob_fillable_usd":     round(ob_fillable, 2),
            "divergence":          round(divergence, 2),
            "oracle_velocity":     oracle_velocity,   # $/s: + widening, - narrowing
            "spot_obi":            spot_obi,          # -1 (sellers) → +1 (buyers)
            "outcome":             None,   # filled by backfill cron
        }
        _POSITIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _POSITIONS_LOG.open("a") as f:
            f.write(json.dumps(position) + "\n")

        log.info(
            "[DIV_FADE] 🟢 LIVE FILL: %s %s | %.1f shares @ %.1f¢ ($%.2f) | oracle gap $%.2f",
            asset, signal, shares, fill_price_cents, cost, abs(divergence),
        )

        # ── Telegram alert ──────────────────────────────────────────────────
        try:
            import notifier as _ntf
            from datetime import datetime, timezone as _tz

            _stake_note = (
                f"${cost:.2f} of ${DIV_FADE_STAKE_USD:.0f} (book thin)"
                if effective_stake < DIV_FADE_STAKE_USD
                else f"${cost:.2f}"
            )
            # Candle reference: Kalshi ticker for 15m, close time for 5m
            if kal_ticker:
                _tf_label   = "15m"
                _candle_ref = f"<code>{kal_ticker}</code>"
            else:
                _tf_label   = "5m"
                _close_t    = datetime.fromtimestamp(candle_end_ts, tz=_tz.utc).strftime("%H:%M UTC")
                _candle_ref = f"closes {_close_t}"

            # Breakeven: need PM token to expire > fill price
            _be = round(fill_price_cents, 1)

            _ntf._send(
                f"🌊 <b>DIV FADE ENTRY</b> — {asset} {_tf_label} {signal}\n"
                f"Bought <b>{shares:.0f} shares @ {fill_price_cents:.1f}¢</b>  ({_stake_note})\n"
                f"Oracle gap: <b>${abs(divergence):.0f}</b>  |  "
                f"signal {signal_price_cents:.0f}¢ → live {live_cents:.0f}¢\n"
                f"Breakeven: price settles &gt;{_be:.0f}¢\n"
                f"Candle: {_candle_ref}"
            )
        except Exception as _te:
            log.debug("[DIV_FADE] Telegram alert failed: %s", _te)

    except Exception as exc:
        log.error("[DIV_FADE] Live trade exception: %s", exc, exc_info=True)


def maybe_log_fade_signal(
    asset: str,
    kalshi_strike: float,
    cl_now: float,
    minutes_left: float,
    candle_end_ts: int,
    pm_up_price: float | None,
    pm_dn_price: float | None,
    kal_ticker: str = "",
    pm_up_token_id: str = "",
    pm_dn_token_id: str = "",
    oracle_velocity: float | None = None,
    spot_obi: float | None = None,
) -> None:
    """Called by matcher when oracle divergence blocks a normal arb.

    Logs a dry-run PM directional signal based on which oracle is leading.
    Deduplicates: only the first block per asset+candle is logged.
    Snapshots PM CLOB orderbook depth at signal time to assess real fillability.

    Args:
        asset:            "BTC" or "ETH"
        kalshi_strike:    CF Benchmarks candle-open price (the fixed Kalshi strike)
        cl_now:           Current Chainlink price
        minutes_left:     Minutes remaining in the candle
        candle_end_ts:    Unix timestamp of candle close (dedup key)
        pm_up_price:      PM UP token mid price in cents (None if unavailable)
        pm_dn_price:      PM DN token mid price in cents (None if unavailable)
        kal_ticker:       Kalshi market ticker (used by backfill to check settlement)
        pm_up_token_id:   PM UP token ID (for orderbook lookup)
        pm_dn_token_id:   PM DN token ID (for orderbook lookup)
    """
    candle_key = f"{asset}:{candle_end_ts}"

    with _lock:
        if candle_key in _logged_candles:
            return  # already logged this candle — skip
        _logged_candles.add(candle_key)

    divergence = kalshi_strike - cl_now  # signed: + means CF leads (CL lagging up)

    # Direction: which oracle is leading?
    if divergence > 0:
        # CF above CL → CL needs to move UP → PM UP
        signal   = "PM_UP"
        pm_price = pm_up_price
        token_id = pm_up_token_id
    else:
        # CL above CF → CL needs to fall → PM DN
        signal   = "PM_DN"
        pm_price = pm_dn_price
        token_id = pm_dn_token_id

    if pm_price is None or pm_price <= 0:
        log.debug("[DIV_FADE] No PM price for %s %s — signal skipped", asset, signal)
        return

    from config import DIV_FADE_MIN_PRICE_CENTS
    if pm_price < DIV_FADE_MIN_PRICE_CENTS:
        log.debug("[DIV_FADE] Skip %s %s — price %.1f¢ < min %.1f¢", asset, signal, pm_price, DIV_FADE_MIN_PRICE_CENTS)
        return

    # Simulate P&L: buy $SIM_STAKE_USD of PM shares (assumes perfect fill)
    shares       = (_SIM_STAKE_USD * 100) / pm_price
    would_profit = round(shares * (100 - pm_price) / 100, 2)
    would_loss   = round(_SIM_STAKE_USD, 2)

    # Snapshot live orderbook depth — how much can we *actually* fill?
    ob = _fetch_ob_depth(token_id, pm_price)

    entry = {
        "ts":                  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "asset":               asset,
        "candle_start_ts":     candle_end_ts - 900,   # 15m candle start
        "candle_end_ts":       candle_end_ts,
        "kal_ticker":          kal_ticker,
        "kalshi_strike":       round(kalshi_strike, 2),
        "cl_now":              round(cl_now, 2),
        "divergence":          round(divergence, 2),
        "abs_divergence":      round(abs(divergence), 2),
        "signal":              signal,
        "pm_price_cents":      round(pm_price, 1),
        "pm_token_id":         token_id,
        "minutes_left":        round(minutes_left, 1),
        # ── Sim (assumes perfect fill at signal price) ────────────────────
        "would_stake_usd":     _SIM_STAKE_USD,
        "would_profit_usd":    would_profit,
        "would_loss_usd":      would_loss,
        # ── Orderbook reality check ───────────────────────────────────────
        "ob_ask_levels":       ob["ob_ask_levels"],
        "ob_depth_shares":     ob["ob_depth_shares"],
        "ob_avg_fill_cents":   ob["ob_avg_fill_cents"],
        "ob_fillable_usd":     ob["ob_fillable_usd"],
        "realistic_stake_usd": ob["realistic_stake_usd"],
        "realistic_profit_usd":ob["realistic_profit_usd"],
        "ob_error":            ob["ob_error"],
        # ── Oracle velocity ($/s: + widening, - narrowing, None=no history) ──
        "oracle_velocity":     oracle_velocity,
        # ── Spot OBI: -1.0 (pure sellers) → 0 (balanced) → +1.0 (pure buyers) ──
        # High +OBI = buy pressure (adverse for PM_DN fades); collect for calibration.
        "spot_obi":            spot_obi,
        # ── Outcome (filled by backfill) ──────────────────────────────────
        "outcome":             None,
    }

    try:
        _SIGNALS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _SIGNALS_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")

        real_stake = ob["realistic_stake_usd"]
        ob_note = (
            f"ob_fill=${real_stake:.0f}/{_SIM_STAKE_USD:.0f} "
            f"({ob['ob_depth_shares']:.0f}sh@{ob['ob_avg_fill_cents']:.1f}¢)"
            if (real_stake is not None and ob.get("ob_avg_fill_cents") is not None)
            else f"ob_err={ob.get('ob_error') or 'empty_book'}"
        )
        log.info(
            "[DIV_FADE] 📋 %s %s %s | CF=$%.2f CL=$%.2f (div=%+.2f) "
            "| PM@%.1f¢ | sim +$%.2f/-$%.2f | %s | %.1fm left",
            "LIVE" if _should_trade_live(asset, signal) else "DRY-RUN",
            asset, signal,
            kalshi_strike, cl_now, divergence,
            pm_price, would_profit, would_loss,
            ob_note, minutes_left,
        )
    except Exception as exc:
        log.warning("[DIV_FADE] Failed to write signal: %s", exc)

    # ── Live execution — fires for any signal configured in DIV_FADE_LIVE_SIGNALS
    if _should_trade_live(asset, signal) and token_id:
        _execute_live_fade(
            token_id=token_id,
            signal=signal,
            signal_price_cents=pm_price,
            asset=asset,
            candle_end_ts=candle_end_ts,
            kal_ticker=kal_ticker,
            divergence=divergence,
        )
