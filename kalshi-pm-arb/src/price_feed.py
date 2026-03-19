"""price_feed.py — Dual WebSocket price state for PM and Kalshi."""
import base64
import datetime
import json
import logging
import threading
import time
import requests
import websocket
from config import PM_WS_URL, KALSHI_WS_URL, PM_CLOB_URL, KALSHI_BASE_URL
import kalshi_auth

log = logging.getLogger("price_feed")

# Thread-safe price state
_lock        = threading.Lock()
_pm_prices   = {}   # token_id -> float cents
_pm_ts       = {}   # token_id -> last update timestamp
_kal_prices  = {}   # ticker -> {"yes": float, "no": float}
_kal_ts      = {}   # ticker -> last update timestamp

PRICE_TTL_SECS = 12  # if WS data is older than this, fall back to REST

# Track subscribed tokens/tickers
_pm_tokens    = set()
_kal_tickers  = set()

# Live WS references for late subscriptions
_pm_ws_ref:  websocket.WebSocketApp | None = None
_kal_ws_ref: websocket.WebSocketApp | None = None


# ── Polymarket ────────────────────────────────────────────────────────────────

def _pm_ws_thread():
    def on_open(ws):
        global _pm_ws_ref
        _pm_ws_ref = ws
        if _pm_tokens:
            ws.send(json.dumps({
                "assets_ids": list(_pm_tokens),
                "type": "subscribe",
            }))
            log.info("PM WS subscribed to %d tokens", len(_pm_tokens))

    def on_message(ws, msg):
        try:
            data = json.loads(msg)
            if isinstance(data, list):
                for item in data:
                    _handle_pm_msg(item)
            else:
                _handle_pm_msg(data)
        except Exception as e:
            log.debug("PM WS parse error: %s", e)

    def on_error(ws, err):
        log.warning("PM WS error: %s", err)

    def on_close(ws, *_):
        global _pm_ws_ref
        _pm_ws_ref = None
        log.info("PM WS closed — reconnecting in 5s")
        time.sleep(5)
        _pm_ws_thread()

    ws = websocket.WebSocketApp(
        PM_WS_URL,
        on_open=on_open, on_message=on_message,
        on_error=on_error, on_close=on_close,
    )
    ws.run_forever(ping_interval=30, ping_timeout=10)


def _handle_pm_msg(data: dict):
    event_type = data.get("event_type") or data.get("type", "")
    if event_type not in ("price_change", "book"):
        return
    asset_id = data.get("asset_id") or data.get("market", "")
    if not asset_id:
        return
    # mid = (best_bid + best_ask) / 2
    mid_raw = data.get("mid_price") or data.get("midpoint")
    if mid_raw is None:
        bid = float(data.get("best_bid", 0) or 0)
        ask = float(data.get("best_ask", 1) or 1)
        mid_raw = (bid + ask) / 2
    mid = float(mid_raw)
    if mid > 1:
        mid /= 100  # already in cents
    with _lock:
        _pm_prices[asset_id] = round(mid * 100, 2)
        _pm_ts[asset_id] = time.time()


# ── Kalshi ────────────────────────────────────────────────────────────────────

def _kal_ws_thread():
    """Kalshi WS: auth goes in the HTTP upgrade headers, then subscribe after connect."""

    def _make_headers():
        """Build RSA-signed headers for the WS upgrade request."""
        return kalshi_auth.signed_headers("GET", "/trade-api/ws/v2")

    def on_open(ws):
        global _kal_ws_ref
        _kal_ws_ref = ws
        if _kal_tickers:
            ws.send(json.dumps({
                "id": 1, "cmd": "subscribe",
                "params": {
                    "channels": ["market_ticker"],
                    "market_tickers": list(_kal_tickers),
                }
            }))
            log.info("Kalshi WS subscribed to %d tickers", len(_kal_tickers))
        else:
            log.info("Kalshi WS connected (no tickers yet — will subscribe on next refresh)")

    def on_message(ws, msg):
        try:
            data = json.loads(msg)
            _handle_kal_msg(data)
        except Exception as e:
            log.debug("Kalshi WS parse error: %s", e)

    def on_error(ws, err):
        log.warning("Kalshi WS error: %s", err)

    def on_close(ws, *_):
        global _kal_ws_ref
        _kal_ws_ref = None
        log.info("Kalshi WS closed — reconnecting in 5s")
        time.sleep(5)
        _kal_ws_thread()

    try:
        headers = _make_headers()
    except Exception as e:
        log.warning("Kalshi WS header build failed: %s — retrying in 10s", e)
        time.sleep(10)
        _kal_ws_thread()
        return

    ws = websocket.WebSocketApp(
        KALSHI_WS_URL,
        header=headers,
        on_open=on_open, on_message=on_message,
        on_error=on_error, on_close=on_close,
    )
    ws.run_forever(ping_interval=30, ping_timeout=10)


def _handle_kal_msg(data: dict):
    msg_type = data.get("type", "")
    if msg_type != "market_ticker":
        return
    msg = data.get("msg", {})
    ticker = msg.get("market_ticker", "")
    if not ticker:
        return
    # Use ASK prices (what you'd actually pay as a taker), not last-trade prices.
    # yes_price = last trade price (mid); yes_ask = what a buyer pays. 
    # On thin Kalshi books, bid-ask spread can be 20-30¢ — using last-trade
    # causes the matcher to see phantom arbs that evaporate at execution.
    yes_ask = (msg.get("yes_ask_dollars") or msg.get("yes_ask") or msg.get("yes_price"))
    no_ask  = (msg.get("no_ask_dollars")  or msg.get("no_ask")  or msg.get("no_price"))
    if yes_ask is None and no_ask is None:
        return  # nothing useful
    if yes_ask is None:
        fp = float(no_ask)
        yes_ask = 1.0 - fp if fp <= 1 else 100 - fp
    if no_ask is None:
        fp = float(yes_ask)
        no_ask = 1.0 - fp if fp <= 1 else 100 - fp
    yes_cents = float(yes_ask) if float(yes_ask) > 1 else float(yes_ask) * 100
    no_cents  = float(no_ask)  if float(no_ask)  > 1 else float(no_ask)  * 100
    with _lock:
        _kal_prices[ticker] = {"yes": round(yes_cents, 2), "no": round(no_cents, 2)}
        _kal_ts[ticker] = time.time()


# ── Public API ────────────────────────────────────────────────────────────────

def subscribe_pm(token_ids: list[str]):
    new = set(token_ids) - _pm_tokens
    with _lock:
        _pm_tokens.update(token_ids)
    if new and _pm_ws_ref:
        try:
            _pm_ws_ref.send(json.dumps({"assets_ids": list(new), "type": "subscribe"}))
            log.info("PM WS subscribed to %d new tokens", len(new))
        except Exception as e:
            log.debug("PM WS send error: %s", e)


def subscribe_kalshi(tickers: list[str]):
    new = set(tickers) - _kal_tickers
    with _lock:
        _kal_tickers.update(tickers)
    if new and _kal_ws_ref:
        try:
            _kal_ws_ref.send(json.dumps({
                "id": 2, "cmd": "subscribe",
                "params": {"channels": ["market_ticker"], "market_tickers": list(new)},
            }))
            log.info("Kalshi WS subscribed to %d new tickers", len(new))
        except Exception as e:
            log.debug("Kalshi WS send error: %s", e)


def get_pm_price(token_id: str) -> float | None:
    """Return live PM price in cents via REST midpoint (always fresh).
    
    The PM WebSocket only sends price_change events on TRADES, not on order book
    moves. In a strongly directional market, the book can shift from 50¢ to 75¢
    with no new trades — making WS cache useless for signal quality. Always use
    REST /midpoint for accurate current order book mid.
    """
    try:
        r = requests.get(f"{PM_CLOB_URL}/midpoint?token_id={token_id}", timeout=4)
        mid = float(r.json().get("mid", 0))
        val = round(mid * 100, 2)
        with _lock:
            _pm_prices[token_id] = val
            _pm_ts[token_id] = time.time()
        return val
    except Exception:
        # Fall back to cache only if REST fails
        with _lock:
            return _pm_prices.get(token_id)


def get_kalshi_price(ticker: str) -> dict | None:
    """Return live Kalshi ask prices {yes, no} in cents.

    Always hits REST /markets/{ticker} for the real bid/ask.
    The WS market_ticker channel sends yes_price (last trade mid), NOT yes_ask.
    Bid-ask spreads on thin Kalshi books are 20-30¢ — using last-trade as a
    proxy for ask causes the matcher to see phantom arbs that evaporate at
    execution (signal=46¢ vs live=64¢).

    WS cache is kept as last-resort fallback only (REST timeout/error).
    We have 4 tickers × 5s cycle = negligible REST traffic.
    """
    with _lock:
        cached = _kal_prices.get(ticker)

    def _cprice(m_dict, key):
        v = m_dict.get(key + "_dollars") or m_dict.get(key)
        if not v: return None
        fv = float(v)
        return int(round(fv * 100)) if fv <= 1 else int(fv)

    # Always try REST first for accurate ask prices
    try:
        from kalshi_auth import signed_headers
        path = f"/trade-api/v2/markets/{ticker}"
        h = signed_headers("GET", path)
        r = requests.get(KALSHI_BASE_URL + f"/markets/{ticker}", headers=h, timeout=5)
        if r.ok:
            m = r.json().get("market", {})
            yes_v = _cprice(m, "yes_ask")
            no_v  = _cprice(m, "no_ask")
            if yes_v is None or no_v is None:
                log.debug("Kalshi %s: empty book (no ask)", ticker)
                return cached  # empty book — return stale rather than None
            val = {"yes": yes_v, "no": no_v}
            with _lock:
                _kal_prices[ticker] = val
                _kal_ts[ticker] = time.time()
            return val
    except Exception as e:
        log.debug("Kalshi REST failed for %s: %s — using WS cache", ticker, e)

    # WS cache as last resort (REST timed out / network error)
    return cached


def start():
    """Start both WS threads in background."""
    threading.Thread(target=_pm_ws_thread,  daemon=True, name="pm-ws").start()
    threading.Thread(target=_kal_ws_thread, daemon=True, name="kal-ws").start()
    log.info("Price feed WS threads started")
