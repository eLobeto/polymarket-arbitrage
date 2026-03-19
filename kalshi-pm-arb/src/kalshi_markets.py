"""kalshi_markets.py — Fetch active Kalshi 15m Up/Down candle markets."""
import datetime
import logging
import re
import requests
from config import KALSHI_BASE_URL, ASSETS, WINDOW_MIN_MINUTES
import kalshi_auth

log = logging.getLogger("kalshi_mkts")
_sess = requests.Session()


def _now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def _ticker_candle_end(ticker: str, dur_minutes: int = 15) -> datetime.datetime | None:
    """
    Parse the candle END time from the Kalshi ticker string.

    Ticker format: KXBTC15M-26MAR121830-30
      26=year, MAR=month, 12=day, 1830=HHMM in **Eastern Time** (ET)

    The HHMM component is the candle END time in ET — identical to the
    close_time field the API returns (just expressed in ET instead of UTC).
    Convert to UTC; do NOT add dur_minutes (it's already the end time).

    Returns None if parsing fails.
    """
    try:
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        m = re.search(r'-(\d{2})([A-Z]{3})(\d{2})(\d{4})-', ticker)
        if not m:
            return None
        yr, mon_str, day, hhmm = m.groups()
        months = {'JAN':1,'FEB':2,'MAR':3,'APR':4,'MAY':5,'JUN':6,
                  'JUL':7,'AUG':8,'SEP':9,'OCT':10,'NOV':11,'DEC':12}
        year = 2000 + int(yr)
        hh = int(hhmm[:2])
        mm = int(hhmm[2:])
        # Interpret as Eastern Time (handles EDT/EST DST automatically)
        dt_et = datetime.datetime(year, months[mon_str], int(day), hh, mm, 0, tzinfo=ET)
        return dt_et.astimezone(datetime.timezone.utc)
    except Exception:
        return None


def _cents(val, default=50):
    v = float(val or default)
    return int(round(v * 100)) if v <= 1 else int(v)


def fetch_kalshi_markets() -> list[dict]:
    """Return active candle markets (5m/15m/1h) for all Kalshi assets within window."""
    result = []
    now = _now_utc()

    TF_DURATIONS = {"5m": 5, "15m": 15, "1h": 60}

    # Define the series to fetch (currently just 15m)
    series_to_fetch = []
    for asset in ASSETS:
        series_to_fetch.append((asset, "15m", f"KX{asset}15M"))

    for asset, tf_label, series in series_to_fetch:
        dur = TF_DURATIONS.get(tf_label, 15)
        try:
            headers = kalshi_auth.signed_headers("GET",
                f"/trade-api/v2/markets?series_ticker={series}&status=open&limit=50")
            r = _sess.get(KALSHI_BASE_URL + "/markets",
                          headers=headers,
                          params={"series_ticker": series, "status": "open", "limit": 50},
                          timeout=8)
            if not r.ok:
                log.warning("Kalshi markets fetch failed for %s/%s: %d", asset, tf_label, r.status_code)
                continue

            for mkt in r.json().get("markets", []):
                ticker = mkt.get("ticker", "")

                # KEY FIX: use candle end time parsed from ticker,
                # NOT Kalshi's close_time (which is trading deadline, not candle end)
                candle_end_dt = _ticker_candle_end(ticker, dur_minutes=dur)

                if candle_end_dt is None:
                    # Fallback: try close_time from API
                    close_str = mkt.get("close_time") or mkt.get("expiration_time", "")
                    if not close_str:
                        continue
                    try:
                        candle_end_dt = datetime.datetime.fromisoformat(
                            close_str.replace("Z", "+00:00"))
                    except Exception:
                        continue

                minutes_left = (candle_end_dt - now).total_seconds() / 60

                # Hard reject: candle already ended
                if minutes_left < 0:
                    continue
                # Window filter: must be within [WINDOW_MIN_MINUTES, 65]
                if minutes_left < WINDOW_MIN_MINUTES or minutes_left > 65:
                    continue

                yes_ask = _cents(mkt.get("yes_ask"), 50)
                no_ask  = _cents(mkt.get("no_ask"),  50)
                yes_bid = _cents(mkt.get("yes_bid"), yes_ask - 1)
                no_bid  = _cents(mkt.get("no_bid"),  no_ask  - 1)

                result.append({
                    "platform":      "kalshi",
                    "asset":         asset,
                    "timeframe":     tf_label,
                    "ticker":        ticker,
                    "series":        series,
                    "candle_end_ts": int(candle_end_dt.timestamp()),
                    "close_dt":      candle_end_dt,
                    "minutes_left":  round(minutes_left, 2),
                    "yes_cents":     yes_ask,
                    "no_cents":      no_ask,
                    "yes_bid":       yes_bid,
                    "no_bid":        no_bid,
                    "floor_strike":  float(mkt.get("floor_strike", 0)),
                })

        except Exception as e:
            log.warning("Error fetching Kalshi %s/%s markets: %s", asset, tf_label, e)

    log.info("Kalshi markets: %d active", len(result))
    return result


def fetch_kalshi_series(series_ticker: str) -> list[dict]:
    """Fetch all open markets for a specific Kalshi series."""
    try:
        path = "/trade-api/v2/markets"
        headers = kalshi_auth.signed_headers("GET", f"{path}?series_ticker={series_ticker}&status=open&limit=100")
        r = _sess.get(KALSHI_BASE_URL + "/markets",
                      headers=headers,
                      params={"series_ticker": series_ticker, "status": "open", "limit": 100},
                      timeout=10)
        if not r.ok:
            return []

        markets = r.json().get("markets", [])
        result = []
        for mkt in markets:
            result.append({
                "platform": "kalshi",
                "ticker": mkt.get("ticker"),
                "series": series_ticker,
                "title": mkt.get("title"),
                "yes_cents": _cents(mkt.get("yes_ask")),
                "no_cents": _cents(mkt.get("no_ask")),
            })
        return result
    except Exception as e:
        log.warning("Kalshi series fetch error for %s: %s", series_ticker, e)
        return []
