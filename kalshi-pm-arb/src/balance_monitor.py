"""balance_monitor.py — Combined portfolio watchdog (Kalshi + Polymarket USDC.e).

The bot moves cash from Kalshi → PM as arbs execute: Kalshi cash buys contracts,
PM accumulates USDC.e from FOK fills. A Kalshi-only monitor fires false positives
every time capital is deployed. This monitor tracks the TRUE combined position.

Thresholds:
  COMBINED_SHUTOFF_PCT = 0.35  — shut off only if BOTH venues are down 15% combined
  COMBINED_ALERT_PCT   = 0.20  — alert at 8% combined drawdown
  KALSHI_FLOOR_USD     = 65    — warn (log only) if Kalshi cash < $65
  PM_REFRESH_CYCLES    = 25    — re-fetch PM balance every 25 cycles (~2.5 min)
"""

import logging
import rebalancer as _rebalancer
import os
import sys
import requests
from kalshi_auth import signed_headers
from config import (
    KALSHI_BASE_URL, TG_TOKEN, TG_CHAT_ID, PM_FUNDER,
    REBALANCE_ENABLED, REBALANCE_TRIGGER_USD, REBALANCE_TARGET_USD,
    REBALANCE_PM_FLOOR_USD, REBALANCE_MIN_AMOUNT,
    PM_REBALANCE_TRIGGER_USD, PM_REBALANCE_TARGET_USD,
    PM_REBALANCE_KALSHI_FLOOR, PM_REBALANCE_MIN_AMOUNT,
)

import json as _json
from datetime import datetime as _dt, timezone as _tz

log = logging.getLogger("balance_monitor")

COMBINED_SHUTOFF_PCT = 0.35   # true combined loss → shutoff
COMBINED_ALERT_PCT   = 0.20   # combined alert threshold
KALSHI_FLOOR_USD     = 65.0   # warn when Kalshi cash falls below this
PM_FLOOR_USD         = 80.0   # warn when PM USDC.e falls below this (capital is in CTF tokens)
PM_REFRESH_CYCLES    = 25     # re-query PM every N check-cycles

_USDC_E      = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
_POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
MATIC_FLOOR  = 1.0   # warn + alert below this — redemptions need gas

_combined_baseline: float | None = None   # dollars
_pm_cache: float  = 0.0                   # last known PM USDC.e (dollars)
_matic_cache: float = 0.0                 # last known MATIC balance
_alert_sent: bool = False
_matic_alert_sent: bool = False
_last_pm_cycle: int = -999
_last_matic_cycle: int = -999
_MATIC_REFRESH_CYCLES = 50  # check MATIC every ~5 min
# Require N consecutive SHUTOFF-threshold breaches before actually exiting.
# Prevents mid-trade balance dips (PM tokens not counted) from triggering SHUTOFF.
_shutoff_consecutive: int = 0
SHUTOFF_CONSECUTIVE_REQUIRED = 3  # 3 × 5 cycles × 5s = ~75s of sustained drawdown


def _get_kalshi() -> tuple[float, float] | None:
    """Returns (cash_usd, portfolio_usd) or None."""
    try:
        r = requests.get(
            KALSHI_BASE_URL + "/portfolio/balance",
            headers=signed_headers("GET", "/trade-api/v2/portfolio/balance"),
            timeout=8,
        )
        if not r.ok:
            log.warning("Kalshi balance check failed: %s", r.status_code)
            return None
        data = r.json()
        cash  = float(data.get("balance", 0)) / 100
        portf = float(data.get("portfolio_value", 0)) / 100
        return cash, portf
    except Exception as e:
        log.warning("Kalshi balance error: %s", e)
        return None


def _get_matic() -> float | None:
    """Fetch native MATIC (POL) balance on Polygon — needed for gas on redemptions."""
    try:
        from web3 import Web3
        w3  = Web3(Web3.HTTPProvider(_POLYGON_RPC, request_kwargs={"timeout": 10}))
        bal = w3.eth.get_balance(Web3.to_checksum_address(PM_FUNDER)) / 1e18
        return float(bal)
    except Exception as e:
        log.warning("MATIC balance error: %s", e)
        return None


def _get_pm() -> float | None:
    """Fetch USDC.e wallet balance on Polygon (dollars). Returns None on error."""
    try:
        from web3 import Web3
        import json as _json
        w3 = Web3(Web3.HTTPProvider(_POLYGON_RPC, request_kwargs={"timeout": 10}))
        abi = '[{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]'
        c = w3.eth.contract(
            address=Web3.to_checksum_address(_USDC_E),
            abi=_json.loads(abi),
        )
        bal = c.functions.balanceOf(Web3.to_checksum_address(PM_FUNDER)).call() / 1e6
        return float(bal)
    except Exception as e:
        log.warning("PM balance error: %s", e)
        return None


def _send_telegram(msg: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception as e:
        log.warning("Telegram send error: %s", e)



_PORTFOLIO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "portfolio.json")
_TRADES_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "trades.jsonl")
_PORTFOLIO_REFRESH_CYCLES = 25
_last_portfolio_cycle: int = -999
_KALSHI_FEE_REFRESH_CYCLES = 200  # ~every 20 min at 5-cycle intervals
_last_fee_cycle: int = -999
_cached_kalshi_fees: float | None = None


def _sync_kalshi_fees() -> float:
    """Query Kalshi settlements API to get total fees paid. Returns total in USD."""
    global _cached_kalshi_fees
    try:
        all_settlements = []
        cursor = None
        for _ in range(100):
            api_path = "/portfolio/settlements"
            params = {"limit": "100"}
            if cursor:
                params["cursor"] = cursor
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            # signed_headers expects the full path from root
            sign_path = f"/trade-api/v2{api_path}?{qs}"
            headers = signed_headers("GET", sign_path)
            r = requests.get(
                f"{KALSHI_BASE_URL}{api_path}?{qs}",
                headers=headers, timeout=10,
            )
            if not r.ok:
                log.warning("Kalshi settlements API error: %s", r.status_code)
                break
            data = r.json()
            items = data.get("settlements", [])
            all_settlements.extend(items)
            cursor = data.get("cursor")
            if not cursor or not items:
                break

        total_fees = sum(float(s.get("fee_cost", 0)) for s in all_settlements)
        _cached_kalshi_fees = round(total_fees, 4)
        log.info("[FEES] Kalshi settlements: %d total, fees=$%.2f",
                 len(all_settlements), _cached_kalshi_fees)
        return _cached_kalshi_fees
    except Exception as e:
        log.warning("_sync_kalshi_fees failed: %s", e)
        return _cached_kalshi_fees if _cached_kalshi_fees is not None else 0.0


def _write_portfolio(kal_cash: float, kal_portf: float, pm_usdc: float):
    """Read src/trades.jsonl incrementally and update portfolio.json with live balances."""
    global _last_portfolio_cycle
    try:
        portfolio = {}
        last_updated = "2000-01-01T00:00:00+00:00"
        if os.path.exists(_PORTFOLIO_FILE):
            with open(_PORTFOLIO_FILE) as _f:
                portfolio = _json.load(_f)
                last_updated = portfolio.get("last_updated", last_updated)

        # Normalise last_updated → comparable UTC string
        lu = last_updated.replace("+00:00", "Z").replace("T", " ")

        new_arb_profit  = 0.0
        new_dir_won     = 0.0
        new_dir_lost    = 0.0
        new_arb_count   = 0
        new_dir_count   = 0
        new_rb_loss     = 0.0

        if os.path.exists(_TRADES_FILE):
            with open(_TRADES_FILE) as _f:
                for line in _f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r  = _json.loads(line)
                        ts = r.get("ts", "").replace("T", " ").replace("Z", "")
                        lu_cmp = lu.replace("Z", "").replace("T", " ")
                        if ts <= lu_cmp:
                            continue
                        rtype = r.get("type", "")
                        if rtype == "arb_fill":
                            new_arb_profit += float(r.get("profit_locked", 0))
                            new_arb_count  += 1
                        elif rtype == "dir_outcome":
                            pnl = float(r.get("pnl_usd", 0))
                            if pnl >= 0:
                                new_dir_won  += pnl
                            else:
                                new_dir_lost += abs(pnl)
                            new_dir_count += 1
                        elif rtype == "rollback":
                            # PM rollback friction: negative pnl = loss, positive = gain
                            # Add to directional_lost (negative pnl increases losses,
                            # positive pnl decreases them)
                            new_rb_loss += -float(r.get("pnl_usd", 0))
                    except Exception:
                        pass

        portfolio["arb_locked_profit"] = round(
            portfolio.get("arb_locked_profit", 0) + new_arb_profit, 4)
        portfolio["directional_won"] = round(
            portfolio.get("directional_won", 0) + new_dir_won, 4)
        portfolio["directional_lost"] = round(
            portfolio.get("directional_lost", 0) + new_dir_lost + new_rb_loss, 4)
        
        # Use API-sourced fee total when available (authoritative)
        if _cached_kalshi_fees is not None:
            portfolio["kalshi_fees_total"] = _cached_kalshi_fees

        tc = portfolio.setdefault("trade_count", {"arb": 0, "directional": 0, "redemptions": 0})
        tc["arb"]         += new_arb_count
        tc["directional"] += new_dir_count

        now_str = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        kal_total = kal_cash + kal_portf
        combined = round(kal_total + pm_usdc, 2)
        deposits_total = portfolio.get("deposits", {}).get("total", 7501.47)

        # ── Authoritative P&L: on-chain truth ──
        # This is the ONLY number that matters. Everything else is analysis.
        portfolio["net_pnl"] = round(combined - deposits_total, 2)

        # ── Internal breakdown (approximate, for analysis only) ──
        baseline = portfolio.get("directional_lost_baseline", 0)
        internal_pnl = round(
            portfolio["arb_locked_profit"]
            + portfolio["directional_won"]
            - portfolio["directional_lost"]
            - portfolio.get("kalshi_fees_total", 0)
            - baseline, 4
        )
        portfolio["internal_pnl"] = internal_pnl
        portfolio["tracking_gap"] = round(portfolio["net_pnl"] - internal_pnl, 2)

        portfolio["live"] = {
            "kalshi_cash":       round(kal_cash,   2),
            "kalshi_portfolio":  round(kal_portf,  2),
            "kalshi_total":      round(kal_total,  2),
            "pm_usdc":           round(pm_usdc,    2),
            "combined":          combined,
            "last_balance_check": now_str,
        }
        portfolio["last_updated"] = now_str

        with open(_PORTFOLIO_FILE, "w") as _f:
            _json.dump(portfolio, _f, indent=2)

        log.info(
            "Portfolio updated: net_pnl=$%.2f | arb=$%.2f dir_net=$%.2f | combined=$%.2f | gap=$%.2f",
            portfolio["net_pnl"],
            portfolio["arb_locked_profit"],
            portfolio["directional_won"] - portfolio["directional_lost"],
            combined,
            portfolio["tracking_gap"],
        )
    except Exception as _e:
        log.warning("portfolio write failed: %s", _e)

def set_baseline():
    """Call once at startup. Records Kalshi + PM USDC.e as the combined baseline."""
    global _combined_baseline, _pm_cache
    kal = _get_kalshi()
    pm  = _get_pm()
    if kal is None:
        log.warning("Could not fetch Kalshi baseline — drawdown monitoring disabled")
        return
    kal_cash, kal_portf = kal
    kal_total = kal_cash + kal_portf
    pm_total  = pm if pm is not None else 0.0
    _pm_cache = pm_total
    _combined_baseline = kal_total + pm_total
    log.info(
        "Balance baseline set: $%.2f combined "
        "(Kalshi $%.2f cash + $%.2f locked | PM $%.2f USDC.e)",
        _combined_baseline, kal_cash, kal_portf, pm_total,
    )
    # Sync fees from Kalshi API at startup
    _sync_kalshi_fees()


def check(cycle: int):
    """
    Check combined drawdown every 5 cycles. Only shuts off on real combined loss.
    Logs a warning when Kalshi cash is low (normal during active trading).
    """
    global _alert_sent, _matic_alert_sent, _pm_cache, _matic_cache, \
           _last_pm_cycle, _last_matic_cycle, _last_portfolio_cycle, _last_fee_cycle

    if _combined_baseline is None:
        return
    if cycle % 5 != 0:
        return

    kal = _get_kalshi()
    if kal is None:
        return
    kal_cash, kal_portf = kal
    kal_total = kal_cash + kal_portf

    # Refresh PM balance every PM_REFRESH_CYCLES checks (web3 is slow)
    if cycle - _last_pm_cycle >= PM_REFRESH_CYCLES:
        pm = _get_pm()
        if pm is not None:
            _pm_cache = pm
            _last_pm_cycle = cycle

    # Sync Kalshi fees from settlements API periodically
    if cycle - _last_fee_cycle >= _KALSHI_FEE_REFRESH_CYCLES:
        _sync_kalshi_fees()
        _last_fee_cycle = cycle

    if cycle - _last_portfolio_cycle >= _PORTFOLIO_REFRESH_CYCLES:
        _write_portfolio(kal_cash, kal_portf, _pm_cache)
        _last_portfolio_cycle = cycle

    # MATIC gas balance check
    if cycle - _last_matic_cycle >= _MATIC_REFRESH_CYCLES:
        matic = _get_matic()
        if matic is not None:
            _matic_cache = matic
            _last_matic_cycle = cycle
            if matic < MATIC_FLOOR:
                if not _matic_alert_sent:
                    msg = (
                        f"⛽ <b>Low MATIC / Gas Warning</b>\n"
                        f"PM wallet MATIC: <b>{matic:.3f}</b> (floor {MATIC_FLOOR})\n"
                        f"CTF redemptions require gas — top up <b>{PM_FUNDER[:10]}…</b> on Polygon."
                    )
                    log.warning("MATIC balance %.3f below floor %.1f — redemptions at risk", matic, MATIC_FLOOR)
                    _send_telegram(msg)
                    _matic_alert_sent = True
            else:
                if _matic_alert_sent:
                    log.info("MATIC balance recovered: %.3f", matic)
                    _matic_alert_sent = False

    combined = kal_total + _pm_cache
    drawdown  = (_combined_baseline - combined) / _combined_baseline

    log.debug(
        "Balance: Kalshi $%.2f+$%.2f | PM $%.2f | Combined $%.2f / baseline $%.2f (Δ%.1f%%)",
        kal_cash, kal_portf, _pm_cache, combined, _combined_baseline, -drawdown * 100,
    )

    # Kalshi cash floor: log warning but keep running (PM is earning)
    if kal_cash < KALSHI_FLOOR_USD:
        log.warning(
            "Kalshi cash $%.2f below floor $%.2f — waiting for settlements "
            "(PM=$%.2f, combined=$%.2f, Δ%+.1f%%)",
            kal_cash, KALSHI_FLOOR_USD, _pm_cache, combined, -drawdown * 100,
        )

    # Auto-rebalance: top up Kalshi from PM USDC.e when cash is low
    if REBALANCE_ENABLED:
        do_rebal, rebal_amount = _rebalancer.should_rebalance(
            kal_cash, _pm_cache,
            REBALANCE_TRIGGER_USD, REBALANCE_TARGET_USD,
            REBALANCE_PM_FLOOR_USD, REBALANCE_MIN_AMOUNT,
        )
        if do_rebal:
            log.info(
                "Auto-rebalance triggered: Kalshi $%.2f < $%.2f -- sending $%.2f from PM",
                kal_cash, REBALANCE_TRIGGER_USD, rebal_amount
            )
            _rebalancer.rebalance(
                rebal_amount, kal_cash, _pm_cache,
                tg_token=TG_TOKEN, tg_chat=TG_CHAT_ID
            )

    # PM USDC floor: normal during active trading (USDC is locked in CTF tokens)
    if _pm_cache < PM_FLOOR_USD and _last_pm_cycle > 0:
        log.warning(
            "PM USDC $%.2f below floor $%.2f — capital deployed in CTF tokens "
            "(Kalshi=$%.2f, combined=$%.2f, Δ%+.1f%%)",
            _pm_cache, PM_FLOOR_USD, kal_total, combined, -drawdown * 100,
        )

    # Reverse rebalance: top up PM wallet from Kalshi when PM runs dry
    if REBALANCE_ENABLED:
        do_rev, rev_amount = _rebalancer.should_reverse_rebalance(
            kal_cash, _pm_cache,
            PM_REBALANCE_TRIGGER_USD, PM_REBALANCE_TARGET_USD,
            PM_REBALANCE_KALSHI_FLOOR, PM_REBALANCE_MIN_AMOUNT,
        )
        if do_rev:
            log.info(
                "Reverse rebalance triggered: PM $%.2f < $%.2f -- sending $%.2f from Kalshi",
                _pm_cache, PM_REBALANCE_TRIGGER_USD, rev_amount,
            )
            _rebalancer.reverse_rebalance(
                rev_amount, kal_cash, _pm_cache,
                tg_token=TG_TOKEN, tg_chat=TG_CHAT_ID,
            )

    # ── Combined shutoff: require N consecutive threshold breaches ────────────
    # Mid-trade, PM tokens are in the CTF contract (not USDC.e) → combined
    # drops temporarily but recovers on resolution. Requiring 3 consecutive
    # breaches (~75s) ensures we only SHUTOFF on sustained real loss, not
    # a single mid-trade accounting dip.
    global _shutoff_consecutive
    if drawdown >= COMBINED_SHUTOFF_PCT:
        _shutoff_consecutive += 1
        log.warning(
            "SHUTOFF threshold breach %d/%d: %.1f%% drawdown "
            "(baseline=$%.2f, kal=$%.2f, pm=$%.2f, combined=$%.2f)",
            _shutoff_consecutive, SHUTOFF_CONSECUTIVE_REQUIRED,
            drawdown * 100, _combined_baseline, kal_total, _pm_cache, combined,
        )
        if _shutoff_consecutive >= SHUTOFF_CONSECUTIVE_REQUIRED:
            msg = (
                f"🛑 <b>Cross-Candle Arb AUTO-SHUTOFF</b>\n"
                f"Combined portfolio down <b>{drawdown*100:.1f}%</b> for "
                f"{_shutoff_consecutive} consecutive checks — real loss detected.\n"
                f"Baseline: <b>${_combined_baseline:.2f}</b>\n"
                f"Kalshi: <b>${kal_cash:.2f}</b> cash + <b>${kal_portf:.2f}</b> locked = <b>${kal_total:.2f}</b>\n"
                f"PM USDC.e: <b>${_pm_cache:.2f}</b>\n"
                f"Combined: <b>${combined:.2f}</b>\n"
                f"Bot stopped. Manual review required before restarting."
            )
            log.error(
                "SHUTOFF: %.1f%% combined drawdown "
                "(baseline=$%.2f, kal=$%.2f, pm=$%.2f, combined=$%.2f)",
                drawdown * 100, _combined_baseline, kal_total, _pm_cache, combined,
            )
            _send_telegram(msg)
            sys.exit(0)
    else:
        if _shutoff_consecutive > 0:
            log.info("SHUTOFF counter reset (drawdown %.1f%% back below threshold)", drawdown * 100)
        _shutoff_consecutive = 0

    # ── Combined alert ────────────────────────────────────────────────────────
    if drawdown >= COMBINED_ALERT_PCT and not _alert_sent:
        msg = (
            f"⚠️ <b>Cross-Candle Arb Alert</b>\n"
            f"Combined down <b>{drawdown*100:.1f}%</b> from baseline.\n"
            f"Kalshi: <b>${kal_total:.2f}</b> | PM: <b>${_pm_cache:.2f}</b>\n"
            f"Combined: <b>${combined:.2f}</b> / baseline <b>${_combined_baseline:.2f}</b>"
        )
        log.warning(
            "ALERT: %.1f%% combined drawdown "
            "(kal=$%.2f, pm=$%.2f, combined=$%.2f, baseline=$%.2f)",
            drawdown * 100, kal_total, _pm_cache, combined, _combined_baseline,
        )
        _send_telegram(msg)
        _alert_sent = True

    if drawdown < COMBINED_ALERT_PCT * 0.5 and _alert_sent:
        _alert_sent = False
        log.info("Combined balance recovered — alert reset (Δ%+.1f%%)", -drawdown * 100)
