"""div_fade_monitor.py — Real-time settlement watcher for live Divergence Fade positions.

Runs as a background daemon thread inside the bot process.
Every POLL_INTERVAL_SECS it checks div_fade_positions.jsonl for positions whose
candle has closed, then checks the PM token midpoint directly:
  - mid > 0.95  → WIN  (our token is the payout winner)
  - mid < 0.05  → LOSS (our token expired worthless)
  - otherwise   → still live, check next cycle

No Kalshi auth needed — this is a PM-only directional strategy.
"""
import json
import logging
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger("div_fade_monitor")

_POSITIONS_LOG     = Path(__file__).parent.parent / "logs" / "div_fade_positions.jsonl"
_SIGNALS_LOG       = Path(__file__).parent.parent / "logs" / "div_fade_signals.jsonl"
_SIGNALS_5M_LOG    = Path(__file__).parent.parent / "logs" / "div_fade_signals_5m.jsonl"
_PM_MIDPOINT_URL   = "https://clob.polymarket.com/midpoint"

POLL_INTERVAL_SECS = 15      # check every 15s — PM markets disappear fast post-settlement
CANDLE_GRACE_SECS  = 10      # start checking 10s after candle close
GIVE_UP_SECS       = 3600    # stop retrying 1hr after candle close (PM market likely gone)

_monitor_started = False
_monitor_lock    = threading.Lock()
_file_lock       = threading.Lock()   # serialize reads/writes to positions jsonl


# ── PM settlement check ───────────────────────────────────────────────────────

_PM_GAMMA_URL = "https://gamma-api.polymarket.com/markets"


def _check_pm_token(token_id: str) -> str | None:
    """Check whether our PM token has settled.

    Tries two sources in order:
      1. CLOB midpoint  — fast, works while market is still active
      2. Gamma API      — works after CLOB delists the market post-settlement;
                          maps our token_id to its index in outcomePrices

    Returns:
      'win'   — token resolved as winner (worth ~$1)
      'loss'  — token resolved as loser (worth ~$0)
      None    — still live, not yet resolved, or fetch error (retry next cycle)
    """
    if not token_id:
        return None

    # ── 1. CLOB midpoint ─────────────────────────────────────────────────────
    try:
        r = requests.get(_PM_MIDPOINT_URL, params={"token_id": token_id}, timeout=5)
        if r.ok:
            mid = float(r.json().get("mid", -1))
            if mid > 0.95:
                return "win"
            if 0 <= mid < 0.05:
                return "loss"
            if mid >= 0:
                return None   # still trading
        # 404 or error → fall through to gamma
    except Exception as e:
        log.debug("CLOB midpoint failed for %s…: %s", token_id[:16], e)

    # ── 2. Gamma API (post-settlement fallback) ───────────────────────────────
    try:
        r = requests.get(_PM_GAMMA_URL, params={"clob_token_ids": token_id}, timeout=6)
        if not r.ok or not r.json():
            return None
        market = r.json()[0]
        token_ids    = market.get("clobTokenIds", [])
        outcome_prices = market.get("outcomePrices", [])
        if not token_ids or not outcome_prices:
            return None
        try:
            idx = token_ids.index(token_id)
            price = float(outcome_prices[idx])
            if price > 0.95:
                return "win"
            if price < 0.05:
                return "loss"
        except (ValueError, IndexError):
            pass
        return None
    except Exception as e:
        log.debug("Gamma API failed for %s…: %s", token_id[:16], e)
        return None


# ── Telegram alert ────────────────────────────────────────────────────────────

def _alert(msg: str) -> None:
    try:
        import notifier as _ntf
        _ntf._send(msg)
    except Exception as e:
        log.debug("Alert failed: %s", e)


# ── Positions file helpers ────────────────────────────────────────────────────

def _load_positions() -> list[dict]:
    positions = []
    try:
        if _POSITIONS_LOG.exists():
            with _POSITIONS_LOG.open() as f:
                for line in f:
                    line = line.strip()
                    if line:
                        positions.append(json.loads(line))
    except Exception as e:
        log.warning("Failed to load positions: %s", e)
    return positions


def _save_positions(positions: list[dict]) -> None:
    try:
        _POSITIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _POSITIONS_LOG.open("w") as f:
            for p in positions:
                f.write(json.dumps(p) + "\n")
    except Exception as e:
        log.warning("Failed to save positions: %s", e)


# ── Monitor worker ────────────────────────────────────────────────────────────

def _load_signals() -> list[dict]:
    signals = []
    try:
        if _SIGNALS_LOG.exists():
            with _SIGNALS_LOG.open() as f:
                for line in f:
                    line = line.strip()
                    if line:
                        signals.append(json.loads(line))
    except Exception as e:
        log.warning("Failed to load signals: %s", e)
    return signals


def _save_signals(signals: list[dict]) -> None:
    try:
        with _SIGNALS_LOG.open("w") as f:
            for s in signals:
                f.write(json.dumps(s) + "\n")
    except Exception as e:
        log.warning("Failed to save signals: %s", e)


def _check_dry_run_signals() -> None:
    """Resolve outcomes for dry-run signals using PM token midpoint.

    Must run in real-time (within ~1hr of candle close) — PM CLOB midpoints
    for 15-minute candle markets disappear shortly after settlement.
    Kalshi settlement is NOT used here because Kalshi (CF Benchmarks) and
    Polymarket (Chainlink) can disagree on direction, especially at divergence.
    Only signals that have pm_token_id are checked — older signals without it
    cannot be accurately resolved.
    """
    now = time.time()

    with _file_lock:
        signals = _load_signals()

    pending = [
        s for s in signals
        if s.get("outcome") is None
        and s.get("pm_token_id")
        and s.get("candle_end_ts", 0) < now - CANDLE_GRACE_SECS
    ]

    if not pending:
        return

    log.info("Div fade monitor: checking %d pending dry-run signal(s)", len(pending))
    updated = False

    for sig in pending:
        token_id   = sig.get("pm_token_id", "")
        candle_end = sig.get("candle_end_ts", 0)

        # Give up — PM market is almost certainly gone after 1hr
        if now - candle_end > GIVE_UP_SECS:
            sig["outcome"] = "no_settle"
            updated = True
            continue

        outcome = _check_pm_token(token_id)
        if outcome is None:
            continue  # still live or market not yet queryable

        sig["outcome"] = outcome
        updated = True

        log.info(
            "Dry-run signal settled via PM: %s %s → %s (div=%.2f)",
            sig.get("asset", "?"), sig.get("signal", "?"), outcome,
            sig.get("divergence", 0),
        )

    if updated:
        with _file_lock:
            _save_signals(signals)


_5M_GIVE_UP_SECS = 900   # give up on 5m signals after 15min (candle gone)


def _check_5m_signals() -> None:
    """Resolve outcomes for PM 5m dry-run signals via PM CLOB midpoint.

    Shorter TTL than 15m signals — 5m markets disappear ~10-15min post-close.
    """
    now = time.time()

    try:
        signals = []
        if _SIGNALS_5M_LOG.exists():
            with _SIGNALS_5M_LOG.open() as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            signals.append(json.loads(line))
                        except Exception:
                            pass
    except Exception as e:
        log.warning("Failed to load 5m signals: %s", e)
        return

    pending = [
        s for s in signals
        if s.get("outcome") is None
        and s.get("pm_token_id")
        and s.get("candle_end_ts", 0) < now - CANDLE_GRACE_SECS
    ]

    if not pending:
        return

    log.info("Div fade 5m monitor: checking %d pending signal(s)", len(pending))
    updated = False

    for sig in pending:
        token_id   = sig.get("pm_token_id", "")
        candle_end = sig.get("candle_end_ts", 0)

        if now - candle_end > _5M_GIVE_UP_SECS:
            sig["outcome"] = "no_settle"
            updated = True
            continue

        outcome = _check_pm_token(token_id)
        if outcome is None:
            continue

        sig["outcome"] = outcome
        updated = True
        log.info(
            "5m signal settled: %s %s → %s (div=%.2f)",
            sig.get("asset", "?"), sig.get("signal", "?"), outcome,
            sig.get("divergence", 0),
        )

    if updated:
        try:
            with _SIGNALS_5M_LOG.open("w") as f:
                for s in signals:
                    f.write(json.dumps(s) + "\n")
        except Exception as e:
            log.warning("Failed to save 5m signals: %s", e)


def _monitor_worker() -> None:
    log.info("Div fade monitor started — polling every %ds", POLL_INTERVAL_SECS)
    while True:
        try:
            _check_positions()
            _check_dry_run_signals()
            _check_5m_signals()
        except Exception as e:
            log.warning("Div fade monitor error: %s", e)
        time.sleep(POLL_INTERVAL_SECS)


def _check_positions() -> None:
    now = time.time()

    with _file_lock:
        positions = _load_positions()

    pending = [
        p for p in positions
        if p.get("outcome") is None
        and p.get("candle_end_ts", 0) < now - CANDLE_GRACE_SECS
    ]

    if not pending:
        return

    log.info("Div fade monitor: checking %d pending position(s)", len(pending))
    updated = False

    for pos in pending:
        token_id   = pos.get("token_id", "")
        candle_end = pos.get("candle_end_ts", 0)

        # Give up after 1hr — PM market likely gone / stuck
        if now - candle_end > GIVE_UP_SECS:
            log.warning("Div fade: giving up on %s…  (>1hr since candle close)", token_id[:16])
            pos["outcome"] = "no_settle"
            updated = True
            continue

        outcome = _check_pm_token(token_id)
        if outcome is None:
            continue   # still live or transient error — retry next cycle

        pos["outcome"] = outcome

        # ── P&L ──────────────────────────────────────────────────────────
        shares   = float(pos.get("shares", 0))
        cost_usd = float(pos.get("cost_usd", 0))
        asset    = pos.get("asset", "?")
        signal   = pos.get("signal", "?")
        div_gap  = abs(pos.get("divergence", 0))
        fill_c   = pos.get("fill_price_cents", pos.get("signal_price_cents", 0))
        ticker   = pos.get("kal_ticker", "")

        if outcome == "win":
            profit_usd = shares - cost_usd
            emoji      = "✅"
            pnl_str    = f"+${profit_usd:.2f}"
        else:
            profit_usd = -cost_usd
            emoji      = "❌"
            pnl_str    = f"-${cost_usd:.2f}"

        pos["profit_usd"] = round(profit_usd, 4)
        updated = True

        log.info(
            "Div fade settled: %s %s %s %s | %.1f shares @ %.1f¢  P&L=%s",
            emoji, asset, signal, outcome, shares, fill_c, pnl_str,
        )

        # Running total across all resolved positions.
        # NOTE: pos["profit_usd"] was already set above, so exclude pos from the
        # sum to avoid double-counting the current trade.
        total_pnl = sum(
            p.get("profit_usd", 0)
            for p in positions
            if p.get("outcome") in ("win", "loss") and p is not pos
        ) + profit_usd

        _alert(
            f"{emoji} <b>DIV FADE SETTLED</b> — {asset} {signal}\n"
            f"Outcome: <b>{outcome.upper()}</b>  {pnl_str}\n"
            f"{shares:.0f} shares @ {fill_c:.1f}¢  (cost ${cost_usd:.2f})\n"
            f"Oracle gap: ${div_gap:.2f}  |  <code>{ticker}</code>\n"
            f"Running P&amp;L: <b>${total_pnl:+.2f}</b>"
        )

    if updated:
        with _file_lock:
            _save_positions(positions)


# ── Public API ────────────────────────────────────────────────────────────────

def start_monitor() -> None:
    """Start the background monitor thread (idempotent — safe to call multiple times)."""
    global _monitor_started
    with _monitor_lock:
        if _monitor_started:
            return
        _monitor_started = True

    t = threading.Thread(target=_monitor_worker, daemon=True, name="div-fade-monitor")
    t.start()
    log.info("Div fade monitor thread started")
