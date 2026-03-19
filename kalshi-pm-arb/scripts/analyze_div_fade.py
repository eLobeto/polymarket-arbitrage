#!/home/ubuntu/kalshi-pm-arb/.venv/bin/python3
"""analyze_div_fade.py — Aggregate stats for the Divergence Fade strategy.

Run from ~/kalshi-pm-arb/:
  python3 scripts/analyze_div_fade.py

Outcome resolution is handled in real-time by div_fade_monitor.py (runs as a
background thread in the bot, polls every 60s, uses PM CLOB midpoint as ground
truth). This script just reads whatever outcomes the monitor already saved and
prints aggregate stats.

Any outcome=None signals older than GIVE_UP_SECS are marked no_settle (the PM
market has expired and the monitor can no longer query it).
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / "config" / ".env")

import requests

SIGNALS_LOG    = Path(__file__).parent.parent / "logs" / "div_fade_signals.jsonl"
SIGNALS_LOG_5M = Path(__file__).parent.parent / "logs" / "div_fade_signals_5m.jsonl"
POSITIONS_LOG  = Path(__file__).parent.parent / "logs" / "div_fade_positions.jsonl"

GIVE_UP_SECS = 3600   # mark no_settle if outcome still None after 1hr


# ── Live positions catch-up ───────────────────────────────────────────────────
# Real-time resolution is in div_fade_monitor.py. This is a safety net for
# positions the monitor missed while the bot was down.

def _check_pm_token(token_id: str) -> str | None:
    """Check PM token settlement via CLOB midpoint, then gamma API fallback."""
    if not token_id:
        return None
    # 1. CLOB midpoint
    try:
        r = requests.get("https://clob.polymarket.com/midpoint",
                         params={"token_id": token_id}, timeout=5)
        if r.ok:
            mid = float(r.json().get("mid", -1))
            if mid > 0.95:   return "win"
            if 0 <= mid < 0.05: return "loss"
            if mid >= 0:     return None
    except Exception:
        pass
    # 2. Gamma API (post-settlement)
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets",
                         params={"clob_token_ids": token_id}, timeout=6)
        if r.ok and r.json():
            m = r.json()[0]
            ids    = m.get("clobTokenIds", [])
            prices = m.get("outcomePrices", [])
            if ids and prices:
                idx = ids.index(token_id)
                p = float(prices[idx])
                if p > 0.95:   return "win"
                if p < 0.05:   return "loss"
    except Exception:
        pass
    return None


def _process_live_positions() -> int:
    """Catch-up resolver for live positions the monitor missed (bot was down).
    Uses PM token midpoint — no Kalshi auth needed.
    No Telegram alerts — monitor fires those in real-time.
    """
    if not POSITIONS_LOG.exists():
        return 0

    positions = []
    with POSITIONS_LOG.open() as f:
        for line in f:
            line = line.strip()
            if line:
                positions.append(json.loads(line))

    now = time.time()
    needs_resolve = [
        p for p in positions
        if p.get("outcome") is None and p.get("candle_end_ts", 0) < now - 300
    ]

    if not needs_resolve:
        return 0

    print(f"Catch-up: checking {len(needs_resolve)} unresolved live position(s)...")
    resolved_count = 0

    for pos in needs_resolve:
        token_id = pos.get("token_id", "")
        outcome  = _check_pm_token(token_id)

        if outcome is None:
            if now - pos.get("candle_end_ts", 0) > GIVE_UP_SECS:
                pos["outcome"] = "no_settle"
                resolved_count += 1
            continue

        pos["outcome"] = outcome
        shares     = float(pos.get("shares", 0))
        cost_usd   = float(pos.get("cost_usd", 0))
        profit_usd = (shares - cost_usd) if outcome == "win" else -cost_usd
        pos["profit_usd"] = round(profit_usd, 4)

        emoji   = "✅" if outcome == "win" else "❌"
        fill_c  = pos.get("fill_price_cents", pos.get("signal_price_cents", 0))
        pnl_str = f"+${profit_usd:.2f}" if outcome == "win" else f"-${cost_usd:.2f}"
        print(f"  {emoji} {pos.get('asset','?')} {pos.get('signal','?')} {outcome}: "
              f"{shares:.0f}sh @ {fill_c:.1f}¢  {pnl_str}")
        resolved_count += 1

    if resolved_count:
        with POSITIONS_LOG.open("w") as f:
            for pos in positions:
                f.write(json.dumps(pos) + "\n")

    return resolved_count


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not SIGNALS_LOG.exists():
        print("No div_fade_signals.jsonl found yet — nothing to analyze.")
        return

    signals = []
    with SIGNALS_LOG.open() as f:
        for line in f:
            line = line.strip()
            if line:
                signals.append(json.loads(line))

    if not signals:
        print("Signal log is empty.")
        return

    now = time.time()

    # ── Mark stale pending signals as no_settle ───────────────────────────────
    # Outcomes are written by div_fade_monitor.py in real-time via PM midpoint.
    # Anything still None after GIVE_UP_SECS missed the window — markets gone.
    stale = [
        s for s in signals
        if s.get("outcome") is None
        and s.get("candle_end_ts", 0) < now - GIVE_UP_SECS
    ]
    if stale:
        for sig in stale:
            sig["outcome"] = "no_settle"
        with SIGNALS_LOG.open("w") as f:
            for sig in signals:
                f.write(json.dumps(sig) + "\n")
        print(f"Marked {len(stale)} stale signal(s) as no_settle (PM market expired).")

    # ── Catch-up live positions ───────────────────────────────────────────────
    _process_live_positions()

    # ── Aggregate stats ───────────────────────────────────────────────────────
    resolved  = [s for s in signals if s.get("outcome") in ("win", "loss")]
    pending   = [s for s in signals if s.get("outcome") is None]
    no_settle = [s for s in signals if s.get("outcome") in ("no_settle", "no_ticker")]
    wins      = [s for s in resolved if s["outcome"] == "win"]
    losses    = [s for s in resolved if s["outcome"] == "loss"]

    def _sim_pnl(sigs):
        w = [s for s in sigs if s.get("outcome") == "win"]
        l = [s for s in sigs if s.get("outcome") == "loss"]
        return (
            sum(s["would_profit_usd"] for s in w),
            sum(s["would_loss_usd"]   for s in l),
        )

    def _real_pnl(sigs):
        w = [s for s in sigs if s.get("outcome") == "win"]
        l = [s for s in sigs if s.get("outcome") == "loss"]
        rp = sum(s.get("realistic_profit_usd") or s["would_profit_usd"] for s in w)
        rl = sum(s.get("realistic_stake_usd")  or s["would_loss_usd"]   for s in l)
        has_ob = sum(1 for s in sigs if s.get("realistic_profit_usd") or s.get("realistic_stake_usd"))
        return rp, rl, has_ob

    sim_profit, sim_loss = _sim_pnl(resolved)
    net_sim  = sim_profit - sim_loss
    win_rate = len(wins) / len(resolved) * 100 if resolved else 0

    real_profit, real_loss, has_ob = _real_pnl(resolved)
    net_real = real_profit - real_loss

    pm_resolved  = sum(1 for s in resolved if s.get("pm_token_id"))
    kal_resolved = len(resolved) - pm_resolved

    print(f"\n{'='*60}")
    print(f"  DIV FADE 15m DRY-RUN  |  {len(signals)} signals total")
    print(f"{'='*60}")
    print(f"  Resolved      : {len(resolved)}  ({len(wins)}W / {len(losses)}L)")
    print(f"    PM-verified : {pm_resolved}  (accurate — Binance spot oracle)")
    print(f"    Kalshi-only : {kal_resolved}  (⚠️ legacy)")
    print(f"  Pending       : {len(pending)}")
    print(f"  No settle     : {len(no_settle)}")
    print(f"  Win rate      : {win_rate:.1f}%")
    print(f"  ── Sim (perfect fill @ signal price) ──────────────")
    print(f"  Sim profit    : +${sim_profit:.2f}")
    print(f"  Sim loss      :  -${sim_loss:.2f}")
    print(f"  Net sim P&L   : ${net_sim:+.2f}")
    if has_ob:
        print(f"  ── Realistic (ob-adjusted, {has_ob} signals) ────────")
        print(f"  Real profit   : +${real_profit:.2f}")
        print(f"  Real loss     :  -${real_loss:.2f}")
        net_real_pct = f"  ({net_real/net_sim*100:.0f}% of sim)" if net_sim else ""
        print(f"  Net real P&L  : ${net_real:+.2f}{net_real_pct}")
    print(f"{'='*60}")

    # ── By signal type ────────────────────────────────────────────────────────
    for sig_type in ["PM_UP", "PM_DN"]:
        st_sigs = [s for s in resolved if s.get("signal") == sig_type]
        if not st_sigs:
            continue
        st_wins = [s for s in st_sigs if s["outcome"] == "win"]
        sp, sl  = _sim_pnl(st_sigs)
        wr      = len(st_wins) / len(st_sigs) * 100
        flag    = "✅" if wr >= 55 else ("⚠️" if wr >= 45 else "❌")
        print(f"  {sig_type}: {len(st_wins)}/{len(st_sigs)} = {wr:.0f}%  sim ${sp-sl:+.2f}  {flag}")
    print()

    # By asset + signal type
    for asset_name in ["BTC", "ETH"]:
        ar = [s for s in resolved if s["asset"] == asset_name]
        if not ar:
            continue
        for sig_type in ["PM_UP", "PM_DN"]:
            st = [s for s in ar if s.get("signal") == sig_type]
            if not st:
                continue
            sw = [s for s in st if s["outcome"] == "win"]
            sp, sl = _sim_pnl(st)
            rp, rl, ob_ct = _real_pnl(st)
            ob_note = f"  real ${rp-rl:+.2f}" if ob_ct else ""
            wr = len(sw) / len(st) * 100
            print(f"    {asset_name} {sig_type}: {len(sw)}/{len(st)} = {wr:.0f}%  sim ${sp-sl:+.2f}{ob_note}")

    # By divergence band
    bands = [(50, 150, "$50-150"), (150, 300, "$150-300"),
             (300, 500, "$300-500"), (500, 9999, ">$500")]
    has_band = any(lo <= s.get("abs_divergence", 0) < hi for s in resolved for lo, hi, _ in bands)
    if has_band:
        print(f"\n  By divergence band:")
        for lo, hi, label in bands:
            band = [s for s in resolved if lo <= s.get("abs_divergence", 0) < hi]
            if not band:
                continue
            bw = [s for s in band if s["outcome"] == "win"]
            ob_band = [s for s in band if s.get("ob_fillable_usd") is not None]
            fill_note = ""
            if ob_band:
                avg_fill = sum(
                    min(s["ob_fillable_usd"], s["would_stake_usd"]) / s["would_stake_usd"]
                    for s in ob_band
                ) / len(ob_band) * 100
                fill_note = f"  avg fill {avg_fill:.0f}%"
            print(f"    {label:>10}: {len(bw)}/{len(band)} ({len(bw)/len(band)*100:.0f}%){fill_note}")

    # ── Entry price buckets ───────────────────────────────────────────────────
    # Breakeven WR ≈ entry price (in decimal). Cheap entries survive lower WR.
    price_buckets = [("<25¢", 0, 25), ("25-40¢", 25, 40), ("40-50¢", 40, 50), ("50-60¢", 50, 60), ("60¢+", 60, 999)]
    price_key = "ob_avg_fill_cents"
    has_prices = any(s.get(price_key) or s.get("pm_price_cents") for s in resolved)
    if has_prices:
        print(f"\n  By entry price  (breakeven WR ≈ entry price):")
        for label, lo, hi in price_buckets:
            bucket = [s for s in resolved
                      if lo <= (s.get(price_key) or s.get("pm_price_cents", 0)) < hi]
            if not bucket:
                continue
            bw  = [s for s in bucket if s["outcome"] == "win"]
            wr  = len(bw) / len(bucket) * 100
            mid = (lo + hi) / 2 if hi < 999 else lo
            pnl = sum(
                s.get("would_profit_usd", 0) if s["outcome"] == "win"
                else -s.get("would_loss_usd", 0)
                for s in bucket
            )
            flag = "✅" if wr > mid else ("⚠️" if wr > mid * 0.85 else "❌")
            print(f"    {label:>7}: {len(bw)}/{len(bucket)} = {wr:.0f}%  sim ${pnl:+.0f}  BE≈{mid:.0f}%  {flag}")

    print()

    # ── 5m signals summary ───────────────────────────────────────────────────
    if SIGNALS_LOG_5M.exists():
        sigs_5m = []
        with SIGNALS_LOG_5M.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    sigs_5m.append(json.loads(line))

        if sigs_5m:
            # Mark stale pending
            stale_5m = [s for s in sigs_5m if s.get("outcome") is None and s.get("candle_end_ts", 0) < now - GIVE_UP_SECS]
            if stale_5m:
                for s in stale_5m:
                    s["outcome"] = "no_settle"
                with SIGNALS_LOG_5M.open("w") as f:
                    for s in sigs_5m:
                        f.write(json.dumps(s) + "\n")

            res_5m  = [s for s in sigs_5m if s.get("outcome") in ("win", "loss")]
            pend_5m = [s for s in sigs_5m if s.get("outcome") is None]
            w5m     = [s for s in res_5m if s["outcome"] == "win"]
            l5m     = [s for s in res_5m if s["outcome"] == "loss"]
            wr_5m   = len(w5m) / len(res_5m) * 100 if res_5m else 0
            sp5, sl5 = _sim_pnl(res_5m)

            print(f"\n{'='*60}")
            print(f"  DIV FADE 5m DRY-RUN  |  {len(sigs_5m)} signals total")
            print(f"{'='*60}")
            print(f"  Resolved      : {len(res_5m)}  ({len(w5m)}W / {len(l5m)}L)")
            print(f"  Pending       : {len(pend_5m)}")
            print(f"  Win rate      : {wr_5m:.1f}%  sim ${sp5-sl5:+.2f}")

            for sig_type in ["PM_UP", "PM_DN"]:
                st = [s for s in res_5m if s.get("signal") == sig_type]
                if not st:
                    continue
                sw = [s for s in st if s["outcome"] == "win"]
                sp, sl = _sim_pnl(st)
                wr = len(sw) / len(st) * 100
                flag = "✅" if wr >= 55 else ("⚠️" if wr >= 45 else "❌")
                print(f"  {sig_type}: {len(sw)}/{len(st)} = {wr:.0f}%  sim ${sp-sl:+.2f}  {flag}")

            for asset_name in ["BTC", "ETH"]:
                for sig_type in ["PM_UP", "PM_DN"]:
                    st = [s for s in res_5m if s["asset"] == asset_name and s.get("signal") == sig_type]
                    if not st:
                        continue
                    sw = [s for s in st if s["outcome"] == "win"]
                    sp, sl = _sim_pnl(st)
                    wr = len(sw) / len(st) * 100
                    print(f"    {asset_name} {sig_type}: {len(sw)}/{len(st)} = {wr:.0f}%  sim ${sp-sl:+.2f}")
            print(f"{'='*60}")

    # ── Live positions summary ────────────────────────────────────────────────
    if POSITIONS_LOG.exists():
        live_pos = []
        with POSITIONS_LOG.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    live_pos.append(json.loads(line))
        if live_pos:
            l_resolved = [p for p in live_pos if p.get("outcome") in ("win", "loss")]
            l_wins     = [p for p in l_resolved if p["outcome"] == "win"]
            l_losses   = [p for p in l_resolved if p["outcome"] == "loss"]
            l_pending  = [p for p in live_pos   if p.get("outcome") is None]
            l_pnl      = sum(p.get("profit_usd", 0) for p in l_resolved)
            l_wr       = len(l_wins) / len(l_resolved) * 100 if l_resolved else 0
            print(f"{'='*56}")
            print(f"  LIVE POSITIONS  |  {len(live_pos)} total  ({len(l_pending)} pending)")
            print(f"{'='*56}")
            print(f"  Resolved  : {len(l_resolved)}  ({len(l_wins)}W / {len(l_losses)}L)  WR {l_wr:.0f}%")
            print(f"  Net P&L   : ${l_pnl:+.2f}")
            print(f"{'='*56}")
            print()


if __name__ == "__main__":
    main()
