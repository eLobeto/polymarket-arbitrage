#!/usr/bin/env python3
"""Analyze PM 5-minute Divergence Fade paper signals.

Usage: python3 scripts/analyze_div_fade_5m.py [--asset ETH|BTC]
"""
import json
import sys
from pathlib import Path
from collections import defaultdict

SIGNALS_LOG = Path(__file__).parent.parent / "logs" / "div_fade_signals_5m.jsonl"

asset_filter = None
for i, arg in enumerate(sys.argv[1:]):
    if arg == "--asset" and i + 1 < len(sys.argv) - 1:
        asset_filter = sys.argv[i + 2].upper()

signals = []
if SIGNALS_LOG.exists():
    with SIGNALS_LOG.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    signals.append(json.loads(line))
                except Exception:
                    pass

if asset_filter:
    signals = [s for s in signals if s.get("asset") == asset_filter]

# PM_DN has no structural edge (reversal bet, 7-9% WR) — PM_UP only
signals   = [s for s in signals if s.get("signal") == "PM_UP"]
resolved  = [s for s in signals if s.get("outcome") in ("win", "loss")]
pending   = [s for s in signals if s.get("outcome") is None]
no_settle = [s for s in signals if s.get("outcome") == "no_settle"]

total_wins   = sum(1 for s in resolved if s.get("outcome") == "win")
total_losses = sum(1 for s in resolved if s.get("outcome") == "loss")
total        = len(resolved)
overall_wr   = total_wins / total * 100 if total else 0

sim_pnl = sum(
    s.get("would_profit_usd", 0) if s.get("outcome") == "win"
    else -s.get("would_loss_usd", 0)
    for s in resolved
)

print(f"═══ PM 5m Div Fade Paper Analysis {'— ' + asset_filter if asset_filter else '(all assets)'} ═══")
print(f"Total signals: {len(signals)}  |  Resolved: {total}  |  Pending: {len(pending)}  |  No-settle: {len(no_settle)}")
print(f"Overall WR: {total_wins}W / {total} = {overall_wr:.1f}%  |  Sim P&L: ${sim_pnl:+.2f}")
print()

# ── By asset ────────────────────────────────────────────────────────────────
if not asset_filter:
    print("── By Asset ──")
    for asset in ("ETH", "BTC"):
        subs = [s for s in resolved if s.get("asset") == asset]
        w = sum(1 for s in subs if s.get("outcome") == "win")
        wr = w / len(subs) * 100 if subs else 0
        pnl = sum(
            s.get("would_profit_usd", 0) if s.get("outcome") == "win"
            else -s.get("would_loss_usd", 0)
            for s in subs
        )
        print(f"  {asset}: {w}W / {len(subs)} = {wr:.1f}%  |  Sim P&L: ${pnl:+.2f}")
    print()

# ── Divergence range breakdown ───────────────────────────────────────────────
print("── Divergence Range Breakdown ──")

# Pick buckets based on asset
if asset_filter == "BTC":
    buckets = [(25, 50), (50, 75), (75, 100), (100, 150), (150, 999)]
elif asset_filter == "ETH":
    buckets = [(3, 5), (5, 7), (7, 10), (10, 15), (15, 999)]
else:
    # Show ETH and BTC separately with their own buckets
    for asset, bkts in [("ETH", [(3,5),(5,7),(7,10),(10,999)]),
                        ("BTC", [(25,50),(50,75),(75,100),(100,999)])]:
        sub = [s for s in resolved if s.get("asset") == asset]
        if not sub:
            continue
        print(f"\n  {asset}:")
        print(f"  {'Range':>10} | {'W':>4} {'L':>4} {'Total':>6} | {'WR%':>6} | {'Sim P&L':>10}")
        print(f"  {'-'*52}")
        for lo, hi in bkts:
            ss = [s for s in sub if lo <= abs(s.get("divergence", 0)) < hi]
            w  = sum(1 for s in ss if s.get("outcome") == "win")
            l  = len(ss) - w
            wr = w / len(ss) * 100 if ss else 0
            pnl = sum(
                s.get("would_profit_usd", 0) if s.get("outcome") == "win"
                else -s.get("would_loss_usd", 0)
                for s in ss
            )
            label = f"${lo}-${hi}" if hi < 999 else f"${lo}+"
            print(f"  {label:>10} | {w:>4} {l:>4} {len(ss):>6} | {wr:>6.1f}% | ${pnl:>+9.2f}")
    print()
    buckets = []  # already printed per-asset

if buckets:
    print(f"  {'Range':>10} | {'W':>4} {'L':>4} {'Total':>6} | {'WR%':>6} | {'Sim P&L':>10}")
    print(f"  {'-'*52}")
    for lo, hi in buckets:
        ss = [s for s in resolved if lo <= abs(s.get("divergence", 0)) < hi]
        w  = sum(1 for s in ss if s.get("outcome") == "win")
        l  = len(ss) - w
        wr = w / len(ss) * 100 if ss else 0
        pnl = sum(
            s.get("would_profit_usd", 0) if s.get("outcome") == "win"
            else -s.get("would_loss_usd", 0)
            for s in ss
        )
        label = f"${lo}-${hi}" if hi < 999 else f"${lo}+"
        print(f"  {label:>10} | {w:>4} {l:>4} {len(ss):>6} | {wr:>6.1f}% | ${pnl:>+9.2f}")

# ── Minutes left distribution ────────────────────────────────────────────────
print()
print("── Entry Price Buckets  (breakeven WR ≈ entry price) ──")
price_key = "ob_avg_fill_cents"
price_buckets = [("<25¢", 0, 25), ("25-40¢", 25, 40), ("40-50¢", 40, 50), ("50-60¢", 50, 60), ("60¢+", 60, 999)]
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
    print(f"  {label:>7}: {len(bw)}/{len(bucket)} = {wr:.0f}%  sim ${pnl:+.0f}  BE≈{mid:.0f}%  {flag}")

print()
print("── 5m Candle Time-Left When Signal Fired (wins only) ──")
win_times = sorted([s.get("candle_minutes_left", 0) for s in resolved if s.get("outcome") == "win"])
if win_times:
    avg = sum(win_times) / len(win_times)
    med = win_times[len(win_times) // 2]
    gt3 = sum(1 for t in win_times if t > 3)
    gt2 = sum(1 for t in win_times if t > 2)
    print(f"  avg={avg:.1f}m  median={med:.1f}m")
    print(f"  >3m left: {gt3}/{len(win_times)} = {gt3/len(win_times)*100:.0f}%")
    print(f"  >2m left: {gt2}/{len(win_times)} = {gt2/len(win_times)*100:.0f}%")
else:
    print("  No resolved wins yet.")

# ── Compare vs 15m ───────────────────────────────────────────────────────────
SIGNALS_15M = Path(__file__).parent.parent / "logs" / "div_fade_signals.jsonl"
if SIGNALS_15M.exists():
    sigs_15m = []
    with SIGNALS_15M.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    sigs_15m.append(json.loads(line))
                except Exception:
                    pass
    if asset_filter:
        sigs_15m = [s for s in sigs_15m if s.get("asset") == asset_filter]
    res_15m = [s for s in sigs_15m if s.get("outcome") in ("win", "loss")]
    w_15m   = sum(1 for s in res_15m if s.get("outcome") == "win")
    wr_15m  = w_15m / len(res_15m) * 100 if res_15m else 0
    print()
    print("── vs 15m Benchmark ──")
    print(f"  15m: {w_15m}W / {len(res_15m)} = {wr_15m:.1f}% resolved")
    print(f"   5m: {total_wins}W / {total} = {overall_wr:.1f}% resolved")
