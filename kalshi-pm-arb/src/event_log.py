"""
event_log.py — Shared structured event log for the hourly PM summary.

All three bots write to /home/ubuntu/pm_events.jsonl on Stockholm EC2.
OpenClaw cron reads it hourly and sends a combined Telegram summary.

Event schema:
  {
    "ts":    float,        # Unix timestamp
    "bot":   str,          # "lag" | "binance-arb" | "kalshi-pm-arb"
    "event": str,          # "fill" | "win" | "loss" | "directional" | "one_sided" | "crash"
    "asset": str,          # "BTC" | "ETH" | "SOL" | "XRP"
    "side":  str,          # "up" | "down" | "yes" | "no"
    "size_usdc": float,    # dollars risked
    "profit": float,       # realized P&L in USD (0.0 if unknown at fill time)
    "note":  str,          # free-form context
  }
"""
import json
import os
import logging
import pathlib
import threading
import time

log = logging.getLogger("event_log")

EVENT_PATH = pathlib.Path(os.environ.get("PM_EVENTS_PATH",
    str(pathlib.Path(__file__).parent.parent / "logs" / "pm_events.jsonl")))
_lock = threading.Lock()


def write(
    bot: str,
    event: str,
    asset: str = "",
    side: str = "",
    size_usdc: float = 0.0,
    profit: float = 0.0,
    note: str = "",
    **extra,
):
    """Append one event to the shared event log."""
    record = {
        "ts":       time.time(),
        "bot":      bot,
        "event":    event,
        "asset":    asset,
        "side":     side,
        "size_usdc": round(size_usdc, 2),
        "profit":   round(profit, 4),
        "note":     note,
        **extra,
    }
    with _lock:
        EVENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENT_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")


def read_since(cutoff_ts: float) -> list[dict]:
    """Return all events since cutoff_ts, oldest first."""
    if not EVENT_PATH.exists():
        return []
    results = []
    with _lock:
        try:
            with open(EVENT_PATH) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        if r.get("ts", 0) >= cutoff_ts:
                            results.append(r)
                    except Exception:
                        pass
        except Exception as e:
            log.debug(f"event_log read error: {e}")
    return results


def trim(max_age_sec: float = 86400.0):
    """Prune entries older than max_age_sec (default 24h)."""
    if not EVENT_PATH.exists():
        return
    cutoff = time.time() - max_age_sec
    kept = []
    with _lock:
        try:
            with open(EVENT_PATH) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        if r.get("ts", 0) >= cutoff:
                            kept.append(line)
                    except Exception:
                        pass
            with open(EVENT_PATH, "w") as f:
                f.write("\n".join(kept) + ("\n" if kept else ""))
        except Exception as e:
            log.debug(f"event_log trim error: {e}")
