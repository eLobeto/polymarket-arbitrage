"""
PM Event Scout — Exit Monitor

Polls open positions every N seconds and closes them when:
  - Price hits +40% gain target
  - Price hits -50% loss floor
  - Market closes in < 30 min (time exit)
  - Market has already resolved

All exits are simulated in paper mode (no real orders placed).
Updates positions.jsonl and paper_balance.json on each close.
"""

import json
import time
import logging
import requests
import yaml
from datetime import datetime, timezone
from pathlib import Path
from daemon import daemonize as _daemonize

BASE_DIR = Path(__file__).parent.parent
LOG_PATH        = BASE_DIR / "logs" / "exit_monitor.log"
POSITIONS_PATH  = BASE_DIR / "logs" / "positions.jsonl"
BALANCE_PATH    = BASE_DIR / "logs" / "paper_balance.json"
SIGNALS_PATH    = BASE_DIR / "logs" / "signals.jsonl"
CONFIG_PATH     = BASE_DIR / "config" / "executor_config.yaml"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [exit_monitor] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

MEX_SIGNALS_PATH  = BASE_DIR / "logs" / "mex_signals.jsonl"
BOND_SIGNALS_PATH = BASE_DIR / "logs" / "bond_signals.jsonl"

GAMMA_BASE = "https://gamma-api.polymarket.com"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_all_positions() -> list:
    if not POSITIONS_PATH.exists():
        return []
    out = []
    with open(POSITIONS_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def save_positions(positions: list):
    with open(POSITIONS_PATH, "w") as f:
        for p in positions:
            f.write(json.dumps(p) + "\n")


def load_balance() -> dict:
    if BALANCE_PATH.exists():
        with open(BALANCE_PATH) as f:
            return json.load(f)
    return {"cash": 0.0, "deployed": 0.0, "total_pnl": 0.0, "trades": 0}


def save_balance(bal: dict):
    with open(BALANCE_PATH, "w") as f:
        json.dump(bal, f)


def get_market_info(market_id: str) -> dict | None:
    try:
        resp = requests.get(
            f"{GAMMA_BASE}/markets",
            params={"condition_ids": market_id},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data[0] if data else None
    except Exception as e:
        log.warning(f"Market fetch failed ({market_id[:12]}): {e}")
        return None


def parse_yes_price(market: dict) -> float | None:
    try:
        op = market.get("outcomePrices", "[]")
        if isinstance(op, str):
            op = json.loads(op)
        if op:
            return float(op[0])
    except Exception:
        pass
    return None


def parse_end_time(market: dict) -> datetime | None:
    try:
        end = market.get("endDate") or market.get("end_date_iso")
        if end:
            return datetime.fromisoformat(end.replace("Z", "+00:00"))
    except Exception:
        pass
    return None


def is_resolved(market: dict) -> bool:
    """Check if market has resolved/closed."""
    status = (market.get("status") or "").lower()
    active = market.get("active", True)
    closed = market.get("closed", False)
    return status in ("resolved", "closed") or not active or closed


def close_position(pos: dict, exit_price: float, reason: str, now: datetime) -> dict:
    """Return updated position dict with exit fields filled."""
    entry = pos["entry_price"]
    direction = pos.get("direction", "YES")
    shares = pos["shares"]

    # P&L: shares * (exit_price - entry_price)
    # For YES: profit when price goes up
    # For NO: we bought the NO side at (1 - yes_price), profit when NO price goes up
    # In both cases we track entry_price as the direction-adjusted price
    pnl = shares * (exit_price - entry)

    return {
        **pos,
        "status": "closed",
        "exit_price": round(exit_price, 4),
        "exit_reason": reason,
        "pnl_usd": round(pnl, 4),
        "closed_at": now.isoformat(),
    }


def _write_signal_outcome(market_id: str, pnl_usd: float, exit_reason: str,
                          signal_source: str = "news") -> None:
    """Write outcome back to the appropriate signals file for quality tracking.

    Routes to the correct file based on signal_source:
      - news  → signals.jsonl       (matched by market_id)
      - mex   → mex_signals.jsonl   (matched by best_no_trade.market_id)
      - bond  → bond_signals.jsonl  (matched by market_id)

    Enables per-strategy WR analysis in paper_summary.py.
    """
    # Pick the right file for each signal type
    if signal_source == "mex":
        target_path = MEX_SIGNALS_PATH
    elif signal_source == "bond":
        target_path = BOND_SIGNALS_PATH
    else:
        target_path = SIGNALS_PATH

    if not target_path.exists():
        return
    try:
        signals = []
        updated = False
        with target_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                s = json.loads(line)
                # MEX signals store the market_id inside best_no_trade
                if signal_source == "mex":
                    s_mid = (s.get("best_no_trade") or {}).get("market_id", "")
                else:
                    s_mid = s.get("market_id", "")
                if s_mid == market_id and s.get("outcome") is None:
                    s["outcome"]     = "win" if pnl_usd >= 0 else "loss"
                    s["pnl_usd"]     = round(pnl_usd, 4)
                    s["exit_reason"] = exit_reason
                    updated = True
                signals.append(s)
        if updated:
            with target_path.open("w") as f:
                for s in signals:
                    f.write(json.dumps(s) + "\n")
    except Exception as e:
        log.debug(f"Signal outcome write failed ({signal_source}): {e}")


def run():
    log.info("Exit monitor started")

    while True:
        try:
            cfg = load_config()
            all_positions = load_all_positions()
            open_positions = [p for p in all_positions if p.get("status") == "open"]

            if not open_positions:
                time.sleep(cfg.get("exit_poll_interval_sec", 60))
                continue

            now = datetime.now(timezone.utc)
            updated = False

            for i, pos in enumerate(all_positions):
                if pos.get("status") != "open":
                    continue

                market_id = pos["market_id"]
                entry_price = pos["entry_price"]
                direction = pos.get("direction", "YES")
                mkt = pos.get("market_title", "")[:55]

                # Fetch market data
                time.sleep(1)  # gentle rate limit
                market = get_market_info(market_id)
                if market is None:
                    continue

                yes_price = parse_yes_price(market)
                if yes_price is None:
                    continue

                # Current price for this direction
                cur_price = yes_price if direction == "YES" else (1.0 - yes_price)

                exit_reason = None
                exit_price = cur_price
                is_mex  = pos.get("signal_source") == "mex"
                is_bond = pos.get("signal_source") == "bond"

                # ── Exit 1: Resolved market (all position types) ───────────
                if is_resolved(market):
                    exit_reason = "resolved"

                # ── MEX positions: hold to resolution only ─────────────────
                # Buying NO on a structurally overpriced outcome is a hold-to-
                # resolution strategy. The NO pays out when any other bracket
                # wins — premature exits on price swings forfeit the edge.
                elif is_mex:
                    pass  # no price-based exits for MEX

                # ── Bond positions: hold to resolution with severe-drop safety ─
                # Buying YES on high-probability markets (80-94¢) — hold for full
                # resolution payout. Only exit early on a genuine information shock
                # (price collapsing >55% from entry, e.g. 87¢ → ~40¢).
                elif is_bond:
                    bond_loss_pct = cfg.get("bond", {}).get("exit_loss_pct", 0.55)
                    if entry_price > 0 and cur_price <= entry_price * (1 - bond_loss_pct):
                        exit_reason = f"bond_shock (-{bond_loss_pct*100:.0f}%)"

                else:
                    # ── Exit 2: Gain target (news/directional only) ────────
                    if entry_price > 0 and cur_price >= entry_price * (1 + cfg["exit_gain_pct"]):
                        exit_reason = f"gain_target (+{cfg['exit_gain_pct']*100:.0f}%)"

                    # ── Exit 3: Loss floor (news/directional only) ─────────
                    elif entry_price > 0 and cur_price <= entry_price * (1 - cfg["exit_loss_pct"]):
                        exit_reason = f"loss_floor (-{cfg['exit_loss_pct']*100:.0f}%)"

                # ── Exit 4: Time exit (all position types) ─────────────────
                if exit_reason is None:
                    end_time = parse_end_time(market)
                    if end_time:
                        ttl_min = (end_time - now).total_seconds() / 60
                        if ttl_min < cfg["exit_time_remaining_min"]:
                            exit_reason = f"time_exit ({ttl_min:.0f}min left)"

                if exit_reason:
                    closed = close_position(pos, exit_price, exit_reason, now)
                    all_positions[i] = closed
                    updated = True

                    # Update paper balance
                    bal = load_balance()
                    bal["cash"] = round(bal["cash"] + pos["size_usd"] + closed["pnl_usd"], 4)
                    bal["deployed"] = max(0.0, round(bal["deployed"] - pos["size_usd"], 4))
                    bal["total_pnl"] = round(bal.get("total_pnl", 0.0) + closed["pnl_usd"], 4)
                    save_balance(bal)

                    # Write outcome back to the correct signals file for quality tracking
                    _write_signal_outcome(
                        market_id=pos.get("market_id", ""),
                        pnl_usd=closed["pnl_usd"],
                        exit_reason=exit_reason,
                        signal_source=pos.get("signal_source", "news"),
                    )

                    pnl_sign = "✅" if closed["pnl_usd"] >= 0 else "❌"
                    log.info(
                        f"{pnl_sign} PAPER EXIT [{exit_reason}] {direction} {mkt} | "
                        f"entry={entry_price:.3f} exit={exit_price:.3f} "
                        f"pnl=${closed['pnl_usd']:+.2f} | "
                        f"cash: ${bal['cash']:.2f}"
                    )

            if updated:
                save_positions(all_positions)

            # Summary log every cycle
            open_count = sum(1 for p in all_positions if p.get("status") == "open")
            if open_count > 0:
                bal = load_balance()
                log.info(
                    f"📊 Open positions: {open_count} | "
                    f"Deployed: ${bal.get('deployed', 0):.2f} | "
                    f"Cash: ${bal.get('cash', 0):.2f} | "
                    f"Total P&L: ${bal.get('total_pnl', 0):+.2f}"
                )

        except Exception as e:
            log.error(f"Exit monitor loop error: {e}", exc_info=True)

        time.sleep(cfg.get("exit_poll_interval_sec", 60))


if __name__ == "__main__":
    import argparse
    _parser = argparse.ArgumentParser()
    _parser.add_argument("--daemon", action="store_true", help="Run as background daemon")
    _args = _parser.parse_args()
    if _args.daemon:
        _base = Path(__file__).parent.parent
        _daemonize(
            pidfile=_base / "logs" / "exit_monitor.pid",
            logfile=_base / "logs" / "exit_monitor.log",
        )
    run()
