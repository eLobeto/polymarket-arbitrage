"""div_fade_executor.py — Standalone execution daemon for the Divergence Fade strategy.

Reads div_fade_signals_5m.jsonl produced by the main arb scanner (div_fade_5m.py).
Applies execution-time gates and places orders independently from the arb loop.

Improvements over inline FAK execution:
  1. Entry delay (ENTRY_DELAY_SECS) — waits 75s into candle before entering.
     Research: best fade entries are 1-2 min after the initial spike, not at the
     moment of detection. Avoids chasing the overshoot top/bottom.
  2. OBI hard block (OBI_BLOCK_THRESHOLD) — re-fetches order book imbalance at
     execution time. If one side strongly dominates, it's informed flow — don't fade.
  3. Maker-first orders — GTC limit buy near best bid instead of FAK market buy.
     Captures spread (maker earns ~1-2¢ vs taker paying ~1-2¢) per the Kalshi
     microstructure finding (takers lose at 80/99 price levels).
  4. Independent poll rate (15s vs arb loop's 3s) — div fade doesn't need 3s.
  5. Persistent dedup (JSON state file) — survives restarts; won't re-enter a candle.
  6. Fault isolation — arb bot and div fade can't block each other.

State file: logs/div_fade_executor_state.json
Positions:  logs/div_fade_positions.jsonl (shared with inline executor)
Signals:    logs/div_fade_signals_5m.jsonl (read-only; written by matcher.py)
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Paths ─────────────────────────────────────────────────────────────────────
_SRC = Path(__file__).parent
_PROJECT = _SRC.parent
SIGNALS_PATH   = _PROJECT / "logs" / "div_fade_signals_5m.jsonl"
POSITIONS_PATH = _PROJECT / "logs" / "div_fade_positions.jsonl"
STATE_PATH     = _PROJECT / "logs" / "div_fade_executor_state.json"
LOG_PATH       = _PROJECT / "logs" / "div_fade_executor.log"
PID_PATH       = _PROJECT / "logs" / "div_fade_executor.pid"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [div_fade_exec] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("div_fade_executor")


# ── Config ─────────────────────────────────────────────────────────────────────

def _cfg():
    """Hot-reload config on every use."""
    from config import (
        DIV_FADE_STAKE_USD,
        DIV_FADE_MAX_PRICE_CENTS,
        DIV_FADE_MIN_PRICE_CENTS,
        DIV_FADE_MIN_SIGNAL_PRICE,
        DIV_FADE_LIVE_SIGNALS,
        DIV_FADE_ENTRY_DELAY_SECS,
        DIV_FADE_OBI_BLOCK_THRESHOLD,
        DIV_FADE_MAKER_TIMEOUT_SECS,
        DIV_FADE_MAKER_FALLBACK_FAK,
        PM_CLOB_URL,
    )
    return {
        "stake":             DIV_FADE_STAKE_USD,
        "max_price":         DIV_FADE_MAX_PRICE_CENTS,
        "min_price":         DIV_FADE_MIN_PRICE_CENTS,
        "min_signal_price":  DIV_FADE_MIN_SIGNAL_PRICE,
        "live_signals":      DIV_FADE_LIVE_SIGNALS,
        "entry_delay":       DIV_FADE_ENTRY_DELAY_SECS,
        "obi_block":         DIV_FADE_OBI_BLOCK_THRESHOLD,
        "maker_timeout":     DIV_FADE_MAKER_TIMEOUT_SECS,
        "maker_fallback":    DIV_FADE_MAKER_FALLBACK_FAK,
        "clob_url":          PM_CLOB_URL,
    }


def _pm_client(cfg: dict):
    from config import (
        PM_PRIVATE_KEY, PM_API_KEY, PM_API_SECRET,
        PM_API_PASSPHRASE, PM_FUNDER,
    )
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    return ClobClient(
        host=cfg["clob_url"],
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


# ── State persistence ─────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"executed_candles": {}}   # key: f"{asset}_{candle_end_ts}"


def _save_state(state: dict):
    # Prune keys older than 24h to keep file small
    cutoff = int(time.time()) - 86400
    state["executed_candles"] = {
        k: v for k, v in state["executed_candles"].items()
        if v.get("ts_unix", 0) > cutoff
    }
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ── Signal reader ─────────────────────────────────────────────────────────────

def _load_signals() -> list[dict]:
    if not SIGNALS_PATH.exists():
        return []
    signals = []
    with SIGNALS_PATH.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    signals.append(json.loads(line))
                except Exception:
                    pass
    return signals


# ── PM live price + OBI ───────────────────────────────────────────────────────

def _fetch_live_price(token_id: str, clob_url: str) -> float | None:
    """Return live PM midpoint in cents, or None on failure."""
    try:
        r = requests.get(
            f"{clob_url}/midpoint",
            params={"token_id": token_id},
            timeout=4,
        )
        if r.ok:
            mid = float(r.json().get("mid", 0))
            return mid * 100 if mid > 0 else None
    except Exception:
        pass
    return None


def _fetch_obi(token_id: str, clob_url: str) -> float | None:
    """
    Return order book imbalance ρ = (bid_depth - ask_depth) / (bid_depth + ask_depth).
    Positive = buy pressure, Negative = sell pressure.
    None on failure.
    """
    try:
        r = requests.get(
            f"{clob_url}/book",
            params={"token_id": token_id},
            timeout=4,
        )
        if not r.ok:
            return None
        book = r.json()
        bids = sum(float(lvl.get("size", 0)) for lvl in (book.get("bids") or [])[:10])
        asks = sum(float(lvl.get("size", 0)) for lvl in (book.get("asks") or [])[:10])
        total = bids + asks
        return round((bids - asks) / total, 4) if total > 0 else None
    except Exception:
        return None


def _fetch_ob_depth(token_id: str, live_cents: float, clob_url: str) -> float:
    """Estimate fillable USD within 15% of live_cents on the ask side."""
    try:
        r = requests.get(
            f"{clob_url}/book",
            params={"token_id": token_id},
            timeout=4,
        )
        if not r.ok:
            return 0.0
        asks = r.json().get("asks") or []
        ceil_price = (live_cents * 1.15) / 100
        fillable = 0.0
        for lvl in asks:
            p = float(lvl.get("price", 1))
            s = float(lvl.get("size", 0))
            if p <= ceil_price:
                fillable += p * s
        return round(fillable, 2)
    except Exception:
        return 0.0


# ── Order execution ───────────────────────────────────────────────────────────

def _place_maker_order(
    token_id: str,
    effective_stake: float,
    live_cents: float,
    cfg: dict,
) -> dict | None:
    """
    Place a GTC limit buy near the best bid.
    Waits MAKER_TIMEOUT_SECS for fill, then cancels.
    Returns fill dict {shares, cost_usd, fill_price_cents, order_type} or None.
    """
    from py_clob_client.clob_types import LimitOrderArgs, OrderType

    # Bid 1 cent below mid — maker position, captures spread
    limit_price = max(0.01, (live_cents - 1.0) / 100)
    shares_wanted = effective_stake / limit_price

    try:
        client = _pm_client(cfg)
        order_args = LimitOrderArgs(
            token_id=token_id,
            price=round(limit_price, 4),
            size=round(shares_wanted, 2),
            side="BUY",
        )
        signed = client.create_order(order_args)
        resp = client.post_order(signed, OrderType.GTC)

        order_id = resp.get("orderID") or resp.get("id")
        if not order_id:
            log.warning("[EXEC] GTC post returned no order_id: %s", resp)
            return None

        log.info(
            "[EXEC] GTC limit placed: %.1f¢ × %.1f shares = $%.2f | order_id=%s",
            limit_price * 100, shares_wanted, effective_stake, order_id[:12],
        )

        # ── Poll for fill ─────────────────────────────────────────────────
        deadline = time.time() + cfg["maker_timeout"]
        while time.time() < deadline:
            time.sleep(3)
            try:
                order_status = client.get_order(order_id)
                status = (order_status.get("status") or "").upper()
                size_matched = float(order_status.get("size_matched") or 0)

                if status == "MATCHED" and size_matched > 0:
                    avg_price = float(order_status.get("average_price") or limit_price)
                    cost = avg_price * size_matched
                    log.info(
                        "[EXEC] GTC FILLED: %.1f shares @ %.1f¢ = $%.2f",
                        size_matched, avg_price * 100, cost,
                    )
                    return {
                        "shares": round(size_matched, 4),
                        "cost_usd": round(cost, 4),
                        "fill_price_cents": round(avg_price * 100, 2),
                        "order_type": "maker_gtc",
                    }
                elif status in ("CANCELLED", "CANCELED", "EXPIRED"):
                    log.info("[EXEC] GTC order %s — no fill", status)
                    return None
            except Exception as poll_e:
                log.debug("[EXEC] Poll error: %s", poll_e)

        # ── Timeout — cancel the order ────────────────────────────────────
        log.info("[EXEC] GTC timeout (%ds) — cancelling %s", cfg["maker_timeout"], order_id[:12])
        try:
            client.cancel(order_id)
        except Exception as cancel_e:
            log.warning("[EXEC] Cancel failed: %s", cancel_e)

        return None

    except ImportError:
        log.warning("[EXEC] LimitOrderArgs not available in installed py_clob_client")
        return None
    except Exception as e:
        log.warning("[EXEC] Maker order error: %s", e)
        return None


def _place_fak_order(
    token_id: str,
    effective_stake: float,
    cfg: dict,
) -> dict | None:
    """FAK market buy — fallback when maker order times out or unavailable."""
    from py_clob_client.clob_types import MarketOrderArgs, OrderType

    try:
        client = _pm_client(cfg)
        order_args = MarketOrderArgs(token_id=token_id, amount=effective_stake, side="BUY")
        signed = client.create_market_order(order_args)
        result = client.post_order(signed, OrderType.FAK)

        if not result.get("success", True) or result.get("status") == "failed":
            log.warning("[EXEC] FAK failed: %s", result)
            return None

        shares = float(result.get("takingAmount", 0))
        cost   = float(result.get("makingAmount", effective_stake))
        if shares == 0:
            log.warning("[EXEC] FAK returned 0 shares")
            return None

        return {
            "shares": round(shares, 4),
            "cost_usd": round(cost, 4),
            "fill_price_cents": round((cost / shares) * 100, 2),
            "order_type": "taker_fak",
        }
    except Exception as e:
        log.warning("[EXEC] FAK error: %s", e)
        return None


# ── Position writer ───────────────────────────────────────────────────────────

def _write_position(signal: dict, fill: dict, live_cents: float):
    pos = {
        "ts":                datetime.now(timezone.utc).isoformat(),
        "asset":             signal["asset"],
        "candle_end_ts":     signal["candle_end_ts"],
        "kal_ticker":        signal.get("kal_ticker", ""),
        "signal":            signal.get("signal", ""),
        "token_id":          signal["token_id"],
        "shares":            fill["shares"],
        "cost_usd":          fill["cost_usd"],
        "fill_price_cents":  fill["fill_price_cents"],
        "signal_price_cents": signal.get("pm_price_cents", 0),
        "live_price_cents":  live_cents,
        "order_type":        fill["order_type"],
        "target_stake_usd":  fill["cost_usd"],
        "effective_stake_usd": fill["cost_usd"],
        "divergence":        signal.get("divergence", 0),
        "oracle_velocity":   signal.get("oracle_velocity"),
        "spot_obi":          signal.get("spot_obi"),
        "entry_delay_secs":  int(time.time()) - int(signal.get("ts_unix", time.time())),
        "outcome":           None,
        "profit_usd":        None,
    }
    with POSITIONS_PATH.open("a") as f:
        f.write(json.dumps(pos) + "\n")

    log.info(
        "✅ [DIV FADE] %s %s | fill=%.1f¢ stake=$%.2f shares=%.1f | %s",
        signal["asset"], signal.get("signal",""),
        fill["fill_price_cents"], fill["cost_usd"], fill["shares"],
        fill["order_type"],
    )


# ── Notifier ──────────────────────────────────────────────────────────────────

def _notify(msg: str):
    try:
        from notifier import send_telegram
        send_telegram(msg)
    except Exception:
        pass


# ── Main loop ─────────────────────────────────────────────────────────────────

def run():
    log.info("div_fade_executor started (maker-first, entry-delay, OBI-gated)")
    state = _load_state()

    while True:
        try:
            cfg = _cfg()
            now = int(time.time())
            signals = _load_signals()

            for sig in signals:
                asset   = sig.get("asset", "")
                signal  = sig.get("signal", "")
                key     = f"{asset}_5m_{signal}"

                # ── Gate 0: Only live signals ─────────────────────────────
                if not cfg["live_signals"].get(key, False):
                    continue

                # ── Gate 1: Minimum signal PM price ───────────────────────
                sig_price = float(sig.get("pm_price_cents") or 0)
                min_price = cfg["min_signal_price"].get(key, 0.0)
                if sig_price < min_price:
                    continue

                candle_end_ts = int(sig.get("candle_end_ts", 0))
                sig_ts_unix   = int(sig.get("ts_unix", 0))
                dedup_key     = f"{asset}_{candle_end_ts}"

                # ── Gate 2: Already executed this candle ──────────────────
                if dedup_key in state["executed_candles"]:
                    continue

                # ── Gate 3: Candle already closed ─────────────────────────
                secs_left = candle_end_ts - now
                if secs_left <= 0:
                    continue

                # ── Gate 4: Need enough candle left after delay ───────────
                secs_since_signal = now - sig_ts_unix
                if secs_since_signal < cfg["entry_delay"]:
                    wait = cfg["entry_delay"] - secs_since_signal
                    log.debug(
                        "[GATE] %s %s — waiting %ds before entry (delay gate)",
                        asset, signal, wait,
                    )
                    continue

                if secs_left < 90:
                    log.info(
                        "[GATE] %s %s — only %ds left in candle after delay, skipping",
                        asset, signal, secs_left,
                    )
                    state["executed_candles"][dedup_key] = {
                        "ts_unix": now, "reason": "too_late",
                    }
                    continue

                token_id = sig.get("token_id", "")
                if not token_id:
                    continue

                # ── Gate 5: Re-fetch live PM price ────────────────────────
                live_cents = _fetch_live_price(token_id, cfg["clob_url"])
                if live_cents is None:
                    log.warning("[GATE] %s %s — price fetch failed, skipping", asset, signal)
                    continue

                if live_cents < cfg["min_price"] or live_cents > cfg["max_price"]:
                    log.info(
                        "[GATE] %s %s — live price %.1f¢ out of range [%.0f, %.0f]",
                        asset, signal, live_cents, cfg["min_price"], cfg["max_price"],
                    )
                    state["executed_candles"][dedup_key] = {
                        "ts_unix": now, "reason": "price_out_of_range",
                    }
                    continue

                if live_cents < min_price:
                    log.info(
                        "[GATE] %s %s — live price %.1f¢ < min signal price %.1f¢ (repriced)",
                        asset, signal, live_cents, min_price,
                    )
                    state["executed_candles"][dedup_key] = {
                        "ts_unix": now, "reason": "repriced_below_gate",
                    }
                    continue

                # ── Gate 6: OBI hard block ────────────────────────────────
                obi = _fetch_obi(token_id, cfg["clob_url"])
                obi_threshold = cfg["obi_block"]
                if obi is not None and abs(obi) > obi_threshold:
                    log.info(
                        "[GATE] %s %s — OBI=%.3f exceeds threshold %.2f (informed flow), skipping",
                        asset, signal, obi, obi_threshold,
                    )
                    state["executed_candles"][dedup_key] = {
                        "ts_unix": now, "reason": f"obi_block_{obi:.3f}",
                    }
                    _save_state(state)
                    continue

                # ── Gate 7: OB depth check ────────────────────────────────
                ob_fillable = _fetch_ob_depth(token_id, live_cents, cfg["clob_url"])
                effective_stake = min(cfg["stake"], ob_fillable)
                MIN_STAKE = 10.0
                if effective_stake < MIN_STAKE:
                    log.info(
                        "[GATE] %s %s — book too thin ($%.2f fillable < $%.0f min)",
                        asset, signal, ob_fillable, MIN_STAKE,
                    )
                    state["executed_candles"][dedup_key] = {
                        "ts_unix": now, "reason": "book_too_thin",
                    }
                    _save_state(state)
                    continue

                log.info(
                    "[EXEC] %s %s | delay=%ds left=%ds live=%.1f¢ obi=%s stake=$%.0f → placing maker order",
                    asset, signal, secs_since_signal, secs_left, live_cents,
                    f"{obi:.3f}" if obi is not None else "n/a", effective_stake,
                )

                # ── Mark executed immediately (prevents race if loop is fast)
                state["executed_candles"][dedup_key] = {
                    "ts_unix": now, "reason": "executing",
                }
                _save_state(state)

                # ── Step 1: Try maker GTC limit order ─────────────────────
                fill = _place_maker_order(token_id, effective_stake, live_cents, cfg)

                # ── Step 2: Fallback to FAK taker if unfilled ─────────────
                if fill is None and cfg["maker_fallback"]:
                    log.info("[EXEC] Maker unfilled — falling back to FAK")
                    fill = _place_fak_order(token_id, effective_stake, cfg)

                if fill is None:
                    log.warning("[EXEC] %s %s — all order attempts failed", asset, signal)
                    state["executed_candles"][dedup_key]["reason"] = "order_failed"
                    _save_state(state)
                    _notify(
                        f"⚠️ Div Fade execution failed\n"
                        f"{asset} {signal} | live={live_cents:.1f}¢ | both maker+FAK failed"
                    )
                    continue

                # ── Record position ───────────────────────────────────────
                _write_position(sig, fill, live_cents)
                state["executed_candles"][dedup_key]["reason"] = "executed"
                state["executed_candles"][dedup_key]["fill_cents"] = fill["fill_price_cents"]
                _save_state(state)

                _notify(
                    f"💜 Div Fade ENTRY [{fill['order_type']}]\n"
                    f"{asset} {signal} | live={live_cents:.1f}¢ → fill={fill['fill_price_cents']:.1f}¢\n"
                    f"${fill['cost_usd']:.2f} × {fill['shares']:.1f} shares | "
                    f"delay={secs_since_signal}s obi={obi:.3f if obi is not None else 'n/a'}"
                )

        except Exception as e:
            log.error("Executor loop error: %s", e, exc_info=True)

        _save_state(state)
        time.sleep(15)   # 15s poll — div fade doesn't need arb-speed scanning


# ── Daemon entry ──────────────────────────────────────────────────────────────

def _daemonize():
    """Double-fork daemonize. Writes PID to PID_PATH."""
    pid = os.fork()
    if pid > 0:
        sys.exit(0)
    os.setsid()
    pid = os.fork()
    if pid > 0:
        sys.exit(0)
    # Redirect std streams
    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, "rb") as f:
        os.dup2(f.fileno(), sys.stdin.fileno())
    with open(LOG_PATH, "ab") as f:
        os.dup2(f.fileno(), sys.stdout.fileno())
        os.dup2(f.fileno(), sys.stderr.fileno())
    PID_PATH.write_text(str(os.getpid()))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true")
    args = parser.parse_args()
    if args.daemon:
        _daemonize()
    run()
