"""pm_markets.py — Fetch active Polymarket 5m/15m Up/Down candle markets.

Uses direct slug construction (btc-updown-15m-{start_ts}) rather than
scanning the full Gamma events list — exact same approach as oracle-lag-bot.
"""
import datetime
import logging
import time
import requests
from config import ASSETS, GAMMA_API_URL, PM_CLOB_URL, WINDOW_MIN_MINUTES

log = logging.getLogger("pm_mkts")
_sess = requests.Session()

WINDOW_MAX_MINUTES = 30.0


def _fetch_intraday(asset: str) -> list[dict]:
    now_ts   = int(time.time())
    slug_pfx = asset.lower()  # "btc", "eth", "sol"
    result   = []

    for tf, dur in [("5m", 300), ("15m", 900), ("1h", 3600)]:
        cur_start  = (now_ts // dur) * dur
        candidates = [cur_start, cur_start - dur]

        for start_ts in candidates:
            end_ts   = start_ts + dur
            mins_left = (end_ts - now_ts) / 60.0

            if not (WINDOW_MIN_MINUTES <= mins_left <= WINDOW_MAX_MINUTES):
                continue

            slug = f"{slug_pfx}-updown-{tf}-{start_ts}"
            try:
                r = _sess.get(f"{GAMMA_API_URL}/events",
                              params={"slug": slug}, timeout=8)
                r.raise_for_status()
                events = r.json()
            except Exception as e:
                log.debug("PM slug fetch error %s/%s: %s", asset, slug, e)
                continue

            if not events:
                continue

            ev = events[0]
            end_raw = ev.get("endDate", "")
            try:
                end_dt = datetime.datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            except Exception:
                end_dt = datetime.datetime.fromtimestamp(end_ts, tz=datetime.timezone.utc)

            for m in ev.get("markets", []):
                liq = float(m.get("liquidity", 0))
                if liq < 100:
                    continue
                cid = m.get("conditionId", "")
                if not cid:
                    continue

                up_cents = dn_cents = None
                up_token = dn_token = None
                try:
                    rc = _sess.get(f"{PM_CLOB_URL}/markets/{cid}", timeout=5)
                    if rc.ok:
                        for tok in rc.json().get("tokens", []):
                            p       = float(tok.get("price", 0)) * 100
                            outcome = tok.get("outcome", "").lower()
                            tid     = tok.get("token_id", "")
                            if "up" in outcome:
                                up_cents = p; up_token = tid
                            elif "down" in outcome:
                                dn_cents = p; dn_token = tid
                except Exception:
                    pass

                if up_cents is None or dn_cents is None:
                    continue

                result.append({
                    "platform":      "polymarket",
                    "asset":         asset,
                    "timeframe":     tf,
                    "ticker":        slug,
                    "condition_id":  cid,
                    "candle_end_ts": end_ts,
                    "close_dt":      end_dt,
                    "minutes_left":  round(mins_left, 2),
                    "up_cents":      round(up_cents, 2),
                    "dn_cents":      round(dn_cents, 2),
                    "up_token_id":   up_token,
                    "dn_token_id":   dn_token,
                    "liquidity":     liq,
                })

    return result


def fetch_pm_markets() -> list[dict]:
    result = []
    for asset in ASSETS:
        result.extend(_fetch_intraday(asset))
    log.info("PM markets: %d active", len(result))
    return result

def fetch_pm_event(slug: str) -> dict | None:
    """Fetch a single Polymarket event by slug."""
    try:
        r = _sess.get(f"{GAMMA_API_URL}/events", params={"slug": slug}, timeout=10)
        r.raise_for_status()
        events = r.json()
        return events[0] if events else None
    except Exception as e:
        log.warning("PM event fetch error for slug %s: %s", slug, e)
        return None
