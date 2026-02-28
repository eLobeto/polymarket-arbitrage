#!/usr/bin/env python3
"""Quick ORB 2x stop comparison via post-processing."""

import sys, os, pickle
from pathlib import Path
from collections import defaultdict

import pandas as pd

# Config
WORKSPACE = Path("/home/node/.openclaw/workspace/ticker-watch")
CACHE_DIR = WORKSPACE / "data" / "cache"
TICKERS = ["AAPL", "NVDA", "TSLA", "QQQ", "IWM"]

def load_pkl(path):
    with open(path, "rb") as f:
        return pickle.load(f)

def get_1m_dates(df1):
    if df1.empty: return []
    d = df1.copy()
    if d.index.tz is None:
        d.index = d.index.tz_localize("UTC")
    d.index = d.index.tz_convert("US/Eastern")
    return sorted(set(d.index.date.astype(str)))

def resolve_trade(entry_price, stop_price, target_price, direction, df1, entry_time_et, close_time_et):
    """Walk forward 1m bars from entry_time_et to close_time_et."""
    if df1.empty or entry_price <= 0:
        return "TIMEOUT", 0
    
    # Filter to trading window
    mask = (df1.index > entry_time_et) & (df1.index <= close_time_et)
    bars = df1.loc[mask]
    
    if bars.empty:
        return "TIMEOUT", 0
    
    for i, (_, row) in enumerate(bars.iterrows()):
        if direction == "long":
            if row["low"] <= stop_price:
                return "LOSS", -1.0  # Stop hit
            if row["high"] >= target_price:
                return "WIN", 1.0    # Target hit (4x risk)
        else:  # short
            if row["high"] >= stop_price:
                return "LOSS", -1.0
            if row["low"] <= target_price:
                return "WIN", 1.0
    
    return "TIMEOUT", 0

# Run baseline detection + 2x stop comparison
print("=" * 60)
print("ORB 2x Stop Comparison (Post-Process)")
print("=" * 60)

sys.path.insert(0, str(WORKSPACE))
from src.patterns.orb_detector import make_variation_c

baseline_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "timeouts": 0, "total_pnl": 0.0})
stop_2x_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "timeouts": 0, "total_pnl": 0.0})

# Load SPY for alignment
spy_5m = load_pkl(CACHE_DIR / "SPY_alpaca_5m.pkl")
print(f"Loaded SPY 5m: {len(spy_5m)} rows\n")

total_baseline_signals = 0
total_2x_signals = 0

# Run on each ticker
for ticker in TICKERS:
    print(f"Processing {ticker}...")
    
    p5 = CACHE_DIR / f"{ticker}_alpaca_5m.pkl"
    p1 = CACHE_DIR / f"{ticker}_alpaca_1m.pkl"
    
    if not p1.exists():
        print(f"  ⚠️ No 1m data for {ticker}")
        continue
    
    df5 = load_pkl(p5)
    df1 = load_pkl(p1)
    dates = get_1m_dates(df1)
    
    # Create detector
    detector = make_variation_c(target_rr=4.0)
    signals = detector.detect_signals(
        ticker=ticker,
        df_5m=df5,
        df_1m=df1,
        dates=dates,
        spy_5m=spy_5m,
        variation="C"
    )
    
    print(f"  {len(signals)} signals detected")
    total_baseline_signals += len(signals)
    
    # Resolve each signal with baseline stop and 2x stop
    for sig in signals:
        entry = sig["entry_price"]
        baseline_stop = sig["stop_price"]
        target = sig["target_price"]
        direction = sig["direction"]
        date = sig["date"]
        
        # Convert to ET
        entry_ts_raw = sig.get("entry_ts_utc")
        if isinstance(entry_ts_raw, str):
            entry_ts = pd.Timestamp(entry_ts_raw, tz="UTC").tz_convert("US/Eastern")
        elif entry_ts_raw.tzinfo is None:
            entry_ts = entry_ts_raw.tz_localize("UTC").tz_convert("US/Eastern")
        else:
            entry_ts = entry_ts_raw.tz_convert("US/Eastern")
        close_ts = pd.Timestamp(f"{date} 16:00:00", tz="US/Eastern")
        
        # Baseline resolution
        outcome_base, pnl_base = resolve_trade(entry, baseline_stop, target, direction, df1, entry_ts, close_ts)
        key_base = outcome_base.lower()
        if key_base not in baseline_stats[ticker]:
            baseline_stats[ticker][key_base] = 0
        baseline_stats[ticker][key_base] += 1
        baseline_stats[ticker]["total_pnl"] += pnl_base
        
        # Calculate 2x stop
        fvg_risk = abs(entry - baseline_stop)
        if direction == "long":
            stop_2x = entry - (2 * fvg_risk)
        else:
            stop_2x = entry + (2 * fvg_risk)
        
        # 2x stop resolution
        outcome_2x, pnl_2x = resolve_trade(entry, stop_2x, target, direction, df1, entry_ts, close_ts)
        key_2x = outcome_2x.lower()
        if key_2x not in stop_2x_stats[ticker]:
            stop_2x_stats[ticker][key_2x] = 0
        stop_2x_stats[ticker][key_2x] += 1
        stop_2x_stats[ticker]["total_pnl"] += pnl_2x
    
    print()

# Calculate aggregates
print("\n" + "=" * 60)
print("RESULTS SUMMARY")
print("=" * 60)

baseline_total = sum(s["wins"] + s["losses"] + s["timeouts"] for s in baseline_stats.values())
stop_2x_total = sum(s["wins"] + s["losses"] + s["timeouts"] for s in stop_2x_stats.values())

baseline_wins = sum(s["wins"] for s in baseline_stats.values())
stop_2x_wins = sum(s["wins"] for s in stop_2x_stats.values())

baseline_pnl = sum(s["total_pnl"] for s in baseline_stats.values())
stop_2x_pnl = sum(s["total_pnl"] for s in stop_2x_stats.values())

print(f"\nBASELINE (FVG Midpoint Stop):")
print(f"  Signals: {baseline_total}")
print(f"  Wins: {baseline_wins} ({100*baseline_wins/baseline_total:.1f}%)")
print(f"  EV/Trade: {baseline_pnl/baseline_total:.3f}R")

print(f"\n2x STOP (2x FVG Risk Stop):")
print(f"  Signals: {stop_2x_total}")
print(f"  Wins: {stop_2x_wins} ({100*stop_2x_wins/stop_2x_total:.1f}%)")
print(f"  EV/Trade: {stop_2x_pnl/stop_2x_total:.3f}R")

print(f"\nCHANGE:")
print(f"  WR: {100*baseline_wins/baseline_total:.1f}% → {100*stop_2x_wins/stop_2x_total:.1f}% ({100*(stop_2x_wins/stop_2x_total - baseline_wins/baseline_total):.1f}pp)")
print(f"  EV: {baseline_pnl/baseline_total:.3f}R → {stop_2x_pnl/stop_2x_total:.3f}R ({stop_2x_pnl/stop_2x_total - baseline_pnl/baseline_total:+.3f}R)")

print("\nPER-TICKER:")
print(f"\n{'Ticker':<8} {'Base WR':>10} {'Base EV':>10} {'2x WR':>10} {'2x EV':>10} {'Δ WR':>8} {'Δ EV':>8}")
print("-" * 70)
for ticker in TICKERS:
    base = baseline_stats[ticker]
    stop2 = stop_2x_stats[ticker]
    
    base_n = base["wins"] + base["losses"] + base["timeouts"]
    stop2_n = stop2["wins"] + stop2["losses"] + stop2["timeouts"]
    
    if base_n > 0 and stop2_n > 0:
        base_wr = 100 * base["wins"] / base_n
        base_ev = base["total_pnl"] / base_n
        stop2_wr = 100 * stop2["wins"] / stop2_n
        stop2_ev = stop2["total_pnl"] / stop2_n
        
        print(f"{ticker:<8} {base_wr:>9.1f}% {base_ev:>10.3f}R {stop2_wr:>9.1f}% {stop2_ev:>10.3f}R {stop2_wr-base_wr:>7.1f}pp {stop2_ev-base_ev:>7.3f}R")

print("\n" + "=" * 60)
