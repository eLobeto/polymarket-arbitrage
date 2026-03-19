"""trade_logger.py — Persistent JSONL trade log for strategy analysis.

Records are appended to trades.jsonl in the bot directory.
Three record types:
  arb_fill    — both legs filled, locked profit known
  dir_entry   — one-sided PM fill (accidental or depth-gate intentional)
  dir_outcome — directional position resolved (win/loss + pnl)
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("trade_logger")

_TRADES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "trades.jsonl")
_OPEN_ARBS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "open_arbs.json")
_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_candle_close(kal_ticker: str) -> str:
    """
    Parse the Kalshi ticker to extract candle close timestamp (UTC ISO).
    Format: KX{asset}{tf}-{YY}{MON}{DD}{HHMM}-{MM}
    Example: KXBTC15M-26MAR102115-15  →  2026-03-10T21:15:00Z
    Returns empty string on parse failure.
    """
    import re
    _MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
               "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    try:
        # e.g. "26MAR102115"
        m = re.search(r'-(\d{2})([A-Z]{3})(\d{2})(\d{4})-\d+$', kal_ticker)
        if not m:
            return ""
        yy, mon, dd, hhmm = m.group(1), m.group(2), m.group(3), m.group(4)
        year  = 2000 + int(yy)
        month = _MONTHS.get(mon, 0)
        day   = int(dd)
        hour  = int(hhmm[:2])
        minute= int(hhmm[2:])
        if not month:
            return ""
        dt = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def _append(record: dict):
    """Thread-safe append of a JSON record to trades.jsonl."""
    try:
        with _lock:
            with open(_TRADES_FILE, "a") as f:
                f.write(json.dumps(record) + "\n")
    except Exception as e:
        log.warning("trade_logger write failed: %s", e)


def log_arb_fill(result: dict, window: dict):
    """Log a successful both-legs-filled arb trade.

    Captures full cost basis for tax purposes:
      - pm_cost_usd  : actual USD spent on PM leg (from CLOB fill)
      - kal_cost_usd : actual USD spent on Kalshi leg (contracts × fill price)
      - total_cost_usd: combined outlay
      - proceeds_usd  : $1.00 × contracts (guaranteed payout at resolution)
      - profit_locked : proceeds - total_cost (net locked gain)
      - roi_pct       : profit_locked / total_cost × 100
    """
    try:
        contracts    = int(result.get("contracts", 0))
        pm_cost      = round(float(result.get("pm_usd",  0)), 4)
        kal_cost     = round(float(result.get("kal_usd", 0)), 4)
        total_cost   = round(pm_cost + kal_cost, 4)
        proceeds     = round(float(result.get("proceeds_usd", contracts)), 4)
        profit       = round(float(result.get("profit_locked", 0)), 4)
        roi_pct      = round(profit / total_cost * 100, 2) if total_cost > 0 else 0.0

        _append({
            # Identifiers
            "ts":              _now_iso(),
            "candle_close_ts": _parse_candle_close(window.get("kal_ticker", "")),
            "type":            "arb_fill",
            "entry_mode":      window.get("entry_mode", "normal"),
            "asset":           window.get("asset", "?"),
            "tf":              window.get("timeframe", "15m"),
            "kal_ticker":      window.get("kal_ticker", "?"),
            "pm_token_id":     window.get("pm_token_id", ""),
            # Fill prices
            "pm_side":       window.get("pm_side", "?"),
            "pm_price_c":    round(float(result.get("pm_price", window.get("pm_price", 0))), 2),
            "pm_shares":     round(float(result.get("pm_shares", 0)), 4),
            "kal_side":      window.get("kal_side", "?"),
            "kal_price_c":   round(float(result.get("kal_price", window.get("kal_price", 0))), 2),
            "contracts":     contracts,
            # Cost basis (tax)
            "pm_cost_usd":   pm_cost,
            "kal_cost_usd":  kal_cost,
            "total_cost_usd": total_cost,
            "proceeds_usd":  proceeds,
            "profit_locked": profit,
            "roi_pct":       roi_pct,
            "condition_id":  window.get("pm_condition_id", ""),
            # Oracle state at entry — for middling fingerprint analysis
            "oracle_divergence": window.get("oracle_divergence", 0.0),
            "oracle_velocity":   window.get("oracle_velocity"),    # $/s at entry; None = no history
            "dead_zone":         window.get("dead_zone"),
            "oracle_allowed":    window.get("oracle_allowed", False),
        })
        # Track as open arb so redeemer can log the outcome
        _track_open_arb(
            condition_id=window.get("pm_condition_id", ""),
            kal_ticker=window.get("kal_ticker", ""),
            candle_close_ts=_parse_candle_close(window.get("kal_ticker", "")),
            asset=window.get("asset", ""),
            tf=window.get("timeframe", ""),
        )
    except Exception as e:
        log.warning("log_arb_fill failed: %s", e)


def log_dir_entry(result: dict, window: dict):
    """Log a directional PM-only entry (accidental one-sided or intentional depth-gate).

    Cost basis: pm_cost_usd is actual USD paid (from CLOB fill cost field).
    At resolution, proceeds are either $1.00 × shares (win) or $0 (loss).
    """
    try:
        pm_result  = result.get("pm_result") or {}
        pm_price_c = round(float(result.get("pm_price", pm_result.get("price_cents",
                                  window.get("pm_price", 0)))), 2)
        pm_shares  = round(float(pm_result.get("shares", result.get("contracts", 0))), 4)
        pm_cost    = round(float(pm_result.get("cost",
                           pm_price_c / 100 * pm_shares)), 4)

        _append({
            # Identifiers
            "ts":           _now_iso(),
            "type":         "dir_entry",
            "entry_mode":   window.get("entry_mode", "normal"),
            "asset":        window.get("asset", "?"),
            "tf":           window.get("timeframe", "15m"),
            "kal_ticker":   window.get("kal_ticker", "?"),
            "pm_token_id":  window.get("pm_token_id", ""),
            # Fill details
            "pm_side":      window.get("pm_side", "?"),
            "pm_price_c":   pm_price_c,
            "pm_shares":    pm_shares,
            # Cost basis (tax)
            "pm_cost_usd":  pm_cost,
            "intentional":  bool(result.get("depth_gate_directional", False)),
            "signal_c":     round(float(window.get("profit_cents", 0)), 2),
        })
    except Exception as e:
        log.warning("log_dir_entry failed: %s", e)


def log_dir_outcome(pos: dict, profit_usd: float, won: bool, already_redeemed: bool = False):
    """Log the outcome of a directional position when it expires.

    Tax fields:
      - cost_basis_usd : what we paid (pm_cost_usd at entry)
      - proceeds_usd   : $1.00 × shares on win, $0 on loss
      - pnl_usd        : net gain/loss
    """
    try:
        shares     = round(float(pos.get("contracts", pos.get("pm_shares", 0))), 4)
        cost_basis = round(float(pos.get("pm_cost_usd", pos.get("usd", 0))), 4)
        proceeds   = round(shares * 1.0, 4) if won else 0.0

        _append({
            # Identifiers
            "ts":               _now_iso(),
            "type":             "dir_outcome",
            "pm_token_id":      pos.get("pm_token_id", ""),
            "asset":            pos.get("asset", "?"),
            "tf":               pos.get("timeframe", "15m"),
            "kal_ticker":       pos.get("kal_ticker", "?"),
            "pm_side":          pos.get("pm_side", "?"),
            # Entry reference
            "pm_price_c":       round(float(pos.get("pm_price_c", 0)), 2),
            "pm_shares":        shares,
            "intentional":      bool(pos.get("intentional", False)),
            # Outcome
            "outcome":          "win" if won else "loss",
            # Cost basis (tax)
            "cost_basis_usd":   cost_basis,
            "proceeds_usd":     proceeds,
            "pnl_usd":          round(float(profit_usd), 4),
            "already_redeemed": already_redeemed,
        })
    except Exception as e:
        log.warning("log_dir_outcome failed: %s", e)


def log_rollback(pm_result: dict, rollback_result: dict, window: dict, cost_usd: float = None):
    """Log a successful PM-only rollback (failed hedge).

    This captures the 'friction loss' from buying and then immediately selling
    the PM leg when the Kalshi leg fails to fill.
    """
    try:
        cost     = round(float(cost_usd if cost_usd is not None else pm_result.get("cost", 0)), 4)
        proceeds = round(float(rollback_result.get("cost", 0)), 4)
        pnl      = round(proceeds - cost, 4)
        shares   = round(float(rollback_result.get("shares", pm_result.get("shares", 0))), 4)

        _append({
            "ts":           _now_iso(),
            "type":         "rollback",
            "asset":        window.get("asset", "?"),
            "pm_token_id":  window.get("pm_token_id", ""),
            "pm_side":      window.get("pm_side", "?"),
            "pm_shares":    shares,
            "cost_usd":     cost,
            "proceeds_usd": proceeds,
            "pnl_usd":      pnl,
        })
        log.info("[ROLLBACK] Logged friction loss: $%.2f (shares=%.2f)", pnl, shares)
    except Exception as e:
        log.warning("log_rollback failed: %s", e)



def _track_open_arb(condition_id: str, kal_ticker: str, candle_close_ts: str, asset: str, tf: str):
    """Persist a new open arb position to open_arbs.json."""
    if not condition_id:
        return
    try:
        with _lock:
            data = {}
            if os.path.exists(_OPEN_ARBS_FILE):
                with open(_OPEN_ARBS_FILE) as f:
                    data = json.load(f)
            data[condition_id] = {
                "kal_ticker":      kal_ticker,
                "candle_close_ts": candle_close_ts,
                "asset":           asset,
                "tf":              tf,
                "fill_ts":         _now_iso(),
            }
            with open(_OPEN_ARBS_FILE, "w") as f:
                json.dump(data, f)
    except Exception as e:
        log.warning("_track_open_arb failed: %s", e)


def resolve_open_arb(condition_id: str):
    """Remove a resolved arb from open_arbs.json. Returns entry or None."""
    if not condition_id:
        return None
    try:
        with _lock:
            if not os.path.exists(_OPEN_ARBS_FILE):
                return None
            with open(_OPEN_ARBS_FILE) as f:
                data = json.load(f)
            entry = data.pop(condition_id, None)
            with open(_OPEN_ARBS_FILE, "w") as f:
                json.dump(data, f)
            return entry
    except Exception as e:
        log.warning("resolve_open_arb failed: %s", e)
        return None


def log_arb_outcome(condition_id: str, winning_side: str, value_usd: float,
                    pm_loss_usd: float = 0.0, kal_loss_usd: float = 0.0):
    """Log which side won a resolved arb trade.

    winning_side: "pm"      — PM tokens redeemed (PM side was correct)
                  "kalshi"  — PM tokens expired worthless (Kalshi side was correct)
                  "middled" — BOTH sides lost (oracle divergence caused opposite outcomes)
    value_usd: redemption value (PM wins) or 0.0 (Kalshi wins / middled)

    When Kalshi wins, we look up the original arb_fill to calculate the Kalshi
    taker fee (~7% of profit). This fee is deducted from payout at settlement
    and must be tracked to keep P&L accurate.
    """
    from fee_regime import FeeRegime
    try:
        entry = resolve_open_arb(condition_id)

        # Calculate Kalshi fee: only applies when Kalshi side wins
        kalshi_fee = 0.0
        if winning_side == "kalshi" and entry:
            # Look up the original arb_fill to get profit_locked
            _original_profit = _lookup_arb_fill_profit(condition_id)
            if _original_profit and _original_profit > 0:
                kalshi_fee = FeeRegime.kalshi_fee_usd(_original_profit, mode="taker")

        record = {
            "ts":              _now_iso(),
            "type":            "arb_outcome",
            "condition_id":    condition_id,
            "kal_ticker":      entry["kal_ticker"]      if entry else "",
            "candle_close_ts": entry["candle_close_ts"] if entry else "",
            "asset":           entry["asset"]           if entry else "",
            "tf":              entry["tf"]              if entry else "",
            "winning_side":    winning_side,
            "value_usd":       round(value_usd, 4),
            "kalshi_fee":      kalshi_fee,
        }
        # For middled trades, add the loss breakdown
        if winning_side == "middled":
            record["pm_loss_usd"] = round(pm_loss_usd, 4)
            record["kal_loss_usd"] = round(kal_loss_usd, 4)
            record["total_loss_usd"] = round(pm_loss_usd + kal_loss_usd, 4)
        _append(record)
        if winning_side == "middled":
            log.info("[OUTCOME] arb MIDDLED (both sides lost): cond=%s | pm_loss=$%.2f | kal_loss=$%.2f | total=$%.2f",
                     condition_id[:12], pm_loss_usd, kal_loss_usd, pm_loss_usd + kal_loss_usd)
        else:
            log.info("[OUTCOME] arb resolved: %s won | cond=%s | value=$%.2f | kalshi_fee=$%.4f",
                     winning_side.upper(), condition_id[:12], value_usd, kalshi_fee)
    except Exception as e:
        log.warning("log_arb_outcome failed: %s", e)


def _lookup_arb_fill_profit(condition_id: str) -> float:
    """Find the original arb_fill profit_locked for a given condition_id."""
    try:
        if not os.path.exists(_TRADES_FILE):
            return 0.0
        with open(_TRADES_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if (r.get("type") == "arb_fill" and
                            r.get("condition_id") == condition_id):
                        return float(r.get("profit_locked", 0))
                except Exception:
                    pass
    except Exception:
        pass
    return 0.0


def _lookup_arb_fill_record(condition_id: str) -> dict | None:
    """Find the original arb_fill record for a given condition_id."""
    try:
        if not os.path.exists(_TRADES_FILE):
            return None
        with open(_TRADES_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if (r.get("type") == "arb_fill" and
                            r.get("condition_id") == condition_id):
                        return r
                except Exception:
                    pass
    except Exception:
        pass
    return None


def summary():
    """Read trades.jsonl and print a strategy analysis summary."""
    if not os.path.exists(_TRADES_FILE):
        print("No trades.jsonl found.")
        return

    arb_fills = []
    arb_outcomes = []
    dir_entries = []
    dir_outcomes = []

    with open(_TRADES_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                t = r.get("type")
                if t == "arb_fill":
                    arb_fills.append(r)
                elif t == "dir_entry":
                    dir_entries.append(r)
                elif t == "dir_outcome":
                    dir_outcomes.append(r)
                elif t == "arb_outcome":
                    arb_outcomes.append(r)
            except Exception:
                pass

    print("=" * 55)
    print("TRADE LOG SUMMARY")
    print("=" * 55)

    # Arb fills
    total_locked   = sum(r.get("profit_locked", 0) for r in arb_fills)
    total_deployed = sum(r.get("total_cost_usd", 0) for r in arb_fills)
    total_proceeds = sum(r.get("proceeds_usd", 0) for r in arb_fills)
    avg_roi        = (total_locked / total_deployed * 100) if total_deployed > 0 else 0
    print(f"\n📦 ARB FILLS: {len(arb_fills)}")
    print(f"   Total deployed:      ${total_deployed:.2f}")
    print(f"   Total proceeds:      ${total_proceeds:.2f}")
    print(f"   Total locked profit: ${total_locked:.2f}")
    print(f"   Avg ROI per trade:   {avg_roi:.2f}%")
    if arb_fills:
        assets = {}
        for r in arb_fills:
            assets[r["asset"]] = assets.get(r["asset"], 0) + r.get("profit_locked", 0)
        for asset, pnl in sorted(assets.items(), key=lambda x: -x[1]):
            print(f"   {asset}: ${pnl:.2f}")
    if arb_outcomes:
        pm_wins    = [o for o in arb_outcomes if o.get("winning_side") == "pm"]
        kal_wins   = [o for o in arb_outcomes if o.get("winning_side") == "kalshi"]
        middled    = [o for o in arb_outcomes if o.get("winning_side") == "middled"]
        pm_value   = sum(o.get("value_usd", 0) for o in pm_wins)
        mid_loss   = sum(o.get("total_loss_usd", 0) for o in middled)
        resolved   = len(arb_outcomes)
        pm_wr      = len(pm_wins) / resolved * 100 if resolved else 0
        kal_wr     = len(kal_wins) / resolved * 100 if resolved else 0
        mid_wr     = len(middled) / resolved * 100 if resolved else 0
        print("\n   🔀 SIDE WIN RATES (" + str(resolved) + " resolved arbs):")
        print(f"      PM won:    {len(pm_wins):3d} ({pm_wr:.0f}%) | ${pm_value:.2f} redeemed")
        print(f"      Kalshi won:{len(kal_wins):3d} ({kal_wr:.0f}%) | capital drained to Kalshi")
        if middled:
            print(f"      MIDDLED:   {len(middled):3d} ({mid_wr:.0f}%) | -${mid_loss:.2f} total loss (both sides lost)")
    else:
        print(f"   🔀 Side win tracking: {len(arb_fills)} fills, 0 outcomes logged yet")

    # Directional
    wins   = [r for r in dir_outcomes if r.get("outcome") == "win"]
    losses = [r for r in dir_outcomes if r.get("outcome") == "loss"]
    total_dir = len(dir_outcomes)
    wr = len(wins) / total_dir * 100 if total_dir else 0
    net_pnl = sum(r.get("pnl_usd", 0) for r in dir_outcomes)
    total_risk = sum(r.get("pm_cost_usd", r.get("entry_usd", 0)) for r in dir_entries)

    print(f"\n🎯 DIRECTIONAL FILLS: {len(dir_entries)} entries")
    print(f"   {len(dir_outcomes)} outcomes resolved: {len(wins)}W / {len(losses)}L ({wr:.1f}% WR)")
    print(f"   Net P&L (resolved): ${net_pnl:.2f}")
    print(f"   Total capital deployed: ${total_risk:.2f}")

    # Intentional vs accidental
    intent_outcomes = [r for r in dir_outcomes if r.get("intentional")]
    accid_outcomes  = [r for r in dir_outcomes if not r.get("intentional")]
    if intent_outcomes:
        iw = sum(1 for r in intent_outcomes if r["outcome"] == "win")
        ip = sum(r.get("pnl_usd", 0) for r in intent_outcomes)
        print(f"   Intentional (depth-gate): {len(intent_outcomes)} | {iw}W | ${ip:.2f} P&L")
    if accid_outcomes:
        aw = sum(1 for r in accid_outcomes if r["outcome"] == "win")
        ap = sum(r.get("pnl_usd", 0) for r in accid_outcomes)
        print(f"   Accidental (Kalshi fail): {len(accid_outcomes)} | {aw}W | ${ap:.2f} P&L")

    pending = len(dir_entries) - len(dir_outcomes)
    if pending > 0:
        print(f"   Pending (no outcome yet): {pending}")

    print(f"\n💰 COMBINED P&L: ${total_locked + net_pnl:.2f}")
    print("=" * 55)


def weekly_summary(weeks: int = 1):
    """Rolling N-week P&L breakdown by asset and day."""
    if not os.path.exists(_TRADES_FILE):
        print("No trades.jsonl found.")
        return

    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(weeks=weeks)

    arbs     = []
    outcomes = []
    with open(_TRADES_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                ts = datetime.fromisoformat(r.get("ts", "").replace("Z", "+00:00"))
                if ts < cutoff:
                    continue
                if r["type"] == "arb_fill":
                    arbs.append(r)
                elif r["type"] == "dir_outcome":
                    outcomes.append(r)
            except Exception:
                pass

    label = f"Last {weeks * 7} days" if weeks > 0 else "All time"
    print("=" * 60)
    print(f"ROLLING TRADE SUMMARY — {label}")
    print("=" * 60)

    # ── Arb by asset ─────────────────────────────────────────────────────────
    by_asset: dict = {}
    for r in arbs:
        a = r.get("asset", "?")
        if a not in by_asset:
            by_asset[a] = {"fills": 0, "profit": 0.0, "cost": 0.0}
        by_asset[a]["fills"]  += 1
        by_asset[a]["profit"] += r.get("profit_locked", 0)
        by_asset[a]["cost"]   += r.get("total_cost_usd", 0)

    total_arb  = sum(r.get("profit_locked", 0) for r in arbs)
    total_cost = sum(r.get("total_cost_usd", 0) for r in arbs)
    avg_roi    = (total_arb / total_cost * 100) if total_cost > 0 else 0

    print(f"\n📦 ARB FILLS: {len(arbs)} | Locked: ${total_arb:.2f} | Avg ROI: {avg_roi:.2f}%")
    print(f"   {'Asset':<6} {'Fills':>5} {'Profit':>9} {'Avg/fill':>9} {'ROI':>7}")
    print(f"   {'─'*6} {'─'*5} {'─'*9} {'─'*9} {'─'*7}")
    for asset, d in sorted(by_asset.items(), key=lambda x: -x[1]["profit"]):
        avg  = d["profit"] / d["fills"] if d["fills"] else 0
        roi  = (d["profit"] / d["cost"] * 100) if d["cost"] > 0 else 0
        print(f"   {asset:<6} {d['fills']:>5} ${d['profit']:>8.2f} ${avg:>8.2f} {roi:>6.1f}%")

    # ── Daily arb totals ──────────────────────────────────────────────────────
    by_day: dict = {}
    for r in arbs:
        day = r.get("ts", "")[:10]
        by_day[day] = by_day.get(day, 0) + r.get("profit_locked", 0)
    if by_day:
        print(f"\n   Daily arb P&L:")
        for day in sorted(by_day):
            bar = "█" * int(by_day[day] / 2)
            print(f"   {day}  ${by_day[day]:>7.2f}  {bar}")

    # ── Directional breakdown ─────────────────────────────────────────────────
    wins       = [o for o in outcomes if o.get("outcome") == "win"]
    losses     = [o for o in outcomes if o.get("outcome") == "loss"]
    dir_pnl    = sum(o.get("pnl_usd", 0) for o in outcomes)
    wr         = len(wins) / len(outcomes) * 100 if outcomes else 0
    intents    = [o for o in outcomes if o.get("intentional")]
    accids     = [o for o in outcomes if not o.get("intentional")]
    iw = sum(1 for o in intents if o["outcome"] == "win")
    aw = sum(1 for o in accids  if o["outcome"] == "win")

    print(f"\n🎯 DIRECTIONAL: {len(wins)}W / {len(losses)}L ({wr:.1f}% WR) | P&L: ${dir_pnl:+.2f}")
    if intents:
        print(f"   Depth-gate: {iw}W/{len(intents)-iw}L | "
              f"Accidental: {aw}W/{len(accids)-aw}L")

    # WR trend: split outcomes into first half / second half
    if len(outcomes) >= 6:
        mid   = len(outcomes) // 2
        early = outcomes[:mid]
        late  = outcomes[mid:]
        wr_e  = sum(1 for o in early if o["outcome"] == "win") / len(early) * 100
        wr_l  = sum(1 for o in late  if o["outcome"] == "win") / len(late)  * 100
        trend = "↑ improving" if wr_l > wr_e else "↓ declining"
        print(f"   WR trend: {wr_e:.0f}% (early) → {wr_l:.0f}% (recent)  {trend}")

    print(f"\n💰 NET P&L: ${total_arb + dir_pnl:+.2f}  "
          f"(arb ${total_arb:+.2f} / dir ${dir_pnl:+.2f})")
    print("=" * 60)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Trade log summary")
    parser.add_argument("--week",  type=int, default=0, help="Rolling N-week summary")
    parser.add_argument("--month", action="store_true",  help="Rolling 4-week summary")
    args = parser.parse_args()
    if args.month:
        weekly_summary(weeks=4)
    elif args.week:
        weekly_summary(weeks=args.week)
    else:
        summary()
