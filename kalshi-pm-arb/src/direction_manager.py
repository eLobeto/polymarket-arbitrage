"""
direction_manager.py — Smart hold/cut for accidental one-sided PM positions.

Logic: After a one-sided fill, evaluate each cycle whether Binance has moved
against our position. If it has and there's still time left, sell back the
PM tokens immediately to limit the loss.

Cut trigger:
  - Binance moved > CUT_PCT_THRESHOLD % AGAINST our direction
  - AND > MIN_SECS_LEFT seconds remain in the candle (not worth cutting at T-60s)
  - AND not already cut

Hold logic:
  - Move is neutral or confirming → hold to resolution (full $1 payout)
  - < MIN_SECS_LEFT remains → hold (cutting now would cost more in spread than we save)

Candle open price: fetched from Binance klines API using candle start time
derived from the Kalshi ticker format (e.g. KXBTC15M-26MAR112000-00).
"""
import time
import logging
import requests
import trade_logger as _tl
from datetime import datetime, timedelta, timezone

log = logging.getLogger("dir_mgr")

CUT_PCT_THRESHOLD = 0.35   # ¢% move against position that triggers cut
MIN_SECS_LEFT     = 120    # don't cut if < 2 min remain (too late, eat the spread)
CHECK_INTERVAL    = 30     # re-evaluate each position at most every 30s

# Kalshi tickers are in ET (New York time)
_ET = timezone(timedelta(hours=-4))  # EDT (UTC-4)

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
    "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
    "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_candle_end(kal_ticker: str, timeframe: str) -> datetime | None:
    """
    Parse candle close time from Kalshi ticker.
    Format: KXBTC15M-26MAR112000-00
      → year=2026, month=MAR, day=11, candle_open=20:00, tf=15m → close=20:15
    """
    try:
        parts     = kal_ticker.split("-")
        dt_part   = parts[1]              # e.g. "26MAR112000"
        year      = 2000 + int(dt_part[:2])
        rest      = dt_part[2:]           # "MAR112000"
        month     = MONTHS[rest[:3]]
        rest2     = rest[3:]              # "112000"
        time_str  = rest2[-4:]            # "2000"
        day       = int(rest2[:-4])       # "11"
        hour      = int(time_str[:2])
        minute    = int(time_str[2:])
        tf_min    = 15 if "15" in timeframe else 5
        open_dt = datetime(year, month, day, hour, minute, tzinfo=_ET).astimezone(timezone.utc)
        return open_dt + timedelta(minutes=tf_min)
    except Exception as e:
        log.debug("Candle end parse failed for %s: %s", kal_ticker, e)
        return None


def _get_candle_open_price(asset: str, timeframe: str, candle_end: datetime) -> float | None:
    """Fetch candle open price from Binance klines."""
    try:
        tf_min      = 15 if "15" in timeframe else 5
        candle_open = candle_end - timedelta(minutes=tf_min)
        interval    = "15m" if tf_min == 15 else "5m"
        symbol      = f"{asset}USDT"
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={
                "symbol":    symbol,
                "interval":  interval,
                "startTime": int(candle_open.timestamp() * 1000),
                "limit":     1,
            },
            timeout=5,
        )
        if r.ok and r.json():
            return float(r.json()[0][1])  # index 1 = open price
    except Exception as e:
        log.debug("Binance kline fetch failed (%s %s): %s", asset, timeframe, e)
    return None


def _get_current_price(asset: str) -> float | None:
    """Fetch current Binance spot price via REST."""
    try:
        r = requests.get(
            'https://api.binance.com/api/v3/ticker/price',
            params={'symbol': f'{asset}USDT'},
            timeout=5,
        )
        if r.ok:
            return float(r.json()['price'])
    except Exception as e:
        log.debug('Binance price fetch failed (%s): %s', asset, e)
    return None


class DirectionManager:
    """
    Evaluates open one-sided PM positions each cycle.
    Calls sell_fn to cut a position when Binance confirms it's going wrong.

    sell_fn: callable(token_id: str, shares: float) → dict | None
    binance_prices: dict[asset_str, float] — live Binance spot prices
    """

    def __init__(self, sell_fn):
        self._sell     = sell_fn
        self._last_chk: dict[str, float] = {}   # token_id → last evaluated ts
        self._cut:      set[str]          = set()  # already cut, don't retry

    def evaluate(self, positions: dict[str, dict]) -> list[str]:
        """
        positions: directional_positions from main loop (pm_token_id → pos_dict)
        Returns: list of pm_token_ids that were cut this cycle.
        """
        cut_this_cycle = []
        now_ts = time.time()
        now_dt = datetime.now(timezone.utc)

        for token_id, pos in list(positions.items()):
            if token_id in self._cut:
                continue  # already cut, outcome checker will clean it up

            # Rate-limit per-position checks
            if now_ts - self._last_chk.get(token_id, 0) < CHECK_INTERVAL:
                continue
            self._last_chk[token_id] = now_ts

            try:
                asset     = pos.get("asset", "BTC")
                tf        = pos.get("timeframe", "15m")
                side      = pos.get("pm_side", "UP")   # "UP" or "DOWN"
                shares    = pos.get("pm_shares", pos.get("contracts", 0))
                kal_ticker = pos.get("kal_ticker", "")

                candle_end = _parse_candle_end(kal_ticker, tf)
                if candle_end is None:
                    continue

                secs_left = (candle_end - now_dt).total_seconds()
                if secs_left < MIN_SECS_LEFT:
                    log.debug("[DIR_MGR] %s %s %s: %.0fs left < %ds — holding to resolution",
                              asset, tf, side, secs_left, MIN_SECS_LEFT)
                    continue
                if secs_left < 0:
                    continue  # already past close

                # Get candle open price
                open_price = _get_candle_open_price(asset, tf, candle_end)
                if open_price is None:
                    continue

                # Get live Binance spot (REST — no WS in this bot)
                spot = _get_current_price(asset)
                if not spot:
                    continue

                pct_move = (spot - open_price) / open_price * 100

                # Is Binance moving against our position?
                if side == "UP":
                    against = pct_move < -CUT_PCT_THRESHOLD
                else:
                    against = pct_move > CUT_PCT_THRESHOLD

                log.info(
                    "[DIR_MGR] %s %s %s: spot=%.4f open=%.4f move=%+.3f%% secs_left=%.0f → %s",
                    asset, tf, side, spot, open_price, pct_move, secs_left,
                    "✂ CUT" if against else "hold",
                )

                if against:
                    log.warning(
                        "[DIR_MGR] Cutting %s %s %s — Binance %.3f%% against, %.0fs left",
                        asset, side, tf, abs(pct_move), secs_left,
                    )
                    result = self._sell(token_id, shares)
                    if result:
                        recovered = result.get("cost", 0)
                        log.info("[DIR_MGR] Cut ✓ — recovered $%.2f from %s", recovered, token_id[:12])
                        self._cut.add(token_id)
                        cut_this_cycle.append(token_id)
                        cut_pnl = recovered - pos.get("usd", 0)
                        _tl.log_dir_outcome(pos, cut_pnl, won=(cut_pnl >= 0))

                        import notifier as _ntf
                        _ntf._send(
                            f"✂️ <b>[DIR CUT]</b> {asset} {tf} {side}\n"
                            f"Binance {pct_move:+.2f}% against — selling back\n"
                            f"Recovered: <b>${recovered:.2f}</b> | {secs_left:.0f}s left"
                        )
                    else:
                        log.warning("[DIR_MGR] Cut FAILED for %s — holding", token_id[:12])

            except Exception as e:
                log.warning("[DIR_MGR] Error evaluating %s: %s", token_id[:12], e)

        return cut_this_cycle
