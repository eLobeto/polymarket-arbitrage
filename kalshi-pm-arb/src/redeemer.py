"""redeemer.py — Auto-redeem resolved Polymarket positions back to USDC.e.

Calls the Gnosis CTF (ConditionalTokens) contract on Polygon to burn winning
conditional tokens and return USDC.e to the wallet.  No-ops gracefully if
nothing is redeemable.
"""
import logging
import os
import time
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / 'config' / '.env')
import requests

import trade_logger as _tl
import notifier as _ntf


def _get_settle_oracle_snapshot(asset: str, kal_ticker: str) -> tuple[float | None, float | None]:
    """
    Capture oracle divergence at settlement time for middling fingerprint analysis.

    Returns (oracle_divergence_usd, spot_price) where:
      - oracle_divergence_usd = abs(CF_Benchmarks_strike - Binance_spot)
      - spot_price = current Binance spot (used to confirm which oracle moved)

    Both may be None if feeds are unavailable.
    """
    try:
        import matcher as _m
        # Get current Binance spot (cached, fast)
        spot, _src = _m._get_oracle_price(asset)
        if spot is None:
            return None, None

        # Get Kalshi floor_strike from settlements API — the CF Benchmarks
        # strike that actually settled this contract.
        # Fall back to looking it up from the open_arbs fill record.
        kal_strike = None
        try:
            from balance_monitor import signed_headers, KALSHI_BASE_URL
            r = requests.get(
                f"{KALSHI_BASE_URL}/portfolio/settlements?limit=20",
                headers=signed_headers("GET", f"/trade-api/v2/portfolio/settlements?limit=20"),
                timeout=10,
            )
            if r.ok:
                for s in r.json().get("settlements", []):
                    if s.get("ticker") == kal_ticker:
                        kal_strike = float(s.get("floor_strike", 0))
                        break
        except Exception:
            pass

        div = abs(kal_strike - spot) if kal_strike else None
        return div, spot
    except Exception as e:
        log.debug("Settle oracle snapshot failed for %s: %s", asset, e)
        return None, None

log = logging.getLogger("REDEEMER")

_CTF_ADDRESS  = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_USDC_E       = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
_POLYGON_RPC  = "https://polygon-bor-rpc.publicnode.com"
_CTF_ABI      = [
    {
        "inputs": [
            {"name": "collateralToken",     "type": "address"},
            {"name": "parentCollectionId",  "type": "bytes32"},
            {"name": "conditionId",         "type": "bytes32"},
            {"name": "indexSets",           "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "type": "function",
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id",      "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
        "stateMutability": "view",
    },
]

# Max positions to redeem per cycle — delegated Polygon accounts have strict
# nonce handling, so we limit to 3 per 5-min cycle to avoid gapped-nonce errors
_MAX_REDEEM_PER_CYCLE = 3


def _check_kalshi_revenue(kal_ticker: str) -> float | None:
    """Check Kalshi settlements API for revenue on a specific ticker.

    Returns:
        Revenue in USD (0.0 = we lost), or None if not found / API error.
    """
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from balance_monitor import signed_headers, KALSHI_BASE_URL

        cursor = None
        for _ in range(100):
            params = {"limit": "100"}
            if cursor:
                params["cursor"] = cursor
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            sign_path = f"/trade-api/v2/portfolio/settlements?{qs}"
            headers = signed_headers("GET", sign_path)
            r = requests.get(
                f"{KALSHI_BASE_URL}/portfolio/settlements?{qs}",
                headers=headers, timeout=10,
            )
            if not r.ok:
                return None
            data = r.json()
            for s in data.get("settlements", []):
                if s.get("ticker") == kal_ticker:
                    return float(s.get("revenue", 0)) / 100  # cents → dollars
            cursor = data.get("cursor")
            if not cursor:
                break
        return None  # ticker not found in settlements
    except Exception as e:
        log.warning("[MIDDLED] Kalshi settlement check failed for %s: %s", kal_ticker, e)
        return None


def redeem_winning_positions() -> float:
    """Scan for redeemable positions and redeem them on-chain.

    Returns:
        Total USDC value redeemed (0.0 if nothing to do or on error).
    """
    addr        = os.environ.get("PM_FUNDER", os.environ.get("FUNDER_ADDRESS", ""))
    private_key = os.environ.get("PM_PRIVATE_KEY", os.environ.get("PRIVATE_KEY", ""))
    if not addr or not private_key:
        log.warning("[REDEEM] FUNDER_ADDRESS or PRIVATE_KEY not set — skipping")
        return 0.0

    try:
        r = requests.get(
            f"https://data-api.polymarket.com/positions"
            f"?user={addr}&sizeThreshold=.1",
            timeout=30,
        )
        r.raise_for_status()
        all_positions = r.json()

        # Log Kalshi wins OR middled trades: positions where PM tokens are worthless.
        # NOTE: PM marks ALL resolved positions as redeemable=True (even losers),
        # so we cannot filter on `not redeemable`. Instead, check any position
        # with currentValue=0 that we have an open arb entry for.
        for p in all_positions:
            cid = p.get("conditionId", "")
            if not cid:
                continue
            current_val = float(p.get("currentValue", 0))
            # Worthless PM tokens + we have an open arb tracking entry for it
            if current_val == 0:
                entry = _tl.resolve_open_arb(cid)
                if entry:
                    # Check if this is a true Kalshi win or a middled trade
                    # by looking up the original fill and checking Kalshi settlement
                    kal_ticker = entry.get("kal_ticker", "")
                    original = _tl._lookup_arb_fill_record(cid)
                    if original and kal_ticker:
                        kal_revenue = _check_kalshi_revenue(kal_ticker)
                        if kal_revenue is not None and kal_revenue == 0:
                            # Kalshi also lost → MIDDLED (oracle divergence)
                            pm_loss = float(original.get("pm_cost_usd", 0))
                            kal_loss = float(original.get("kal_cost_usd", 0))
                            _div_settle, _spot_settle = _get_settle_oracle_snapshot(
                                entry.get("asset", ""), kal_ticker
                            )
                            _tl.log_arb_outcome(cid, "middled", 0.0,
                                                pm_loss_usd=pm_loss,
                                                kal_loss_usd=kal_loss,
                                                oracle_divergence_at_settle=_div_settle,
                                                spot_at_settle=_spot_settle)
                            _ntf.arb_middled(
                                asset=entry.get("asset", "?"),
                                tf=entry.get("tf", "15m"),
                                pm_loss=pm_loss,
                                kal_loss=kal_loss,
                                kal_ticker=kal_ticker,
                            )
                            log.warning("[MIDDLED] Both sides lost on %s — pm_loss=$%.2f kal_loss=$%.2f total=$%.2f",
                                        kal_ticker, pm_loss, kal_loss, pm_loss + kal_loss)
                            continue
                    # Normal Kalshi win — notify
                    _fill_profit = _tl._lookup_arb_fill_profit(cid)
                    from fee_regime import FeeRegime
                    _kalshi_fee = FeeRegime.kalshi_fee_usd(_fill_profit, mode="taker") if _fill_profit > 0 else 0.0
                    _div_settle_kal, _spot_settle_kal = _get_settle_oracle_snapshot(
                        entry.get("asset", ""), kal_ticker
                    )
                    _tl.log_arb_outcome(cid, "kalshi", 0.0,
                                        oracle_divergence_at_settle=_div_settle_kal,
                                        spot_at_settle=_spot_settle_kal)
                    try:
                        _net_profit = FeeRegime.net_profit_usd(_fill_profit, mode="taker") if _fill_profit > 0 else 0.0
                        _ntf.arb_won(
                            asset=entry.get("asset", "?"),
                            tf=entry.get("tf", "15m"),
                            winning_side="kalshi",
                            profit_usd=_net_profit,
                            kal_ticker=kal_ticker,
                            kalshi_fee=_kalshi_fee,
                        )
                        log.info("[REDEEM] Kalshi win alert sent: %s profit=$%.2f fee=$%.2f",
                                 kal_ticker, _net_profit, _kalshi_fee)
                    except Exception as _kal_e:
                        log.warning("[REDEEM] Kalshi win alert failed: %s", _kal_e)

        # Filter to redeemable positions with real value.
        # Require currentValue > 0.01 upfront — PM sets redeemable=True on ALL
        # resolved positions including losers (cv=0), so we'd spam logs with
        # hundreds of zero-value positions without this guard.
        # Fresh wins where PM data-api hasn't updated cv yet will be caught on
        # the next cycle (typically within 5 min).
        positions = [
            p for p in all_positions
            if p.get("redeemable")
            and float(p.get("size", 0)) > 0.01
            and float(p.get("currentValue", 0)) > 0.01
        ]
        if not positions:
            log.debug("[REDEEM] No redeemable positions")
            return 0.0

        log.info("[REDEEM] %d redeemable position(s) found", len(positions))

        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware

        w3 = Web3(Web3.HTTPProvider(_POLYGON_RPC))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        wallet    = w3.eth.account.from_key(private_key)
        ctf       = w3.eth.contract(
            address=Web3.to_checksum_address(_CTF_ADDRESS),
            abi=_CTF_ABI,
        )
        usdc_e    = Web3.to_checksum_address(_USDC_E)
        nonce     = w3.eth.get_transaction_count(wallet.address, "pending")
        gas_price = int(w3.eth.gas_price * 1.5)
        total_val = 0.0
        redeemed  = 0

        # Sort by size descending — redeem biggest positions first
        positions.sort(key=lambda p: float(p.get("size", 0)), reverse=True)

        for p in positions:
            if redeemed >= _MAX_REDEEM_PER_CYCLE:
                log.info("[REDEEM] Hit per-cycle limit (%d) — remaining %d positions will be redeemed next cycle",
                         _MAX_REDEEM_PER_CYCLE, len(positions) - redeemed)
                break

            cond_id = p.get("conditionId", "")
            size    = float(p.get("size", 0))
            val     = float(p.get("currentValue", 0))
            title   = p.get("title", "")[:50]

            if not cond_id:
                continue

            # On-chain balanceOf check — eliminates already-redeemed positions.
            # currentValue was already checked in the outer filter (> 0.01).
            token_id = p.get("asset", "")
            if token_id:
                try:
                    on_chain_bal = ctf.functions.balanceOf(
                        Web3.to_checksum_address(addr), int(token_id)
                    ).call() / 1e6
                    if on_chain_bal < 0.01:
                        continue  # already redeemed
                    # Use currentValue (from PM API) as dollar value — NOT
                    # on_chain_bal which is share count, not dollar amount.
                    # val is already set from p["currentValue"] above.
                except Exception as _be:
                    log.debug("[REDEEM] On-chain balance check failed for %s: %s — skipping", title, _be)
                    continue
            else:
                continue  # no token_id, can't verify

            cond_b  = bytes.fromhex(
                cond_id[2:] if cond_id.startswith("0x") else cond_id
            )
            try:
                tx = ctf.functions.redeemPositions(
                    usdc_e, bytes(32), cond_b, [1, 2]
                ).build_transaction({
                    "from":     wallet.address,
                    "nonce":    nonce,
                    "gasPrice": gas_price,
                    "gas":      200_000,
                    "chainId":  137,
                })
                signed  = w3.eth.account.sign_transaction(tx, private_key)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                log.info("[REDEEM] ✅ $%.2f  %s  tx=%s...", val, title, tx_hash.hex()[:20])
                total_val += val
                redeemed  += 1
                # Log PM win for side-win-rate tracking + send alert
                try:
                    # Look up original fill to calculate actual profit
                    _orig = _tl._lookup_arb_fill_record(cond_id)
                    _asset_pm = (_orig.get("asset", "") if _orig else "") or p.get("asset", "")[:3].upper()
                    _kal_tk_pm = (_orig.get("kal_ticker", "") if _orig else "")
                    _div_settle_pm, _spot_settle_pm = _get_settle_oracle_snapshot(_asset_pm, _kal_tk_pm)
                    _tl.log_arb_outcome(cond_id, "pm", val,
                                        oracle_divergence_at_settle=_div_settle_pm,
                                        spot_at_settle=_spot_settle_pm)
                    if _orig:
                        _total_cost = float(_orig.get("total_cost_usd", 0))
                        _real_profit = val - _total_cost
                        log.info("[REDEEM] PM win alert: asset=%s profit=$%.2f",
                                 _orig.get("asset", "?"), _real_profit)
                        _ntf.arb_won(
                            asset=_orig.get("asset", "?"),
                            tf=_orig.get("tf", "15m"),
                            winning_side="pm",
                            profit_usd=_real_profit,
                        )
                    else:
                        # Check if this is a div fade position (separate positions log)
                        _pm_token_id = p.get("asset", "")
                        try:
                            from div_fade_logger import lookup_div_fade_position, update_div_fade_outcome
                            _fade_pos = lookup_div_fade_position(_pm_token_id)
                            if _fade_pos:
                                _fade_cost   = float(_fade_pos.get("cost_usd", 0))
                                _fade_profit = round(val - _fade_cost, 4)
                                _fade_shares = float(_fade_pos.get("shares", 0))
                                _fade_fill   = float(_fade_pos.get("fill_price_cents", 0))
                                _fade_asset  = _fade_pos.get("asset", "?")
                                _fade_signal = _fade_pos.get("signal", "?")
                                update_div_fade_outcome(_pm_token_id, "win", _fade_profit)
                                _ntf.div_fade_won(
                                    asset=_fade_asset,
                                    signal=_fade_signal,
                                    shares=_fade_shares,
                                    fill_price_cents=_fade_fill,
                                    cost_usd=_fade_cost,
                                    profit_usd=_fade_profit,
                                )
                                log.info("[REDEEM] Div fade win: %s %s profit=$%.2f",
                                         _fade_asset, _fade_signal, _fade_profit)
                            else:
                                log.warning("[REDEEM] No fill record for cond=%s token=%s — alert skipped",
                                            cond_id[:16], _pm_token_id[:16])
                        except Exception as _fe:
                            log.warning("[REDEEM] Div fade lookup failed: %s", _fe)
                except Exception as _le:
                    log.warning("[REDEEM] outcome alert failed: %s", _le, exc_info=True)

                # Wait for tx to confirm and re-fetch nonce from chain.
                # Delegated Polygon accounts need the previous tx to fully
                # confirm before accepting the next one.
                if redeemed < _MAX_REDEEM_PER_CYCLE:
                    log.info("[REDEEM] Waiting for tx confirmation before next redemption...")
                    _confirmed = False
                    for _wait in range(15):  # up to 30s
                        time.sleep(2.0)
                        fresh_nonce = w3.eth.get_transaction_count(wallet.address, "latest")
                        if fresh_nonce > nonce:
                            nonce = fresh_nonce
                            _confirmed = True
                            log.info("[REDEEM] Tx confirmed (nonce %d), proceeding", nonce)
                            break
                    if not _confirmed:
                        log.warning("[REDEEM] Tx not confirmed after 30s — stopping batch")
                        break
                    gas_price = int(w3.eth.gas_price * 1.5)

            except Exception as exc:
                err_str = str(exc)
                if "gapped-nonce" in err_str or "in-flight" in err_str:
                    log.warning("[REDEEM] ⚠️ Nonce issue on %s — stopping batch (will retry next cycle): %s",
                                title, err_str)
                    break  # Don't continue with stale nonces
                log.warning("[REDEEM] ❌ $%.2f  %s  error=%s", val, title, exc)

        return total_val

    except Exception as exc:
        log.warning("[REDEEM] Scan failed: %s", exc)
        return 0.0
