"""executor.py — Sequential PM-first leg execution."""
import asyncio
import json
import logging
import os
import socket
import time
import threading
import requests
from typing import Optional
from config import (LIVE_STAKE_USD, PM_CLOB_URL, KALSHI_BASE_URL,
                    VOL_SCALING_ENABLED, MOMENTUM_FILTER_ENABLED, MOMENTUM_MAX_PCT, MOMENTUM_ARB_REDUCTION,
                    DIRECTIONAL_STAKE_USD,
                    PM_PRIVATE_KEY, PM_API_KEY, PM_API_SECRET,
                    PM_API_PASSPHRASE, PM_FUNDER,
                    MIN_SIDE_CENTS, MAX_SIDE_CENTS,
                    TG_TOKEN, TG_CHAT_ID, DIRECTIONAL_MIN_CENTS, PM_MAX_SLIPPAGE_CENTS,
                    MAKER_MAX_PRICE_DRIFT_CENTS, MAKER_MAX_PENDING_USD, MAKER_POLL_INTERVAL_SECS)
import kalshi_auth
import price_feed

log = logging.getLogger("executor")


# ── Kalshi depth cache: ticker → (depth, timestamp, threshold) ───────────────
# Caches orderbook depth for 5s to avoid rate-limiting on rapid cycles.
_depth_cache: dict = {}
_pm_balance_cache: list = [0.0, 0.0]  # [balance_usd, timestamp] — 60s TTL

# ── Maker Exposure Tracking ──────────────────────────────────────────────────
_pending_maker_usd = 0.0
_pending_lock = threading.Lock()

# ── Geoblock Circuit Breaker ──────────────────────────────────────────────────
_pm_geoblocked: bool = False

def _trigger_geoblock_circuit_breaker():
    """
    Called when PM returns 403 (geo-restricted).
    - Sets the global circuit breaker flag (blocks all future Kalshi orders).
    - Cancels ALL resting Kalshi orders immediately.
    - Sends Telegram alert.
    """
    global _pm_geoblocked
    if _pm_geoblocked:
        return  # already tripped — don't double-alert
    _pm_geoblocked = True
    log.error("PM GEOBLOCK DETECTED — circuit breaker tripped. Cancelling all resting Kalshi orders.")

    # Cancel all resting Kalshi orders to avoid orphaned directional positions
    try:
        h = kalshi_auth.signed_headers("GET", "/trade-api/v2/portfolio/orders")
        r = requests.get(
            KALSHI_BASE_URL + "/portfolio/orders",
            headers=h,
            params={"status": "resting"},
            timeout=8,
        )
        if r.ok:
            orders = r.json().get("orders", [])
            log.warning("Cancelling %d resting Kalshi orders due to PM geoblock", len(orders))
            for order in orders:
                oid = order.get("order_id", "")
                if oid:
                    _cancel_kalshi(oid)
        else:
            log.error("Could not fetch resting orders for cancellation: %d %s", r.status_code, r.text[:100])
    except Exception as e:
        log.error("Failed to cancel resting Kalshi orders on geoblock: %s", e)

    # Telegram alert
    msg = (
        f"🚨 <b>PM GEOBLOCK DETECTED</b>\n"
        f"Host: {socket.gethostname()}\n"
        f"POST /order returned 403 — Trading restricted from this IP.\n"
        f"All resting Kalshi orders cancelled.\n"
        f"Bot is blocking all new executions.\n"
        f"⚠️ Check for any open directional Kalshi positions!"
    )
    if TG_TOKEN and TG_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                timeout=5,
            )
        except Exception as e:
            log.error("Telegram alert failed: %s", e)


# ── Background Rollback Retry Queue ──────────────────────────────────────────
# When a PM SELL rollback fails (CLOB ledger lag), queue it here for periodic
# retry in a background thread. Each entry: {token_id, shares, queued_at, attempts}.
_rollback_queue: list = []
_rollback_lock = threading.Lock()
_rollback_thread_started = False


def _enqueue_rollback(token_id: str, shares: float):
    """Add a failed rollback to the background retry queue."""
    with _rollback_lock:
        _rollback_queue.append({
            "token_id": token_id,
            "shares": shares,
            "queued_at": time.time(),
            "attempts": 0,
        })
    log.warning("Queued background rollback: %.4f shares of token %s…", shares, token_id[:16])
    _ensure_rollback_thread()


def _ensure_rollback_thread():
    """Start the background rollback worker if not already running."""
    global _rollback_thread_started
    if _rollback_thread_started:
        return
    _rollback_thread_started = True
    t = threading.Thread(target=_rollback_worker, daemon=True, name="rollback-retry")
    t.start()
    log.info("Background rollback retry thread started")


def _rollback_worker():
    """Background thread: retry failed PM SELL rollbacks every 30s."""
    while True:
        time.sleep(30)
        with _rollback_lock:
            if not _rollback_queue:
                continue
            # Work on oldest entry
            entry = _rollback_queue[0]

        token_id = entry["token_id"]
        shares = entry["shares"]
        entry["attempts"] += 1
        attempt = entry["attempts"]
        age_s = time.time() - entry["queued_at"]

        # Give up after 10 minutes (position will expire or be manually handled)
        if age_s > 600:
            log.warning("Background rollback giving up on token %s… after %.0fs (%d attempts)",
                        token_id[:16], age_s, attempt)
            with _rollback_lock:
                if _rollback_queue and _rollback_queue[0] is entry:
                    _rollback_queue.pop(0)
            continue

        log.info("Background rollback attempt %d for token %s… (%.0fs old)",
                 attempt, token_id[:16], age_s)
        result = _sell_pm_fok(token_id, shares)
        if result:
            log.info("✅ Background rollback SUCCEEDED for token %s…: sold %.2f shares → $%.2f",
                     token_id[:16], result.get("shares", 0), result.get("cost", 0))
            try:
                import notifier as _ntf_bg
                _ntf_bg._send(
                    f"✅ <b>Background rollback succeeded</b>\n"
                    f"Token: <code>{token_id[:16]}…</code>\n"
                    f"Sold: {result.get('shares', 0):.1f} shares → ${result.get('cost', 0):.2f}\n"
                    f"Attempt: {attempt} ({age_s:.0f}s after initial failure)"
                )
            except Exception:
                pass
            with _rollback_lock:
                if _rollback_queue and _rollback_queue[0] is entry:
                    _rollback_queue.pop(0)
        else:
            log.info("Background rollback attempt %d failed for token %s… — will retry in 30s",
                     attempt, token_id[:16])


def preflight_check() -> bool:
    """
    Verify both platforms are reachable and credentials work before any live order.
    Returns True if all checks pass, False otherwise.
    """
    ok = True
    # 1. Kalshi auth
    try:
        h = kalshi_auth.signed_headers("GET", "/trade-api/v2/portfolio/balance")
        r = requests.get(KALSHI_BASE_URL + "/portfolio/balance", headers=h, timeout=6)
        if r.ok:
            bal = r.json().get("balance", 0) / 100
            log.info("Preflight Kalshi: OK — balance $%.2f", bal)
        else:
            log.error("Preflight Kalshi FAILED: %d %s", r.status_code, r.text[:100])
            ok = False
    except Exception as e:
        log.error("Preflight Kalshi FAILED: %s", e)
        ok = False
    # 2. PM CLOB reachable
    try:
        r = requests.get(f"{PM_CLOB_URL}/ok", timeout=6)
        if r.ok:
            log.info("Preflight PM CLOB: OK")
        else:
            log.error("Preflight PM CLOB FAILED: %d", r.status_code)
            ok = False
    except Exception as e:
        log.error("Preflight PM CLOB FAILED: %s", e)
        ok = False
    # 3. PM wallet balance
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
        usdc_abi = '[{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]'
        import json as _json
        c = w3.eth.contract(
            address=Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"),
            abi=_json.loads(usdc_abi)
        )
        bal = c.functions.balanceOf(Web3.to_checksum_address(PM_FUNDER)).call() / 1e6
        if bal < 2:
            log.warning("Preflight PM wallet LOW: $%.2f USDC.e — redemptions may be pending", bal)
            # Don't block — CLOB may still work from prior approvals; orders fail naturally if not
        else:
            log.info("Preflight PM wallet: OK — $%.2f USDC.e", bal)
    except Exception as e:
        log.warning("Preflight PM wallet check failed: %s (continuing)", e)
    return ok


# ── Polymarket ────────────────────────────────────────────────────────────────

_pm_client = None

def _get_pm_client():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    return ClobClient(
        host="https://clob.polymarket.com",
        key=PM_PRIVATE_KEY,
        chain_id=137,
        creds=ApiCreds(
            api_key=PM_API_KEY,
            api_secret=PM_API_SECRET,
            api_passphrase=PM_API_PASSPHRASE,
        ),
        signature_type=0,
        funder=PM_FUNDER,
    )

def _pm():
    global _pm_client
    if _pm_client is None:
        _pm_client = _get_pm_client()
    return _pm_client


def _buy_pm_fok(token_id: str, side_label: str, amount_usd: float,
                max_price_cents: float = 75.0, min_price_cents: float = 25.0,
                min_fill_pct: float = 0.50) -> Optional[dict]:
    """Place FAK (fill-and-kill) market buy on Polymarket. Returns fill dict or None.

    FAK accepts partial fills — if the book can fill 60% of the order, we take it
    and adjust the Kalshi leg accordingly. Much higher fill rate than FOK which
    rejects anything less than 100%.

    min_fill_pct: minimum fraction of requested amount that must fill (default 50%).
                  Prevents micro-fills that aren't worth hedging.

    Fetches live CLOB midpoint immediately before placing — aborts if price has
    moved outside the valid range since the signal was detected.
    """
    from py_clob_client.clob_types import MarketOrderArgs, OrderType

    # ── Live price verification (prevents buying at 73¢ when signal said 50¢) ──
    try:
        r_mid = requests.get(
            f"{PM_CLOB_URL}/midpoint?token_id={token_id}", timeout=4
        )
        live_cents = float(r_mid.json().get("mid", 0)) * 100
        if live_cents > max_price_cents:
            log.warning("PM FAK aborted — live price %.1f¢ > cap %.1f¢", live_cents, max_price_cents)
            return None
        if live_cents < min_price_cents:
            log.warning("PM FAK aborted — live price %.1f¢ < floor %.1f¢", live_cents, min_price_cents)
            return None
        log.info("PM live price check: %.1f¢ ✓", live_cents)
    except Exception as e:
        log.warning("PM price check failed (%s) — aborting for safety", e)
        return None  # fail safe: don't execute if we can't verify price

    try:
        client = _pm()
        order_args = MarketOrderArgs(token_id=token_id, amount=amount_usd, side="BUY")
        signed   = client.create_market_order(order_args)
        result   = client.post_order(signed, OrderType.FAK)
        if not result.get("success", True) or result.get("status") == "failed":
            log.warning("PM FAK not successful: %s", result)
            return None
        shares = float(result.get("takingAmount", 0))
        cost   = float(result.get("makingAmount", amount_usd))
        if shares == 0:
            log.warning("PM FAK returned 0 shares")
            return None
        fill_price = cost / shares * 100  # cents
        # Check minimum fill threshold
        fill_pct = cost / amount_usd
        if fill_pct < min_fill_pct:
            log.warning("PM FAK partial fill too small: $%.2f / $%.2f (%.0f%% < %.0f%% min) — rolling back",
                        cost, amount_usd, fill_pct * 100, min_fill_pct * 100)
            # Roll back the small fill
            _rb = _sell_pm_fok(token_id, shares)
            if _rb is None:
                _enqueue_rollback(token_id, shares)
                log.warning("PM micro-fill rollback failed — %.2f shares stuck", shares)
            return None
        if fill_pct < 1.0:
            log.info("PM %s partial fill: $%.2f / $%.2f (%.0f%%) → %.2f shares @ %.1f¢",
                     side_label, cost, amount_usd, fill_pct * 100, shares, fill_price)
        else:
            log.info("PM %s filled: $%.2f → %.2f shares @ %.1f¢", side_label, cost, shares, fill_price)
        return {"shares": shares, "cost": cost, "price_cents": fill_price}
    except Exception as e:
        log.warning("PM FAK error: %s", e)
        global _pm_client
        err_str = str(e).lower()
        if "403" in str(e) or "restricted" in err_str or "forbidden" in err_str:
            _trigger_geoblock_circuit_breaker()
        if "401" in str(e) or "403" in str(e):
            _pm_client = None
        return None


# ── Kalshi ────────────────────────────────────────────────────────────────────


def _sell_pm_fok(token_id: str, shares: float) -> Optional[dict]:
    """Roll back a one-sided PM fill by selling shares back to the CLOB.

    Two-phase approach:
      Phase 1: Wait for tokens to appear on-chain (ERC1155 balanceOf, up to 12s).
      Phase 2: Poll CLOB positions until the exchange acknowledges our balance
               (avoids the "not enough balance/allowance" race condition).
    Then attempt FOK sell, retrying up to 5x with 3s between attempts.
    Never gives up early — always tries even if CLOB poll times out.
    """
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    import time as _t

    # ── Phase 1: Wait for on-chain ERC1155 token delivery ────────────────────
    _t.sleep(2.0)  # minimum initial wait for tx submission
    actual_shares = None
    try:
        from web3 import Web3 as _W3
        _w3 = _W3(_W3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
        _CTF_ABI = [{"inputs":[{"name":"account","type":"address"},{"name":"id","type":"uint256"}],
                     "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],
                     "type":"function","stateMutability":"view"}]
        _ctf = _w3.eth.contract(
            address=_W3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"),
            abi=_CTF_ABI)
        for _bal_attempt in range(1, 7):  # up to 6 tries × 2s = 12s max
            _bal = _ctf.functions.balanceOf(
                _W3.to_checksum_address(PM_FUNDER), int(token_id)).call()
            _on_chain = _bal / 1e6
            log.info("PM SELL: on-chain=%.4f (attempt %d) gross=%.4f",
                     _on_chain, _bal_attempt, shares)
            if _on_chain >= 0.01:
                actual_shares = min(shares, _on_chain)
                break
            if _bal_attempt < 6:
                log.info("PM SELL: tokens not on-chain yet — waiting 2s (attempt %d/6)", _bal_attempt)
                _t.sleep(2.0)
        if actual_shares is None:
            log.warning("PM SELL: on-chain balance still 0 after 12s — rollback cannot proceed")
            return None
    except Exception as _e:
        log.warning("PM SELL: on-chain check failed (%s) — fallback to gross*0.985", _e)
        actual_shares = round(shares * 0.985, 2)

    actual_shares = round(actual_shares - 0.0001, 4)
    if actual_shares < 0.01:
        return None
    log.info("PM SELL: on-chain confirmed %.4f shares — polling CLOB for internal sync", actual_shares)

    # ── Phase 2: Poll CLOB /balance-allowance until internal ledger is ready ──
    # The CLOB's internal ledger lags on-chain state for short-lived 15-min
    # candle markets. In practice, this poll has NEVER confirmed (0/8+ times)
    # — the FOK sell always succeeds after timeout using on-chain balance.
    # We keep a short poll as a courtesy but cap at 15s to avoid burning
    # rollback time near expiry. Phase 1 (on-chain) is the real gate.
    _CLOB_POLL_MAX = 15  # was 180s; CLOB never confirms anyway, sell works without it
    _CLOB_POLL_INT = 2
    _clob_ok = False
    _clob_elapsed = 0
    while _clob_elapsed < _CLOB_POLL_MAX:
        try:
            _bal_client = _pm()
            _bal_resp = _bal_client.get_balance_allowance(
                params={"asset_type": "CONDITIONAL", "token_id": token_id}
            )
            _allowance = float(_bal_resp.get("allowance", 0))
            if _allowance >= actual_shares * 0.99:
                log.info("PM SELL: CLOB allowance %.4f confirmed after %ds — safe to sell",
                         _allowance, _clob_elapsed)
                _clob_ok = True
                actual_shares = min(actual_shares, _allowance - 0.0001)
                break
            log.debug("PM SELL: CLOB allowance %.4f < %.4f (%ds elapsed) — waiting",
                      _allowance, actual_shares, _clob_elapsed)
        except Exception as _pe:
            log.debug("PM SELL: balance-allowance poll error (%s) — will retry", _pe)
        _t.sleep(_CLOB_POLL_INT)
        _clob_elapsed += _CLOB_POLL_INT

    if not _clob_ok:
        log.warning("PM SELL: CLOB allowance not confirmed after %ds — attempting sell anyway", _CLOB_POLL_MAX)

    # ── Phase 3: FOK sell with retries ───────────────────────────────────────
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        try:
            client = _pm()
            order_args = MarketOrderArgs(token_id=token_id, amount=actual_shares, side="SELL")
            signed  = client.create_market_order(order_args)
            result  = client.post_order(signed, OrderType.FOK)
            if result.get("success", True) and result.get("status") != "failed":
                # For SELL: makingAmount = shares sold, takingAmount = USDC received
                # (opposite of BUY where makingAmount = USDC spent, takingAmount = shares)
                sold     = float(result.get("makingAmount", actual_shares))
                proceeds = float(result.get("takingAmount", 0))
                log.info("PM SELL rollback OK (attempt %d): sold %.2f shares → $%.2f recovered",
                         attempt, sold, proceeds)
                return {"shares": sold, "cost": proceeds}
            log.warning("PM SELL FOK attempt %d/%d failed: %s", attempt, max_attempts, result)
        except Exception as e:
            err_str = str(e)
            log.warning("PM SELL FOK attempt %d/%d error: %s", attempt, max_attempts, err_str)
            if "no match" in err_str.lower():
                # Book is empty (near expiry) — no point retrying
                log.error("PM SELL FOK: no match (book empty / near expiry) — giving up")
                return None
        if attempt < max_attempts:
            log.info("PM SELL rollback: waiting 3s before retry %d/%d", attempt + 1, max_attempts)
            _t.sleep(3.0)

    log.error("PM SELL rollback FAILED after %d attempts — directional exposure remains!", max_attempts)
    return None


def _buy_kalshi_taker(ticker: str, side: str, price_cents: int, count: int) -> Optional[dict]:
    """
    Place a Kalshi limit order at the ask price, then verify it filled within 1.5s.
    If not filled → cancel immediately and return None. Never leaves resting orders.
    """
    try:
        path = "/trade-api/v2/portfolio/orders"
        # Kalshi API uses yes_price for both sides; for NO: yes_price = 100 - no_price
        yes_price_api = price_cents if side.lower() == "yes" else (100 - price_cents)
        payload = json.dumps({
            "ticker":    ticker,
            "action":    "buy",
            "type":      "limit",
            "side":      side.lower(),
            "yes_price": yes_price_api,
            "count":     count,
        })
        headers = kalshi_auth.signed_headers("POST", path)
        r = requests.post(
            KALSHI_BASE_URL + "/portfolio/orders",
            headers=headers,
            data=payload,
            timeout=10,
        )
        if not r.ok:
            log.warning("Kalshi order failed %d: %s", r.status_code, r.text[:200])
            return None

        order    = r.json().get("order", {})
        order_id = order.get("order_id", "")
        status   = order.get("status", "")
        log.info("Kalshi %s %s placed: %d contracts @ %d¢ id=%s status=%s",
                 ticker, side, count, price_cents, order_id[:12], status)

        if status in ("filled", "executed"):
            # Immediate fill — great (Kalshi uses "executed" for instant taker fills)
            log.info("Kalshi %s %s instant fill: %d contracts @ %d¢",
                     ticker, side, count, price_cents)
            return {"order_id": order_id, "contracts": count, "price_cents": price_cents}

        # Poll at 1s — if still resting (0 fills), cancel immediately.
        # Cuts PM exposure window from 8s→1.5s on failed fills.
        import time as _time
        _time.sleep(1.0)

        def _poll_fill(wait_extra=0):
            if wait_extra:
                _time.sleep(wait_extra)
            ch = kalshi_auth.signed_headers("GET", f"/trade-api/v2/portfolio/orders/{order_id}")
            cr = requests.get(KALSHI_BASE_URL + f"/portfolio/orders/{order_id}",
                              headers=ch, timeout=6)
            if cr.status_code == 404:
                return count, 0  # archived = fully executed
            if cr.ok:
                o = cr.json().get("order", {})
                return o.get("filled_count", 0) or 0, o.get("remaining_count", count) or 0
            return None, None

        # First poll at T+1s
        filled, remaining = _poll_fill()
        if filled is None:
            log.warning("Kalshi order status check failed — cancelling for safety")
            _cancel_kalshi(order_id)
            return None

        if filled >= count or remaining == 0:
            # Fully filled on first poll
            log.info("Kalshi %s filled at T+1s (%d contracts)", ticker, filled or count)
            return {"order_id": order_id, "contracts": filled or count, "price_cents": price_cents}

        if filled == 0:
            # Resting with zero fills after 1s → cancel immediately, save PM
            log.warning("Kalshi resting (0 fills after 1s) — cancelling immediately")
            _cancel_kalshi(order_id)

            # Race guard: poll order status until terminal state (canceled/filled),
            # then check the fills endpoint once. This is deterministic — a fixed
            # sleep is a guess; waiting for terminal state is exact. Cap at 3s total.
            _MAX_CANCEL_WAIT = 3.0
            _POLL_INTERVAL   = 0.25
            _elapsed         = 0.0
            _terminal        = False
            _order_status    = "unknown"
            while _elapsed < _MAX_CANCEL_WAIT:
                _time.sleep(_POLL_INTERVAL)
                _elapsed += _POLL_INTERVAL
                try:
                    _oh  = kalshi_auth.signed_headers("GET", f"/trade-api/v2/portfolio/orders/{order_id}")
                    _or  = requests.get(
                        KALSHI_BASE_URL + f"/portfolio/orders/{order_id}",
                        headers=_oh, timeout=4,
                    )
                    if _or.ok:
                        _order_status = _or.json().get("order", {}).get("status", "")
                        if _order_status in ("canceled", "filled"):
                            _terminal = True
                            break
                except Exception:
                    pass  # keep polling
            log.info(
                "Kalshi order terminal after %.2fs — status=%s",
                _elapsed, _order_status,
            )

            # Now check fills endpoint — order is in terminal state, no more fills coming
            try:
                _fh = kalshi_auth.signed_headers("GET", "/trade-api/v2/portfolio/fills")
                _fr = requests.get(
                    KALSHI_BASE_URL + f"/portfolio/fills?order_id={order_id}&limit=50",
                    headers=_fh, timeout=6,
                )
                if _fr.ok:
                    _fills = _fr.json().get("fills", [])
                    _total_filled = int(sum(float(f.get("count_fp", 0)) for f in _fills))
                    if _total_filled > 0:
                        # Use the correct price field based on side:
                        # YES orders → yes_price_dollars; NO orders → no_price_dollars
                        _price_field = "yes_price_dollars" if side.lower() == "yes" else "no_price_dollars"
                        _avg_price = int(round(
                            sum(float(f.get(_price_field, 0))
                                * float(f.get("count_fp", 0)) for f in _fills)
                            / _total_filled * 100
                        ))
                        log.info(
                            "Kalshi cancel race detected via fills endpoint: %d contracts @ ~%d¢",
                            _total_filled, _avg_price or price_cents,
                        )
                        return {"order_id": order_id, "contracts": _total_filled,
                                "price_cents": _avg_price or price_cents, "_race_fill": True}
            except Exception as _rge:
                log.warning("Race guard fills check failed (%s) — falling back to order poll", _rge)
                filled_after, _ = _poll_fill()
                if filled_after and filled_after > 0:
                    log.info("Kalshi cancel race (fallback poll): %d contracts @ %d¢",
                             filled_after, price_cents)
                    return {"order_id": order_id, "contracts": filled_after,
                            "price_cents": price_cents, "_race_fill": True}
            log.info("Kalshi cancel confirmed — 0 fills (no race condition)")
            return None

        # Partial fill — give 3 more seconds to complete
        log.info("Kalshi partial fill (%d/%d) — waiting 3s more", filled, count)
        filled2, remaining2 = _poll_fill(wait_extra=3.0)
        if filled2 and filled2 >= count:
            log.info("Kalshi %s filled at T+4s (%d contracts)", ticker, filled2)
            return {"order_id": order_id, "contracts": filled2, "price_cents": price_cents}
        # Cancel remainder, accept partial
        if remaining2 and remaining2 > 0:
            _cancel_kalshi(order_id)
        final = filled2 or filled
        if final > 0:
            log.info("Kalshi partial accepted: %d/%d contracts", final, count)
            return {"order_id": order_id, "contracts": final, "price_cents": price_cents}
        log.warning("Kalshi not filled after 4s (filled=%d/%d) — cancelled", final, count)
        return None

    except Exception as e:
        log.warning("Kalshi order error: %s", e)
        return None


def _cancel_kalshi(order_id: str):
    """Best-effort cancel of a Kalshi order."""
    try:
        path = f"/trade-api/v2/portfolio/orders/{order_id}"
        h = kalshi_auth.signed_headers("DELETE", path)
        requests.delete(KALSHI_BASE_URL + f"/portfolio/orders/{order_id}",
                       headers=h, timeout=6)
        log.info("Kalshi order %s cancelled", order_id[:12])
    except Exception as e:
        log.error("Kalshi cancel error for %s: %s", order_id[:12], e)


def _check_maker_race(order_id: str, side: str, price_cents: int, count: int) -> Optional[dict]:
    """After cancelling a maker order, check if a taker filled it before the cancel landed.

    Polls order status until terminal (canceled/filled), then queries the fills
    endpoint once. Returns a fill result dict if fills exist, else None.

    This is the maker-mode equivalent of the race guard already in _buy_kalshi_taker.
    Without this, a taker fill that races the cancel goes undetected — the bot thinks
    it has no Kalshi position and rolls back PM, leaving an unhedged winning Kalshi leg.
    """
    import time as _t

    # Wait for order to reach terminal state (cap at 3s)
    _MAX_WAIT = 3.0
    _POLL     = 0.25
    _elapsed  = 0.0
    _order_status = "unknown"
    while _elapsed < _MAX_WAIT:
        _t.sleep(_POLL)
        _elapsed += _POLL
        try:
            _oh = kalshi_auth.signed_headers("GET", f"/trade-api/v2/portfolio/orders/{order_id}")
            _or = requests.get(
                KALSHI_BASE_URL + f"/portfolio/orders/{order_id}",
                headers=_oh, timeout=4,
            )
            if _or.ok:
                _order_status = _or.json().get("order", {}).get("status", "")
                if _order_status in ("canceled", "filled"):
                    break
        except Exception:
            pass
    log.info("Maker race check: order terminal in %.2fs — status=%s", _elapsed, _order_status)

    # Now query fills — order is in terminal state, no more fills coming
    try:
        _fh = kalshi_auth.signed_headers("GET", "/trade-api/v2/portfolio/fills")
        _fr = requests.get(
            KALSHI_BASE_URL + f"/portfolio/fills?order_id={order_id}&limit=50",
            headers=_fh, timeout=6,
        )
        if _fr.ok:
            _fills = _fr.json().get("fills", [])
            _total = int(sum(float(f.get("count_fp", 0)) for f in _fills))
            if _total > 0:
                _pf = "yes_price_dollars" if side.lower() == "yes" else "no_price_dollars"
                _vwap = int(round(
                    sum(float(f.get(_pf, 0)) * float(f.get("count_fp", 0)) for f in _fills)
                    / _total * 100
                ))
                log.info(
                    "⚡ Maker cancel race detected via fills: %d contracts @ %d¢ — arb IS hedged",
                    _total, _vwap or price_cents,
                )
                return {
                    "order_id":   order_id,
                    "contracts":  _total,
                    "price_cents": _vwap or price_cents,
                    "_race_fill": True,
                }
            log.info("Maker race check: 0 fills — cancel confirmed clean")
    except Exception as _e:
        log.warning("Maker race check fills query failed (%s) — assuming no fill", _e)

    return None

async def _buy_kalshi_maker(ticker: str, side: str, price_cents: int, count: int,
                            minutes_left: float, token_id: str, initial_pm_price: float,
                            pm_amount: float):
    # Place a GTC LIMIT maker bid on Kalshi at price_cents.
    # Waits for a taker to hit us. 0% maker fee. Cancelled at
    # MAKER_CANCEL_MINS_BEFORE_CLOSE minutes before candle close.
    # Uses asyncio.sleep so the event loop stays responsive.
    import asyncio
    from config import MAKER_CANCEL_MINS_BEFORE_CLOSE

    # Track exposure
    global _pending_maker_usd
    with _pending_lock:
        _pending_maker_usd += pm_amount
        log.info("Maker exposure: +$%.2f (Total pending: $%.2f)", pm_amount, _pending_maker_usd)

    yes_price_api = price_cents if side.lower() == "yes" else (100 - price_cents)
    import json as _json
    payload = _json.dumps({
        "ticker":    ticker,
        "action":    "buy",
        "type":      "limit",
        "side":      side.lower(),
        "yes_price": yes_price_api,
        "count":     count,
    })
    path_url = "/trade-api/v2/portfolio/orders"
    headers  = kalshi_auth.signed_headers("POST", path_url)
    order_id = ""
    try:
        r = requests.post(KALSHI_BASE_URL + "/portfolio/orders",
                          headers=headers, data=payload, timeout=10)
        if not r.ok:
            log.warning("Kalshi maker order rejected %d: %s", r.status_code, r.text[:200])
            with _pending_lock:
                _pending_maker_usd -= pm_amount
            return None
        order    = r.json().get("order", {})
        order_id = order.get("order_id", "")
        status   = order.get("status", "")
        log.info("Kalshi MAKER posted: %s %s %d contracts @ %dc  id=%s  status=%s",
                 ticker, side, count, price_cents, order_id[:12], status)
        if status in ("filled", "executed"):
            log.info("Kalshi maker instant fill: %d contracts @ %dc", count, price_cents)
            with _pending_lock:
                _pending_maker_usd -= pm_amount
            return {"order_id": order_id, "contracts": count, "price_cents": price_cents}
    except Exception as e:
        log.warning("Kalshi maker place error: %s", e)
        with _pending_lock:
            _pending_maker_usd -= pm_amount
        return None

    deadline_secs = max((minutes_left - MAKER_CANCEL_MINS_BEFORE_CLOSE) * 60, 10)
    elapsed       = 0.0

    try:
        while elapsed < deadline_secs:
            # Ghost Order Pulse-Check: Poll faster as we approach the deadline
            # T-6m to T-5m: 2s interval; otherwise 5s (MAKER_POLL_INTERVAL_SECS)
            time_to_deadline = deadline_secs - elapsed
            current_poll = 2.0 if time_to_deadline < 60.0 else MAKER_POLL_INTERVAL_SECS
            
            sleep_secs = min(current_poll, time_to_deadline)
            await asyncio.sleep(sleep_secs)
            elapsed += sleep_secs

            # 1. Price Drift Monitor (Flash Crash Safeguard)
            live_pm = price_feed.get_pm_price(token_id)
            if live_pm:
                drift = abs(live_pm - initial_pm_price)
                if drift > MAKER_MAX_PRICE_DRIFT_CENTS:
                    # Hybrid Logic: Check if Taker Fallback is still profitable
                    # Use a fresh Kalshi ask to see if we'd still make $0.01+
                    log.info("Maker drift detected (%.1f¢) — checking profitability for Taker fallback", drift)
                    
                    fresh_kal = price_feed.get_kalshi_price(ticker)
                    if fresh_kal:
                        side_key = side.lower() # 'yes' or 'no'
                        # For Taker, we use the ask price + buffer
                        _buf = 3 if side_key == "yes" else 8
                        current_kal_taker = fresh_kal.get(side_key, 100) + _buf
                        
                        combined_cost = live_pm + current_kal_taker
                        if combined_cost < 100.0:
                            log.info("Drifted arb still profitable (combined %.1f¢) — continuing to wait or fallback", combined_cost)
                        else:
                            log.warning("Drifted arb UNPROFITABLE (combined %.1f¢) — killing trade", combined_cost)
                            _cancel_kalshi(order_id)
                            _race = _check_maker_race(order_id, side, price_cents, count)
                            if _race:
                                return _race  # taker raced the cancel — arb is actually hedged
                            return {"drift_cancel": True}
                    else:
                        # If we can't verify price, play it safe and kill
                        log.warning("Drifted but cannot verify Kalshi price — killing trade for safety")
                        _cancel_kalshi(order_id)
                        _race = _check_maker_race(order_id, side, price_cents, count)
                        if _race:
                            return _race  # taker raced the cancel — arb is actually hedged
                        return {"drift_cancel": True}

            # 2. Status Check
            try:
                ch = kalshi_auth.signed_headers("GET",
                     "/trade-api/v2/portfolio/orders/" + order_id)
                cr = requests.get(KALSHI_BASE_URL + "/portfolio/orders/" + order_id,
                                  headers=ch, timeout=6)
                if cr.status_code == 404:
                    log.info("Kalshi maker filled (archived) after %.0fs", elapsed)
                    return {"order_id": order_id, "contracts": count, "price_cents": price_cents}
                if cr.ok:
                    o         = cr.json().get("order", {})
                    st        = o.get("status", "")
                    filled    = int(o.get("filled_count",    0) or 0)
                    remaining = int(o.get("remaining_count", count) or 0)
                    log.debug("Kalshi maker poll @%.0fs: status=%s filled=%d remaining=%d",
                             elapsed, st, filled, remaining)
                    if st in ("filled", "executed") or remaining == 0:
                        log.info("Kalshi maker filled: %d contracts @ %dc", filled or count, price_cents)
                        return {"order_id": order_id, "contracts": filled or count,
                                "price_cents": price_cents}
            except Exception as e:
                log.warning("Kalshi maker poll error at %.0fs: %s", elapsed, e)

        log.warning("Kalshi maker deadline: %.1fmin remaining=%s — cancelling %s",
                    elapsed / 60, minutes_left, order_id[:12])
        _cancel_kalshi(order_id)
        _race = _check_maker_race(order_id, side, price_cents, count)
        if _race:
            return _race  # taker raced the deadline cancel — arb is hedged
        return None
    finally:
        with _pending_lock:
            _pending_maker_usd -= pm_amount
            log.info("Maker exposure cleared: -$%.2f (Remaining pending: $%.2f)", pm_amount, _pending_maker_usd)





def _pm_book_check(token_id: str, amount_usd: float, signal_price_cents: float) -> tuple:
    """
    Walk the PM CLOB ask side for `amount_usd` and compute the volume-weighted
    average fill price (VWAP). Returns (ok, vwap_cents, unfilled_usd).

    ok=False means: not enough liquidity OR projected fill price exceeds signal
    by more than PM_MAX_SLIPPAGE_CENTS. Caller should abort or reduce size.
    """
    try:
        r = requests.get(
            f"{PM_CLOB_URL}/book?token_id={token_id}", timeout=4
        )
        if not r.ok:
            log.debug("PM book fetch %d — skipping impact check", r.status_code)
            return True, signal_price_cents, 0.0

        asks = r.json().get("asks", [])
        if not asks:
            log.debug("PM book: empty asks — skipping impact check")
            return True, signal_price_cents, 0.0

        remaining_usd  = amount_usd
        total_cost     = 0.0
        total_shares   = 0.0

        for level in sorted(asks, key=lambda x: float(x["price"])):
            price = float(level["price"])
            size  = float(level["size"])
            if price > 0.95:   # ignore near-certain fringe asks
                continue
            avail_cost = price * size
            take       = min(remaining_usd, avail_cost)
            shares     = take / price
            total_cost   += take
            total_shares += shares
            remaining_usd -= take
            if remaining_usd <= 0.01:
                break

        if total_shares < 1:
            log.warning("PM book: insufficient liquidity (0 shares fillable) for $%.2f order", amount_usd)
            return False, signal_price_cents, amount_usd

        vwap_cents  = total_cost / total_shares * 100
        slippage    = vwap_cents - signal_price_cents
        unfilled    = remaining_usd

        log.info(
            "PM book: VWAP %.2fc (signal %.2fc, slippage %+.2fc) | "
            "filled %.1f shares | unfilled $%.2f",
            vwap_cents, signal_price_cents, slippage, total_shares, unfilled
        )

        if unfilled > amount_usd * 0.10:   # >10% of order can't fill
            log.warning("PM book: insufficient depth — $%.2f of $%.2f unfilled", unfilled, amount_usd)
            return False, vwap_cents, unfilled

        if slippage > PM_MAX_SLIPPAGE_CENTS:
            log.warning(
                "PM book: price impact %.2fc exceeds limit %.2fc — skipping",
                slippage, PM_MAX_SLIPPAGE_CENTS
            )
            return False, vwap_cents, unfilled

        return True, vwap_cents, unfilled

    except Exception as e:
        log.debug("PM book check error (%s) — proceeding without check", e)
        return True, signal_price_cents, 0.0


# ── Main execution ────────────────────────────────────────────────────────────

# Module-level crash recovery flags — set after PM fills so main.py can
# detect and rollback if an exception escapes execute_arb mid-flight.
_last_pm_filled: bool = False
_last_pm_shares: float = 0.0

async def execute_arb(window: dict, live: bool = True, directional_fallback: bool = False) -> dict:
    """
    Execute both legs of a cross-platform arb window simultaneously.
    Returns result dict with fill details.
    """
    global _last_pm_filled, _last_pm_shares
    _last_pm_filled = False
    _last_pm_shares = 0.0
    pm_price   = window["pm_price"]    # cents
    kal_price  = window["kal_price"]   # cents
    pm_token   = window["pm_token_id"]
    kal_ticker = window["kal_ticker"]
    kal_side   = window["kal_side"]    # "YES" or "NO"
    asset      = window["asset"]

    # Equal contract counts — critical for direction-neutral compression
    pm_price_usd  = pm_price  / 100
    kal_price_usd = kal_price / 100
    contracts = min(
        int(LIVE_STAKE_USD / pm_price_usd),
        int(LIVE_STAKE_USD / kal_price_usd),
    )
    if contracts < 1:
        return {"success": False, "error": "contract count < 1"}

    pm_amount  = contracts * pm_price_usd
    kal_amount = contracts * kal_price_usd
    profit_locked = contracts * (100 - pm_price - kal_price) / 100

    log.info("Executing arb: %s | PM %s @ %.1f¢ + Kalshi %s @ %.1f¢ = %.1f¢ combined | %d contracts | locked $%.3f",
             asset, window["pm_side"], pm_price, kal_side, kal_price,
             pm_price + kal_price, contracts, profit_locked)

    if not live:
        return {
            "success":       True,
            "paper":         True,
            "contracts":     contracts,
            "pm_price":      pm_price,
            "kal_price":     kal_price,
            "profit_locked": profit_locked,
        }

    # ── Geoblock circuit breaker — refuse all live execution if tripped ───────
    if _pm_geoblocked:
        log.warning("PM geoblock circuit breaker active — refusing execution")
        return {"success": False, "error": "PM geoblocked — circuit breaker active"}

    # ── Step -2: Cap stake to available Kalshi cash (80%) + PM USDC.e (40%) ──
    # Kalshi cap: prevents insufficient_balance errors.
    # PM cap: prevents PM wallet from being drained in 1-2 fills, keeping the
    #         bot firing continuously rather than stalling until redeemer sweeps.
    #         Uses 40% of PM balance so at least 2-3 fills remain after each trade.
    #         Floor of $15 so we don't size down to useless micro-trades.
    _PM_STAKE_FRACTION = 0.25
    _PM_STAKE_MIN      = 15.0

    # Spread-proportional sizing: larger arb spread = higher conviction = scale up
    # spread_cents = profit per contract (¢). Target baseline = 3¢.
    # Scale: 1¢ spread → 0.5×, 3¢ → 1.0×, 5¢ → 1.67×, ≥6¢ → 2.0× (capped)
    _spread_cents = 100 - pm_price - kal_price
    _TARGET_SPREAD = 3.0    # typical minimum spread (¢)
    _MAX_SCALE     = 2.0    # cap at 2× base stake
    _MIN_SCALE     = 0.5    # floor at 0.5× (never go below half stake)
    _spread_scale  = min(max(_spread_cents / _TARGET_SPREAD, _MIN_SCALE), _MAX_SCALE)

    # Stake override: non-standard entry modes (e.g. div_collapse) use a
    # fixed reduced stake and skip spread scaling for risk control.
    _base_stake = window.get("stake_override_usd", LIVE_STAKE_USD)
    _entry_mode = window.get("entry_mode", "normal")
    if _entry_mode != "normal":
        effective_stake = _base_stake   # fixed — no scaling
        log.info("Entry mode=%s → fixed stake=$%.2f (no spread scaling)", _entry_mode, effective_stake)
    else:
        effective_stake = _base_stake * _spread_scale
        log.info("Spread=%.1f¢ → stake_scale=%.2f× → base_stake=$%.2f",
                 _spread_cents, _spread_scale, effective_stake)

    # ── Parallel pre-checks ─────────────────────────────────────────────────
    # Kalshi balance, PM balance (60s cached), position check, Kalshi live ask,
    # and PM pre-flight midpoint are all independent — run simultaneously.
    # Cuts ~2.5s sequential latency to ~0.5s (PM Web3 was the bottleneck).
    import concurrent.futures as _cf
    import json as _json2

    def _fetch_kal_balance():
        try:
            hb = kalshi_auth.signed_headers("GET", "/trade-api/v2/portfolio/balance")
            rb = requests.get(KALSHI_BASE_URL + "/portfolio/balance", headers=hb, timeout=5)
            return rb.json().get("balance", 0) / 100 if rb.ok else None
        except Exception as e:
            log.warning("Kalshi balance fetch error: %s", e); return None

    def _fetch_pm_balance():
        _now_b = time.time()
        if _pm_balance_cache[0] > 0 and (_now_b - _pm_balance_cache[1]) < 60.0:
            return _pm_balance_cache[0]
        try:
            from web3 import Web3 as _W3
            _w3 = _W3(_W3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
            _abi = '[{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]'
            _c2 = _w3.eth.contract(address=_W3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"),
                                   abi=_json2.loads(_abi))
            bal = _c2.functions.balanceOf(_W3.to_checksum_address(PM_FUNDER)).call() / 1e6
            _pm_balance_cache[0] = bal; _pm_balance_cache[1] = time.time()
            return bal
        except Exception as e:
            log.warning("PM balance fetch error (%s) — cached $%.2f", e, _pm_balance_cache[0])
            return _pm_balance_cache[0] or None

    def _fetch_position():
        try:
            hp = kalshi_auth.signed_headers("GET", "/trade-api/v2/portfolio/positions")
            rp = requests.get(KALSHI_BASE_URL + "/portfolio/positions", headers=hp,
                              params={"count_filter": "position", "ticker_filter": kal_ticker}, timeout=5)
            if rp.ok:
                return [p for p in rp.json().get("market_positions", [])
                        if p["ticker"] == kal_ticker and p["position"] != 0]
            return []
        except Exception as e:
            log.warning("Position pre-check error (%s)", e); return []

    def _fetch_kal_ask():
        try:
            hk = kalshi_auth.signed_headers("GET", f"/trade-api/v2/markets/{kal_ticker}")
            rk = requests.get(KALSHI_BASE_URL + f"/markets/{kal_ticker}", headers=hk, timeout=5)
            return rk.json().get("market", {}) if rk.ok else {}
        except Exception as e:
            log.warning("Kalshi ask fetch error: %s", e); return {}

    def _fetch_pm_mid():
        try:
            r = requests.get(f"{PM_CLOB_URL}/midpoint?token_id={pm_token}", timeout=4)
            return float(r.json().get("mid", 0)) * 100
        except Exception as e:
            log.warning("PM midpoint fetch error: %s", e); return None

    _t0 = time.time()
    with _cf.ThreadPoolExecutor(max_workers=5) as _pool:
        _f_kb  = _pool.submit(_fetch_kal_balance)
        _f_pb  = _pool.submit(_fetch_pm_balance)
        _f_pos = _pool.submit(_fetch_position)
        _f_ka  = _pool.submit(_fetch_kal_ask)
        _f_pm  = _pool.submit(_fetch_pm_mid)
        _kal_cash_raw = _f_kb.result()
        _pm_usdc_raw  = _f_pb.result()
        _existing_pos = _f_pos.result()
        _mk_live      = _f_ka.result()
        _pm_mid_pre   = _f_pm.result()
    log.info("Parallel pre-checks: %.0fms", (time.time() - _t0) * 1000)

    # Kalshi balance → stake cap
    if _kal_cash_raw is not None:
        kalshi_cash = _kal_cash_raw
        capped_kal  = kalshi_cash * 0.80
        if capped_kal < 1.0:
            log.warning("Kalshi cash $%.2f too low — skipping", kalshi_cash)
            return {"success": False, "error": "balance too low", "skip_cooldown": True}
        if capped_kal < LIVE_STAKE_USD:
            log.warning("Kalshi cash $%.2f < stake — capping to $%.2f (80%%)", kalshi_cash, capped_kal)
        effective_stake = min(effective_stake, capped_kal)

    # PM USDC balance → stake cap
    pm_usdc   = _pm_usdc_raw or 0.0
    capped_pm = max(pm_usdc * _PM_STAKE_FRACTION, _PM_STAKE_MIN)
    if pm_usdc < _PM_STAKE_MIN:
        log.warning("PM USDC.e $%.2f too low — skipping", pm_usdc)
        import time as _tbal
        _now_bal = _tbal.time()
        if not hasattr(execute_arb, "_last_lowbal_alert") or _now_bal - execute_arb._last_lowbal_alert > 1800:
            import notifier as _ntf_bal
            _ntf_bal._send(f"⚠️ [LOW BALANCE] PM USDC.e: ${pm_usdc:.2f} Bot paused.")
            execute_arb._last_lowbal_alert = _now_bal
        return {"success": False, "error": "PM balance too low", "skip_cooldown": True}
    if capped_pm < effective_stake:
        log.info("PM USDC.e $%.2f — capping stake to $%.2f (%.0f%%)", pm_usdc, capped_pm, _PM_STAKE_FRACTION * 100)
    effective_stake = min(effective_stake, capped_pm)

    # Recompute contracts with capped stake
    pm_price_usd  = pm_price  / 100
    kal_price_usd = kal_price / 100
    contracts = min(int(effective_stake / pm_price_usd), int(effective_stake / kal_price_usd))
    if contracts < 1:
        return {"success": False, "error": "contract count < 1", "skip_cooldown": True}
    pm_amount     = contracts * pm_price_usd
    profit_locked = contracts * (100 - pm_price - kal_price) / 100

    # Position pre-check result
    if _existing_pos:
        log.warning("Already have open position in %s — skipping", kal_ticker)
        return {"success": False, "error": "already in market"}

    # Kalshi live ask (from parallel fetch)
    _mk_live = _mk_live or {}
    def _c(v, d):
        val = v if v is not None else d
        if val is None: return int(d) if d is not None else 50
        fv = float(val); return int(round(fv * 100)) if fv <= 1 else int(fv)
    def _cprice_mk(key, fallback):
        v = _mk_live.get(key + "_dollars") or _mk_live.get(key)
        return _c(v, fallback) if v is not None else int(fallback)
    if _mk_live:
        if kal_side.upper() == "YES":
            live_kal_price = min(_cprice_mk("yes_ask", kal_price) + 3, 99)
        else:
            live_kal_price = max(100 - _cprice_mk("yes_bid", 100 - kal_price) + 8, 1)
        log.info("Kalshi live ask: %d¢ (signal %.1f¢, side=%s)", live_kal_price, kal_price, kal_side)
        kal_price = live_kal_price
    else:
        log.warning("Kalshi ask fetch failed — using signal price %.1f¢", kal_price)

    # PM pre-flight check (midpoint fetched in parallel)
    if _pm_mid_pre is None:
        log.warning("PM pre-flight midpoint failed — aborting")
        return {"success": False, "error": "PM pre-flight check failed"}
    pm_live_pre = _pm_mid_pre
    # div_collapse: oracle gap has already shrunk — 8¢ NO buf is overkill and
    # kills valid signals (e.g. ETH ask 33¢ + 8¢ = 41¢ → PM 59¢ → 100¢ exact reject).
    # Use 4¢ NO buf for collapse entries; standard 8¢ for all others.
    _pf_kal_buf = 3 if kal_side.upper() == "YES" else (4 if _entry_mode == "div_collapse" else 8)
    live_combined_buffered = pm_live_pre + kal_price + _pf_kal_buf
    if live_combined_buffered >= 100:
        log.warning("Pre-flight FAILED: live combined %.1f¢ (PM %.1f¢ + Kal %d¢ + %d¢ buf) ≥ 100¢",
                    live_combined_buffered, pm_live_pre, int(kal_price), _pf_kal_buf)
        return {"success": False, "pm_filled": False, "kal_filled": False,
                "error": f"No profit after buffer: combined {live_combined_buffered:.1f}¢"}
    if pm_live_pre < MIN_SIDE_CENTS or pm_live_pre > MAX_SIDE_CENTS:
        log.warning("Pre-flight FAILED: PM live %.1f¢ outside valid range", pm_live_pre)
        return {"success": False, "pm_filled": False, "kal_filled": False,
                "error": f"PM {pm_live_pre:.1f}¢ out of range"}
    log.info("Pre-flight ✓ — PM %.1f¢ + Kal %d¢ + %d¢ buf = %.1f¢ (%.1f¢ net profit)",
             pm_live_pre, int(kal_price), _pf_kal_buf, live_combined_buffered, 100 - live_combined_buffered)
    pm_price = pm_live_pre
    # ── Step 0a.5: Kalshi depth check (cached) ───────────────────────────────
    # Verify the book has enough contracts at our price BEFORE touching PM.
    # Thin books cause Kalshi no-fills → directional PM exposure. Skip early.
    # Cache results per ticker for 5s to avoid rate-limiting the orderbook endpoint.
    _now = time.time()
    cached = _depth_cache.get(kal_ticker)
    if cached and (_now - cached[1]) < 5.0:
        depth, threshold_cached = cached[0], cached[2]
        log.info("Kalshi depth (cached): %d contracts at ≥%d¢ (need %d)", depth, threshold_cached, contracts)
    else:
        depth = None
        threshold = 100 - int(kal_price)
        try:
            hob = kalshi_auth.signed_headers("GET", f"/trade-api/v2/markets/{kal_ticker}/orderbook")
            rob = requests.get(KALSHI_BASE_URL + f"/markets/{kal_ticker}/orderbook", headers=hob, timeout=4)
            if rob.ok:
                _rjson = rob.json()
                ob    = _rjson.get("orderbook", {})      # legacy: prices as int cents
                ob_fp = _rjson.get("orderbook_fp", {})   # new: prices as decimal dollar strings
                threshold_dol = threshold / 100.0
                if ob_fp:
                    # New format: [[price_str, size_str], ...]
                    if kal_side.upper() == "YES":
                        depth = sum(int(float(s)) for p, s in ob_fp.get("no_dollars", [])
                                    if float(p) >= threshold_dol)
                    else:
                        depth = sum(int(float(s)) for p, s in ob_fp.get("yes_dollars", [])
                                    if float(p) >= threshold_dol)
                elif ob:
                    # Legacy format: [[price_int, size_int], ...]
                    if kal_side.upper() == "YES":
                        depth = sum(int(s) for p, s in ob.get("no", []) if int(p) >= threshold)
                    else:
                        depth = sum(int(s) for p, s in ob.get("yes", []) if int(p) >= threshold)
                else:
                    depth = 0
                _depth_cache[kal_ticker] = (depth, _now, threshold)
                log.info("Kalshi depth: %d contracts available at ≥%d¢ (need %d)", depth, threshold, contracts)
            else:
                log.warning("Kalshi orderbook fetch failed (%d) — aborting to avoid one-sided fill", rob.status_code)
        except Exception as e:
            log.warning("Kalshi depth check error (%s) — aborting to avoid one-sided fill", e)

    # ── Dynamic Kalshi NO buffer: scale by YES bid depth ────────────────────────
    # Deep YES bids = stable book = less drift after PM fill → smaller buffer.
    # Thin YES bids = volatile repricing → larger buffer.
    if kal_side.upper() == "NO" and depth is not None and _mk_live:
        def __c(v, d):
            val = v if v is not None else d
            fv = float(val); return int(round(fv * 100)) if fv <= 1 else int(fv)
        _yb_raw = _mk_live.get("yes_bid_dollars") or _mk_live.get("yes_bid")
        _yes_bid_live = __c(_yb_raw, 100 - kal_price)
        if depth >= 3 * contracts:
            _dyn_buf = 4    # deep book — minimal drift risk
        elif depth >= 2 * contracts:
            _dyn_buf = 6    # moderate depth
        elif depth >= contracts:
            _dyn_buf = 8    # shallow (previous fixed default)
        else:
            _dyn_buf = 12   # very thin — large buffer
        _dyn_kal = max(100 - _yes_bid_live + _dyn_buf, 1)
        if _dyn_kal != int(kal_price):
            log.info("Dynamic NO buffer: depth=%d contracts → +%d¢ (price %d¢→%d¢)",
                     depth, _dyn_buf, int(kal_price), _dyn_kal)
            kal_price = _dyn_kal

    # ── Depth gate: fill-to-depth instead of hard skip ─────────────────────
    # Kalshi books are thin (50-150 contracts typical). Rather than skipping an
    # entire arb window because we can't get full size, cap contracts to what's
    # available and take the partial fill. A 100-contract fill at 20¢ = $20 locked.
    # Only hard-skip if depth is truly negligible (< MIN_DEPTH_CONTRACTS).
    MIN_DEPTH_CONTRACTS = 15
    _directional_only = False
    if depth is None:
        log.warning("Depth check failed — skipping for safety")
        return {"success": False, "error": "depth check failed", "skip_cooldown": True}
    elif depth < MIN_DEPTH_CONTRACTS:
        log.warning("Depth gate: Kalshi depth %d below minimum %d — skipping", depth, MIN_DEPTH_CONTRACTS)
        return {"success": False, "error": f"Kalshi depth {depth} < min {MIN_DEPTH_CONTRACTS}", "skip_cooldown": True}
    elif depth < contracts:
        # Partial depth available — scale down to what the book can absorb
        log.info("Depth gate: capping contracts %d → %d (available depth), stake will scale accordingly",
                 contracts, depth)
        contracts = depth
        # Recompute effective_stake to match depth-capped contracts
        effective_stake = min(contracts * max(pm_price / 100, kal_price / 100), effective_stake)

    # Recompute contract count and profit with fresh prices + balance-capped stake
    pm_price_usd  = pm_price  / 100
    kal_price_usd = kal_price / 100
    contracts = min(int(effective_stake / pm_price_usd), int(effective_stake / kal_price_usd))
    if contracts < 1:
        return {"success": False, "error": "contract count < 1 after price refresh", "skip_cooldown": True}
    # CRITICAL: recalculate pm_amount to match depth-capped contracts.
    # Without this, PM buys full stake ($100) even when Kalshi depth was capped
    # to e.g. 37 contracts ($23), making kal_contracts 4× the book capacity.
    pm_amount = contracts * pm_price_usd
    profit_locked = contracts * (100 - pm_price - kal_price) / 100
    if profit_locked <= 0:
        log.warning("No profit after price refresh (combined %.1f¢) — skipping", pm_price + kal_price)
        return {"success": False, "error": f"combined {pm_price + kal_price:.1f}¢ ≥ 100¢ after refresh"}

    pm_price = pm_live_pre

    # ── Step 0c: PM order book price-impact check ───────────────────────────
    # Walk the ask side for our order size. If VWAP > signal + PM_MAX_SLIPPAGE_CENTS
    # or the book is too thin to fill 90% of our order, abort before touching anything.
    _pm_book_ok, _pm_vwap, _pm_unfilled = _pm_book_check(pm_token, pm_amount, pm_price)
    if not _pm_book_ok:
        return {
            "success": False, "pm_filled": False, "kal_filled": False,
            "error": f"PM book impact too high (VWAP {_pm_vwap:.1f}c, unfilled ${_pm_unfilled:.2f})",
            "skip_cooldown": True,
        }

        # ── Execution sequence: SEQUENTIAL PM-first ────────────────────────────
    # 1. Fire PM FOK first (no Kalshi exposure if PM fails/rejects).
    # 2. Use actual PM fill price to re-check combined profitability.
    # 3. Re-fetch fresh Kalshi ask before firing (avoids stale price).
    # 4. If post-fill combined >= 100¢: attempt PM rollback, abort clean.
    # 5. Fire Kalshi taker with fresh price.
    #
    # Reverted from simultaneous (asyncio.gather) on 2026-03-11.
    # Rationale: Vultr Stockholm has no PM geo-blocking; PM-first avoids
    # orphaned Kalshi positions entirely. The 8s Kalshi timeout handles
    # slow partial fills that previously appeared as ghosts.

    log.info("Firing PM first (sequential) — %d contracts, PM=%.1f¢ Kal=%.1f¢",
             contracts, pm_price, kal_price)

    # ── Step 1: Buy PM ───────────────────────────────────────────────────────
    pm_result = _buy_pm_fok(
        pm_token, window["pm_side"], pm_amount,
        pm_price + 8,   # cap: allow 8¢ slippage above signal price
        pm_price - 8,   # floor: don't buy if price dropped >8¢
    )

    if pm_result is None:
        log.info("PM failed — clean abort, no Kalshi exposure")
        return {"success": False, "pm_filled": False, "kal_filled": False,
                "contracts": contracts, "directional": False,
                "error": "PM failed — clean abort", "skip_cooldown": True}

    # CRITICAL: PM filled. Set recovery flags in case of crash.
    actual_pm_price  = pm_result["price_cents"]
    actual_pm_shares = pm_result["shares"]
    _last_pm_filled = True
    _last_pm_shares = actual_pm_shares
    
    kal_contracts    = int(actual_pm_shares)

    if kal_contracts < 1:
        log.warning("PM filled but 0 effective contracts — rolling back PM")
        _rb0 = _sell_pm_fok(pm_token, actual_pm_shares)
        if _rb0 is None:
            _enqueue_rollback(pm_token, actual_pm_shares)
        # Clear recovery flags — we handled it
        _last_pm_filled = False
        return {"success": False, "pm_filled": True, "kal_filled": False,
                "directional": _rb0 is None,
                "error": "PM filled, 0 contracts" + (" — rolled back ✓" if _rb0 else " — STILL OPEN ⚠"),
                "pm_result": pm_result,
                "rollback_result": _rb0}

    # ── Step 2: Re-fetch Kalshi price + post-fill combined check ────────────
    try:
        import kalshi_auth as _ka
        _kal_h = _ka.signed_headers("GET", f"/trade-api/v2/markets/{kal_ticker}")
        _kal_r = requests.get(
            f"https://api.elections.kalshi.com/trade-api/v2/markets/{kal_ticker}",
            headers=_kal_h, timeout=4,
        )
        _kal_data   = _kal_r.json().get("market", {})
        # Normalize: Kalshi API may return decimal(0-1), cents(0-100), or millicents(>100)
        def _norm_k(v, d):
            f = float(v) if v is not None else float(d)
            if f <= 1.0:   return f * 100
            if f <= 100.0: return f
            return f / 100
        def _get_kal_field(key, fb):
            v = _kal_data.get(key + "_dollars") or _kal_data.get(key)
            return _norm_k(v, fb) if v is not None else float(fb)
        fresh_kal_price = (_get_kal_field("yes_ask", kal_price)
                           if kal_side.lower() == "yes"
                           else (100.0 - _get_kal_field("yes_bid", 100 - kal_price)))
        log.info("Post-fill Kalshi refresh: %.1f cents (was %.1f cents)", fresh_kal_price, kal_price)
    except Exception as _e:
        log.warning("Kalshi price refresh failed (%s) — using signal price %.1f¢", _e, kal_price)
        fresh_kal_price = kal_price

    post_fill_combined = actual_pm_price + fresh_kal_price
    if post_fill_combined >= 100:
        log.warning("Post-fill combined %.1f¢ >= 100¢ — rolling back PM to avoid loss",
                    post_fill_combined)
        rollback = _sell_pm_fok(pm_token, actual_pm_shares)
        if rollback:
            log.info("PM rollback successful: recovered $%.2f", rollback.get("cost", 0))
        else:
            log.error("PM ROLLBACK FAILED — directional exposure! %.2f shares @ %.1f¢",
                      actual_pm_shares, actual_pm_price)
            _enqueue_rollback(pm_token, actual_pm_shares)
        # Blacklist this candle to prevent rollback churn
        try:
            import matcher as _matcher_bl2
            _candle_ts2 = window.get("kalshi_market", {}).get("candle_end_ts", 0)
            if _candle_ts2:
                _matcher_bl2.blacklist_candle(asset, _candle_ts2)
        except Exception:
            pass
        # Clear recovery flags — we handled it
        _last_pm_filled = False
        return {
            "success": False, "pm_filled": True, "kal_filled": False,
            "contracts": kal_contracts, "pm_price": actual_pm_price,
            "directional": rollback is None,
            "error": f"Post-fill combined {post_fill_combined:.1f}¢ >= 100¢" +
                     (" — PM rolled back ✓" if rollback else " — PM STILL OPEN ⚠"),
            "pm_result": pm_result,
            "rollback_result": rollback,
        }

    # Re-apply a taker buffer to the refreshed Kalshi price.
    # Without this: order placed at exactly the ask; if book ticks 1¢ in the
    # few ms between our REST call and the order arriving, it goes resting.
    # Buffer values match the Step-0 defaults (YES: conservative +3¢; NO: +8¢).
    # The trade was already confirmed profitable at fresh_kal_price; paying a
    # small buffer still keeps combined well below 100¢.
    _pf_buf = 3 if kal_side.upper() == "YES" else 8
    kal_price_buffered = int(fresh_kal_price) + _pf_buf
    _pf_combined_check = actual_pm_price + kal_price_buffered
    if _pf_combined_check >= 100:
        # Buffered price wipes out the profit — only possible if refresh was at
        # a near-break-even price. Abort and rollback.
        log.warning(
            "Post-fill buffer check: %.1f¢ + %d¢ (Kal %d¢ + %d¢ buf) = %.1f¢ ≥ 100¢ — rolling back PM",
            actual_pm_price, kal_price_buffered, int(fresh_kal_price), _pf_buf, _pf_combined_check,
        )
        rollback = _sell_pm_fok(pm_token, actual_pm_shares)
        if rollback is None:
            _enqueue_rollback(pm_token, actual_pm_shares)
        # Blacklist this candle
        try:
            import matcher as _matcher_bl3
            _candle_ts3 = window.get("kalshi_market", {}).get("candle_end_ts", 0)
            if _candle_ts3:
                _matcher_bl3.blacklist_candle(asset, _candle_ts3)
        except Exception:
            pass
        # Clear recovery flags — we handled it
        _last_pm_filled = False
        return {
            "success": False, "pm_filled": True, "kal_filled": False,
            "contracts": kal_contracts, "pm_price": actual_pm_price,
            "directional": rollback is None,
            "error": f"Post-fill buffer combined {_pf_combined_check:.1f}¢ ≥ 100¢" +
                     (" — PM rolled back ✓" if rollback else " — PM STILL OPEN ⚠"),
            "pm_result": pm_result,
            "rollback_result": rollback,
        }
    log.info(
        "Post-fill buffer applied: Kal %d¢ + %d¢ buf = %d¢ | combined %.1f¢ (%.1f¢ profit)",
        int(fresh_kal_price), _pf_buf, kal_price_buffered,
        _pf_combined_check, 100 - _pf_combined_check,
    )
    kal_price = kal_price_buffered  # taker-safe order price

    # ── Step 3: Buy Kalshi ───────────────────────────────────────────────────
    # MAKER MODE: post a GTC LIMIT bid at the original signal price (no taker
    # buffer needed, 0% Kalshi maker fee). Waits up to MAKER_CANCEL_MINS_BEFORE_CLOSE
    # min before expiry, then cancels and falls through to PM rollback.
    # TAKER MODE (fallback): used when maker is disabled or candle is near-close.
    from config import KALSHI_MAKER_MODE, MAKER_CANCEL_MINS_BEFORE_CLOSE, MAKER_MIN_MINS_LEFT
    _win_mins_left = window.get("minutes_left", 0)
    
    # Exposure Throttle: Limit concurrent pending maker USD to avoid unhedged spikes
    with _pending_lock:
        _current_pending = _pending_maker_usd
        
    _use_maker = (KALSHI_MAKER_MODE
                  and _win_mins_left >= MAKER_MIN_MINS_LEFT
                  and _current_pending + pm_amount <= MAKER_MAX_PENDING_USD)
    
    if KALSHI_MAKER_MODE and not _use_maker:
        if _current_pending + pm_amount > MAKER_MAX_PENDING_USD:
            log.warning("Maker exposure throttle: $%.2f pending exceeds $%.2f limit — forcing TAKER fallback",
                        _current_pending + pm_amount, MAKER_MAX_PENDING_USD)
        elif _win_mins_left < MAKER_MIN_MINS_LEFT:
            log.info("Kalshi TAKER mode: %.1f min left < maker threshold", _win_mins_left)

    if _use_maker:
        # Use signal price (no taker buffer) — we're posting at our target, not chasing
        _kal_signal_price = int(window["kal_price"])
        log.info("Kalshi MAKER mode: posting %s @ %dc signal price (%.1f min left)",
                 kal_side, _kal_signal_price, _win_mins_left)
        kal_result = await _buy_kalshi_maker(
            kal_ticker, kal_side, _kal_signal_price, kal_contracts, _win_mins_left,
            pm_token, actual_pm_price, pm_amount
        )
        if kal_result and kal_result.get("drift_cancel"):
            log.info("Maker drift cancel — skipping taker fallback")
            # Blacklist this candle (drift = unstable book)
            try:
                import matcher as _matcher_bl4
                _candle_ts4 = window.get("kalshi_market", {}).get("candle_end_ts", 0)
                if _candle_ts4:
                    _matcher_bl4.blacklist_candle(asset, _candle_ts4)
            except Exception:
                pass
            kal_result = None
        elif kal_result is None:
            log.info("Kalshi maker timed out (T-%.1fm) — falling back to TAKER order at %d¢",
                     MAKER_CANCEL_MINS_BEFORE_CLOSE, int(kal_price))
            kal_result = _buy_kalshi_taker(kal_ticker, kal_side, int(kal_price), kal_contracts)
    else:
        kal_result = _buy_kalshi_taker(kal_ticker, kal_side, int(kal_price), kal_contracts)
        # NOTE: retry removed — it caused double-fills when the cancel race fired.
        # _buy_kalshi_taker now re-checks order status after cancel to detect race fills.

    # ── Outcome: Kalshi failed → roll back PM ────────────────────────────────
    if kal_result is None:
        log.warning("Kalshi failed after PM fill — rolling back PM (%.2f shares @ %.1f¢)",
                    actual_pm_shares, actual_pm_price)
        rollback = _sell_pm_fok(pm_token, actual_pm_shares)
        if rollback:
            log.info("PM rollback successful: recovered $%.2f from %.2f shares",
                     rollback.get("cost", 0), actual_pm_shares)
        else:
            log.error("PM ROLLBACK FAILED — directional exposure remains! %.2f shares @ %.1f¢",
                      actual_pm_shares, actual_pm_price)
            _enqueue_rollback(pm_token, actual_pm_shares)
            try:
                import notifier as _ntf_rb
                _ntf_rb._send(
                    f"🚨 <b>PM ROLLBACK FAILED</b>\n"
                    f"Naked: <b>{actual_pm_shares:.1f} shares @ {actual_pm_price:.1f}¢</b>\n"
                    f"Token: <code>{pm_token[:16]}…</code>\n"
                    f"Action: background retry queued (every 30s for 10min)"
                )
            except Exception as _ntf_e:
                log.error("Notifier error: %s", _ntf_e)
        # Blacklist this candle to prevent rollback churn
        try:
            import matcher as _matcher_bl
            _candle_ts = window.get("kalshi_market", {}).get("candle_end_ts", 0)
            if _candle_ts:
                _matcher_bl.blacklist_candle(asset, _candle_ts)
        except Exception as _bl_e:
            log.debug("Blacklist error: %s", _bl_e)
        # Clear recovery flags — we handled it
        _last_pm_filled = False
        return {
            "success":    False, "pm_filled": True, "kal_filled": False,
            "contracts":  kal_contracts, "pm_price": actual_pm_price,
            "directional": rollback is None,
            "error":      "Kalshi failed" + (" — PM rolled back ✓" if rollback else " — PM STILL OPEN ⚠"),
            "pm_result":  pm_result,
            "rollback_result": rollback,
        }

    # ── Outcome: both filled ✅ ───────────────────────────────────────────────
    actual_kal_price   = kal_result["price_cents"]
    actual_kal_filled  = kal_result.get("contracts", kal_contracts)

    # Partial Kalshi fill: if fewer contracts filled than PM shares bought, sell
    # the excess PM shares to eliminate naked directional exposure.
    pm_shares_gross = pm_result.get("shares", kal_contracts)
    excess_pm = round(pm_shares_gross - actual_kal_filled, 4)
    excess_pm_result = None
    if excess_pm > 1.0:
        log.warning(
            "Partial Kalshi fill (%d vs %d PM shares) — selling %.2f excess PM shares",
            actual_kal_filled, int(pm_shares_gross), excess_pm,
        )
        _excess_rollback = _sell_pm_fok(pm_token, excess_pm)
        if _excess_rollback:
            log.info("Excess PM shares sold: recovered $%.2f", _excess_rollback.get("cost", 0))
            excess_pm_result = _excess_rollback
        else:
            log.error("Excess PM rollback FAILED — %.2f shares unhedged", excess_pm)
            _enqueue_rollback(pm_token, excess_pm)
            try:
                import notifier as _ntf_ex
                _ntf_ex._send(
                    f"⚠️ <b>Partial fill — excess PM naked</b>\n"
                    f"Kalshi filled {actual_kal_filled} / {int(pm_shares_gross)} contracts\n"
                    f"Unhedged: <b>{excess_pm:.1f} shares @ {actual_pm_price:.1f}¢</b>"
                )
            except Exception:
                pass

    # ── Actual fill price lookup (maker mode improvement) ────────────────────
    # In maker mode, Kalshi may fill at a *better* price than the limit we posted
    # (e.g. posted 59¢, filled at 55.7¢ blended). Query the fills endpoint to
    # get the true VWAP rather than using the order price for cost/profit logging.
    _kal_order_id = kal_result.get("order_id")
    actual_kal_fill_price = actual_kal_price  # fallback: use order price
    if _kal_order_id:
        try:
            _fh2 = kalshi_auth.signed_headers("GET", "/trade-api/v2/portfolio/fills")
            _fr2 = requests.get(
                KALSHI_BASE_URL + f"/portfolio/fills?order_id={_kal_order_id}&limit=50",
                headers=_fh2, timeout=6,
            )
            if _fr2.ok:
                _fills2 = _fr2.json().get("fills", [])
                _tot2 = float(sum(float(f.get("count_fp", 0)) for f in _fills2))
                if _tot2 > 0:
                    _price_field2 = "yes_price_dollars" if kal_side.lower() == "yes" else "no_price_dollars"
                    _actual_vwap = (
                        sum(float(f.get(_price_field2, 0)) * float(f.get("count_fp", 0))
                            for f in _fills2) / _tot2 * 100
                    )
                    log.info(
                        "Kalshi actual fill VWAP: %.2f¢ (order price was %d¢, saved $%.2f)",
                        _actual_vwap, actual_kal_price,
                        (actual_kal_price - _actual_vwap) * actual_kal_filled / 100,
                    )
                    actual_kal_fill_price = _actual_vwap
        except Exception as _fe:
            log.debug("Kalshi fills lookup failed (%s) — using order price for cost", _fe)

    profit_locked     = actual_kal_filled * (100 - actual_pm_price - actual_kal_fill_price) / 100
    pm_cost_usd       = round(pm_result.get("cost", 0), 4)
    pm_shares         = round(pm_result.get("shares", 0), 4)
    kal_cost_usd      = round(actual_kal_filled * actual_kal_fill_price / 100, 4)

    # SUCCESS: Both legs hedged. Clear recovery flags.
    _last_pm_filled = False
    return {
        "success":        True,
        "pm_filled":      True,
        "kal_filled":     True,
        "contracts":      kal_contracts,
        "pm_price":       actual_pm_price,
        "kal_price":      actual_kal_fill_price,   # actual fill VWAP (not order price)
        "kal_order_price": actual_kal_price,        # original order price for reference
        "pm_usd":         pm_cost_usd,
        "kal_usd":        kal_cost_usd,
        "pm_shares":      pm_shares,
        "total_cost_usd": round(pm_cost_usd + kal_cost_usd, 4),
        "proceeds_usd":   round(kal_contracts * 1.0, 4),
        "profit_locked":  profit_locked,
        "directional":    False,
        "pm_result":      pm_result,
        "kal_result":     kal_result,
        "excess_pm_result": excess_pm_result,
        "excess_pm_shares": excess_pm,
    }
