"""main.py — Cross-platform candle arb bot (Kalshi ↔ Polymarket)."""
import argparse
import asyncio
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path

# Ensure src/ is on the path when run as `python3 src/main.py`
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / "config" / ".env")

import kalshi_markets
import pm_markets
import price_feed
import matcher
import executor
import notifier
from direction_manager import DirectionManager
import balance_monitor
import redeemer
import trade_logger
import div_fade_monitor
from config import (
    LIVE_TRADING, PM_FUNDER, POLL_INTERVAL_SECS, POLL_INTERVAL_OVERNIGHT_SECS, MARKET_REFRESH_SECS,
    ASSETS, MAX_DIRECTIONAL_USD, DIRECTIONAL_MAX_PER_SIDE,
)

REDEEM_INTERVAL_SECS = 300  # redeem every 5 minutes

COOLDOWN_FILE      = os.path.join(os.path.dirname(__file__), "cooldowns.json")
DIRECTIONAL_FILE   = os.path.join(os.path.dirname(__file__), "directional_positions.json")


def _load_cooldowns() -> dict:
    """Load persisted cooldowns from disk, purge expired entries."""
    try:
        with open(COOLDOWN_FILE) as f:
            data = json.load(f)
        saved_host = data.pop("__host__", None)
        current_host = socket.gethostname()
        if saved_host and saved_host != current_host:
            log.warning(
                "Cooldowns written by '%s' but running on '%s' — clearing all stale cooldowns",
                saved_host, current_host,
            )
            return {}
        now = time.time()
        return {k: v for k, v in data.items() if now < v}  # v is expiry timestamp
    except Exception:
        return {}


def _save_cooldowns(cooldowns: dict):
    try:
        with open(COOLDOWN_FILE, "w") as f:
            json.dump({**cooldowns, "__host__": socket.gethostname()}, f)
    except Exception as e:
        log.warning("Failed to save cooldowns: %s", e)


def _load_directional_positions() -> dict:
    """Load persisted directional positions, purge expired (>20min old)."""
    try:
        with open(DIRECTIONAL_FILE) as f:
            data = json.load(f)
        now = time.time()
        active = {k: v for k, v in data.items() if now - v.get("timestamp", 0) < 1200}
        purged = len(data) - len(active)
        if active or purged:
            log.info("Loaded %d directional position(s) from disk (%d expired purged)", len(active), purged)
        return active
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("Failed to load directional positions: %s", e)
        return {}


def _save_directional_positions(positions: dict):
    try:
        with open(DIRECTIONAL_FILE, "w") as f:
            json.dump(positions, f)
    except Exception as e:
        log.warning("Failed to save directional positions: %s", e)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("MAIN")


def _resolve_pm_outcome(pm_token: str):
    """
    Query gamma API to determine if a PM conditional token resolved as winner or loser.

    Returns:
      True  — token resolved to $1 (winner)
      False — token resolved to $0 (loser / expired worthless)
      None  — undetermined (still trading, API error, or market not found)

    Fixes the 'already_redeemed' false-positive bug: a position absent from
    data-api positions could be EITHER redeemed (won) OR expired worthless (lost).
    The gamma API outcomePrices field gives the definitive answer.
    """
    import requests as _req
    try:
        r = _req.get(
            f"https://gamma-api.polymarket.com/markets?clob_token_ids={pm_token}",
            timeout=8,
        )
        if not r.ok:
            log.warning("Gamma API outcome check failed: HTTP %d for %s", r.status_code, pm_token[:16])
            return None
        markets = r.json()
        if not isinstance(markets, list) or not markets:
            log.warning("Gamma API: no market found for token %s", pm_token[:16])
            return None
        m = markets[0]
        token_ids = m.get("clobTokenIds", [])
        prices    = m.get("outcomePrices", [])
        if not token_ids or not prices or len(token_ids) != len(prices):
            return None
        try:
            idx   = token_ids.index(pm_token)
            price = float(prices[idx])
            if price >= 0.99:
                log.info("Gamma API: token %s resolved as WINNER ($1)", pm_token[:16])
                return True
            elif price <= 0.01:
                log.info("Gamma API: token %s resolved as LOSER ($0)", pm_token[:16])
                return False
            else:
                log.info("Gamma API: token %s still trading @ %.3f — outcome unclear", pm_token[:16], price)
                return None
        except (ValueError, IndexError):
            log.warning("Gamma API: token %s not found in clobTokenIds", pm_token[:16])
            return None
    except Exception as e:
        log.warning("Gamma API outcome check error: %s", e)
        return None


def _check_directional_outcomes(expired: dict):
    """Query PM for outcome of each expired directional position and send P&L alert."""
    import requests as _req
    try:
        addr = PM_FUNDER
        r = _req.get(
            f"https://data-api.polymarket.com/positions?user={addr}&sizeThreshold=.01",
            timeout=25,
        )
        if not r.ok:
            return
        positions = r.json()
        if not isinstance(positions, list):
            return
        pm_lookup = {p.get("asset", ""): float(p.get("currentValue", 0)) for p in positions}
    except Exception as e:
        log.warning("Outcome check API failed: %s", e)
        return

    for pm_token, pos in expired.items():
        try:
            current_val = pm_lookup.get(pm_token, None)
            if current_val is None:
                # Position absent from PM — could be redeemed (won) OR expired worthless (lost).
                # Ask gamma API for the definitive resolution; don't guess.
                gamma_won = _resolve_pm_outcome(pm_token)
                if gamma_won is True:
                    # Use actual_shares if stored, else fall back to int contracts
                    actual_sh = float(pos.get("actual_shares", pos["contracts"]))
                    profit = actual_sh - pos["usd"]
                    log.info("Outcome resolved WIN (gamma): %s profit=$%.2f", pm_token[:16], profit)
                    notifier.directional_outcome(pos, profit, won=True, already_redeemed=True)
                    trade_logger.log_dir_outcome(pos, profit, won=True, already_redeemed=True)
                elif gamma_won is False:
                    profit = -pos["usd"]
                    log.info("Outcome resolved LOSS (gamma): %s cost=$%.2f", pm_token[:16], pos["usd"])
                    notifier.directional_outcome(pos, profit, won=False)
                    trade_logger.log_dir_outcome(pos, profit, won=False)
                else:
                    # Gamma API inconclusive — record as loss (conservative; avoids phantom wins)
                    profit = -pos["usd"]
                    log.warning("Outcome UNDETERMINED (gamma) for %s — recording as loss", pm_token[:16])
                    notifier.directional_outcome(pos, profit, won=False)
                    trade_logger.log_dir_outcome(pos, profit, won=False)
            elif current_val > pos["usd"] * 0.5:
                profit = current_val - pos["usd"]
                notifier.directional_outcome(pos, profit, won=True)
                trade_logger.log_dir_outcome(pos, profit, won=True)
            else:
                profit = -pos["usd"]
                notifier.directional_outcome(pos, profit, won=False)
                trade_logger.log_dir_outcome(pos, profit, won=False)
        except Exception as e:
            log.warning("Outcome notify failed for %s: %s", pm_token[:12], e)


async def main():
    mode = "LIVE" if LIVE_TRADING else "PAPER"
    log.info("=" * 60)
    log.info("Cross-Platform Candle Arb Bot — %s MODE", mode)
    log.info("Assets: %s", ASSETS)
    log.info("=" * 60)

    if LIVE_TRADING:
        log.info("Running pre-flight checks...")
        if not executor.preflight_check():
            log.error("Pre-flight FAILED — refusing to start in LIVE mode. Fix issues and restart.")
            return
        log.info("Pre-flight passed ✓")

    if LIVE_TRADING:
        balance_monitor.set_baseline()

    price_feed.start()
    div_fade_monitor.start_monitor()
    time.sleep(2)

    active_kalshi: list[dict] = []
    active_pm:     list[dict] = []
    last_refresh   = 0.0
    last_redeem    = 0.0
    cooldowns      = _load_cooldowns()
    log.info("Loaded %d active cooldowns from disk", len(cooldowns))
    cycle          = 0
    directional_positions: dict[str, dict] = _load_directional_positions()
    direction_mgr = DirectionManager(sell_fn=executor._sell_pm_fok)

    while True:
        now = time.time()

        if now - last_refresh >= MARKET_REFRESH_SECS:
            try:
                active_kalshi = kalshi_markets.fetch_kalshi_markets()
                active_pm     = pm_markets.fetch_pm_markets()
                last_refresh  = now

                pm_tokens = [
                    tid for m in active_pm
                    for tid in [m.get("up_token_id"), m.get("dn_token_id")]
                    if tid
                ]
                kal_tickers = [m["ticker"] for m in active_kalshi]
                price_feed.subscribe_pm(pm_tokens)
                price_feed.subscribe_kalshi(kal_tickers)

            except Exception as e:
                log.error("Market refresh error: %s", e)

        if LIVE_TRADING and now - last_redeem >= REDEEM_INTERVAL_SECS:
            redeemed = redeemer.redeem_winning_positions()
            if redeemed > 0:
                notifier._send(f"💸 <b>Auto-redeemed ${redeemed:.2f}</b> in winning PM positions → back in wallet")
            last_redeem = now

        if LIVE_TRADING:
            balance_monitor.check(cycle)

        cycle += 1
        try:
            windows = matcher.find_arb_windows(active_kalshi, active_pm)
        except Exception as e:
            log.error("Matcher error: %s", e)
            windows = []

        if windows:
            log.info("[CYCLE %d] %d arb window(s) found", cycle, len(windows))
        else:
            log.debug("[CYCLE %d] No arb windows", cycle)

        # Smart hold/cut: evaluate open directional positions against Binance signal
        if directional_positions:
            _cut = direction_mgr.evaluate(directional_positions)
            for _tid in _cut:
                directional_positions.pop(_tid, None)
                _save_directional_positions(directional_positions)

        expired_positions = {k: v for k, v in directional_positions.items()
                             if now - v["timestamp"] >= 1200}
        directional_positions = {k: v for k, v in directional_positions.items()
                                 if now - v["timestamp"] < 1200}
        if expired_positions:
            _save_directional_positions(directional_positions)
            asyncio.get_event_loop().run_in_executor(
                None, _check_directional_outcomes, expired_positions
            )
        open_directional_usd = sum(v["usd"] for v in directional_positions.values())
        open_dir_up  = sum(1 for v in directional_positions.values() if v["pm_side"] == "UP")
        open_dir_dn  = sum(1 for v in directional_positions.values() if v["pm_side"] == "DOWN")
        if open_directional_usd >= MAX_DIRECTIONAL_USD:
            log.warning("[EXPOSURE] Open directional PM exposure $%.2f >= $%.2f limit -- pausing new entries",
                        open_directional_usd, MAX_DIRECTIONAL_USD)

        for window in windows:
            asset      = window["asset"]
            kal_ticker = window["kal_ticker"]

            if open_directional_usd >= MAX_DIRECTIONAL_USD:
                log.debug("Skipping %s — directional exposure cap reached ($%.2f)", kal_ticker, open_directional_usd)
                continue

            _pm_side = window.get("pm_side", "?")
            if _pm_side == "UP" and open_dir_up >= DIRECTIONAL_MAX_PER_SIDE:
                log.debug("Skipping %s — UP concentration limit (%d/%d)",
                          kal_ticker, open_dir_up, DIRECTIONAL_MAX_PER_SIDE)
                continue
            if _pm_side == "DOWN" and open_dir_dn >= DIRECTIONAL_MAX_PER_SIDE:
                log.debug("Skipping %s — DOWN concentration limit (%d/%d)",
                          kal_ticker, open_dir_dn, DIRECTIONAL_MAX_PER_SIDE)
                continue

            expiry = cooldowns.get(kal_ticker, 0)
            if now < expiry:
                log.debug("Already entered %s this candle (%.0fs left)", kal_ticker,
                          expiry - now)
                continue

            log.info("[ARB] %s %s: PM %s @ %.1f¢ + Kalshi %s @ %.1f¢ = %.1f¢ combined (profit %.1f¢)",
                     asset, window["timeframe"],
                     window["pm_side"],  window["pm_price"],
                     window["kal_side"], window["kal_price"],
                     window["combined"], window["profit_cents"])

            if not LIVE_TRADING:
                notifier.paper_window(window)
                cooldowns[kal_ticker] = window["kalshi_market"]["candle_end_ts"]
                continue

            notifier.arb_detected(window)

            try:
                result = await executor.execute_arb(window, live=LIVE_TRADING, directional_fallback=True)
            except Exception as e:
                log.error("Execution error: %s", e, exc_info=True)
                # CRITICAL: If PM was already filled inside execute_arb before
                # the crash, we have naked exposure. The executor tracks this
                # via an internal flag. Attempt emergency rollback.
                _pm_token = window.get("pm_token_id", "")
                _pm_shares = getattr(executor, '_last_pm_shares', 0)
                _pm_filled = getattr(executor, '_last_pm_filled', False)
                if _pm_filled and _pm_token and _pm_shares > 0:
                    log.error("🚨 PM was filled before crash — emergency rollback! %.2f shares", _pm_shares)
                    _emergency_rb = executor._sell_pm_fok(_pm_token, _pm_shares)
                    if _emergency_rb:
                        log.info("Emergency rollback OK: recovered $%.2f", _emergency_rb.get("cost", 0))
                    else:
                        log.error("🚨 EMERGENCY ROLLBACK FAILED — naked PM exposure!")
                        executor._enqueue_rollback(_pm_token, _pm_shares)
                    try:
                        notifier._send(
                            f"🚨 <b>CRASH DURING ARB — {'rollback OK' if _emergency_rb else 'NAKED POSITION!'}</b>\n"
                            f"Error: <code>{str(e)[:100]}</code>\n"
                            f"PM shares: {_pm_shares:.1f}"
                        )
                    except Exception:
                        pass
                    result = {"success": False, "error": str(e), "pm_filled": True,
                              "directional": _emergency_rb is None,
                              "rollback_result": _emergency_rb}
                else:
                    result = {"success": False, "error": str(e)}

            skip_reason = result.get("error", "")
            skip_cooldown = result.get("skip_cooldown", False)
            no_attempt_errors = ("balance too low", "contract count < 1", "geoblocked", "not enough balance")
            if not skip_cooldown and not any(e in skip_reason for e in no_attempt_errors):
                cooldowns[kal_ticker] = window["kalshi_market"]["candle_end_ts"]
                _save_cooldowns(cooldowns)
            elif skip_cooldown or any(e in skip_reason for e in no_attempt_errors):
                log.info("Skipping cooldown for %s — no execution attempted (%s)", kal_ticker, skip_reason)

            if result.get("success"):
                log.info("[FILL] Both legs filled! Locked profit: $%.3f", result["profit_locked"])
                notifier.both_filled(result, window)
                trade_logger.log_arb_fill(result, window)
                directional_positions.pop(window.get("pm_token_id", ""), None)
                _save_directional_positions(directional_positions)
                if result.get("excess_pm_result"):
                    # Capture friction loss from partial Kalshi fill (excess PM sold back)
                    prorated_cost = (result["pm_usd"] / result["pm_shares"]) * result["excess_pm_shares"]
                    trade_logger.log_rollback(result["pm_result"], result["excess_pm_result"], window, cost_usd=prorated_cost)
            elif result.get("directional"):
                pm_usd   = result.get("pm_price", 50) / 100 * result.get("contracts", 0)
                pm_side  = window.get("pm_side", "?")
                directional_positions[window.get("pm_token_id", kal_ticker)] = {
                    "usd": pm_usd, "timestamp": now, "pm_side": pm_side,
                    "contracts": result.get("contracts", 0),
                    "actual_shares": float((result.get("pm_result") or {}).get("shares", result.get("contracts", 0))),
                    "asset": window.get("asset", "?"),
                    "timeframe": window.get("timeframe", "15m"),
                    "intentional": result.get("depth_gate_directional", False),
                    "kal_ticker": kal_ticker,
                }
                open_directional_usd += pm_usd
                _save_directional_positions(directional_positions)
                if result.get("depth_gate_directional"):
                    log.info("[FILL] Intentional directional (depth-gate fallback) %s $%.2f @ %.1fc. Total open: $%.2f",
                             pm_side, pm_usd, result.get("pm_price", 0), open_directional_usd)
                else:
                    log.warning("[FILL] One-sided fill -- directional exposure! Total open: $%.2f", open_directional_usd)
                notifier.one_sided(result, window)
                trade_logger.log_dir_entry(result, window)
            elif result.get("pm_filled") and result.get("rollback_result"):
                # Captures the friction loss from a successful PM-only rollback
                trade_logger.log_rollback(result["pm_result"], result["rollback_result"], window)
                try:
                    import notifier as _ntf_rb
                    _rb_cost = float(result["pm_result"].get("cost", 0))
                    _rb_proc = float(result["rollback_result"].get("cost", 0))
                    _rb_loss = _rb_proc - _rb_cost  # negative = loss
                    _ntf_rb._send(
                        f"↩️ <b>[ROLLBACK]</b> {window.get('asset','?')} {window.get('timeframe','15m')} "
                        f"PM_{window.get('pm_side','?')} @ {result.get('pm_price',0):.1f}¢\n"
                        f"Kalshi no-fill → PM sold back\n"
                        f"Spent: ${_rb_cost:.2f} | Recovered: ${_rb_proc:.2f} | Loss: <b>-${abs(_rb_loss):.2f}</b>"
                    )
                except Exception as _e:
                    log.warning("Rollback notifier error: %s", _e)
            else:
                log.warning("[FILL] Both legs failed")

            break  # one entry per cycle max

        # Overnight: Kalshi books thin 23:00-13:00 UTC — slow scan
        from datetime import datetime, timezone as _tz
        _hour = datetime.now(_tz.utc).hour
        _poll = POLL_INTERVAL_SECS if 13 <= _hour < 23 else POLL_INTERVAL_OVERNIGHT_SECS
        await asyncio.sleep(_poll)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kalshi-PM cross-candle arb bot")
    parser.add_argument("--daemon", action="store_true",
                        help="Run as background daemon (double-fork)")
    args = parser.parse_args()

    if args.daemon:
        from daemon import daemonize
        daemonize()

    asyncio.run(main())
