"""
rebalancer.py — Auto-rebalance PM USDC.e ↔ Kalshi via Polygon native USDC.

Forward (PM → Kalshi):
  1. Swap USDC.e → native USDC via Uniswap v3 (fee tier 500, ~1:1)
  2. Send native USDC to Kalshi Zerohash deposit address
  3. Zerohash credits Kalshi account within ~5 min (minus 0.4% fee)

Reverse (Kalshi → PM):
  1. Call Kalshi withdrawal API to send USDC to our Polygon wallet via Zerohash
  2. Poll for native USDC to arrive on-chain (up to 15 min)
  3. Swap native USDC → USDC.e via Uniswap v3 (same pool, reversed)

Trigger: called from balance_monitor.py
Safety:  shared 24h cooldown between forward and reverse; venue floors enforced
"""
import json
import logging
import os
import time
import uuid
import requests
from web3 import Web3

log = logging.getLogger("rebalancer")

# ── Config ────────────────────────────────────────────────────────────────────
POLYGON_RPC         = "https://polygon-bor-rpc.publicnode.com"
USDC_E              = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"   # bridged
NATIVE_USDC         = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"   # Circle native
UNISWAP_ROUTER      = "0xE592427A0AEce92De3Edee1F18E0157C05861564"   # v3 Polygon
KALSHI_DEPOSIT_ADDR = "0xF2055c918D8635879973025Ad7145E370A0991cF"   # Zerohash/Polygon
UNISWAP_FEE_TIER    = 500       # 0.05% — confirmed working for USDC.e/USDC pair
REBALANCE_COOLDOWN  = 86_400    # 24h between rebalances
SWAP_SLIPPAGE       = 0.995     # accept 0.5% slippage on swap

_COOLDOWN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rebalance_state.json")


def _load_last_rebalance_ts() -> float:
    """Load persisted rebalance timestamp from disk (survives restarts)."""
    try:
        with open(_COOLDOWN_FILE) as f:
            return float(json.load(f).get("last_rebalance_ts", 0.0))
    except Exception:
        return 0.0


def _save_last_rebalance_ts(ts: float):
    try:
        with open(_COOLDOWN_FILE, "w") as f:
            json.dump({"last_rebalance_ts": ts}, f)
    except Exception as e:
        log.warning("Failed to persist rebalance cooldown: %s", e)


_last_rebalance_ts: float = _load_last_rebalance_ts()   # survives bot restarts

ERC20_ABI = [
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf",
     "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
     "name":"approve","outputs":[{"name":"","type":"bool"}],
     "stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"to","type":"address"},{"name":"amount","type":"uint256"}],
     "name":"transfer","outputs":[{"name":"","type":"bool"}],
     "stateMutability":"nonpayable","type":"function"},
]

ROUTER_ABI = [{"inputs":[{"components":[
    {"name":"tokenIn","type":"address"},{"name":"tokenOut","type":"address"},
    {"name":"fee","type":"uint24"},{"name":"recipient","type":"address"},
    {"name":"deadline","type":"uint256"},{"name":"amountIn","type":"uint256"},
    {"name":"amountOutMinimum","type":"uint256"},{"name":"sqrtPriceLimitX96","type":"uint160"}
],"name":"params","type":"tuple"}],"name":"exactInputSingle",
"outputs":[{"name":"amountOut","type":"uint256"}],
"stateMutability":"payable","type":"function"}]


def _send_tx(w3, account, tx, label, timeout=90):
    signed = w3.eth.account.sign_transaction(tx, account.key)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    log.info("%s TX: https://polygonscan.com/tx/%s", label, h.hex())
    r = w3.eth.wait_for_transaction_receipt(h, timeout=timeout)
    if r.status != 1:
        raise RuntimeError(f"{label} transaction failed")
    return r


def rebalance(amount_usd: float, kalshi_cash: float, pm_usdc: float,
              tg_token: str = None, tg_chat: str = None) -> bool:
    """
    Swap `amount_usd` USDC.e → native USDC and send to Kalshi deposit address.
    Returns True on success, False on failure.
    """
    global _last_rebalance_ts

    # ── Cooldown check ────────────────────────────────────────────────────────
    elapsed = time.time() - _last_rebalance_ts
    if elapsed < REBALANCE_COOLDOWN:
        log.info("Rebalance cooldown active (%.1fh remaining) — skipping",
                 (REBALANCE_COOLDOWN - elapsed) / 3600)
        return False

    amount_raw = int(amount_usd * 1_000_000)
    log.info("Rebalancing: swapping $%.2f USDC.e → native USDC → Kalshi", amount_usd)

    try:
        privkey  = os.environ.get("PM_PRIVATE_KEY", os.environ.get("PRIVATE_KEY", ""))
        w3       = Web3(Web3.HTTPProvider(POLYGON_RPC))
        account  = w3.eth.account.from_key(privkey)
        gas_px   = int(w3.eth.gas_price * 1.5)

        usdc_e_c   = w3.eth.contract(address=Web3.to_checksum_address(USDC_E),        abi=ERC20_ABI)
        native_c   = w3.eth.contract(address=Web3.to_checksum_address(NATIVE_USDC),   abi=ERC20_ABI)
        router_c   = w3.eth.contract(address=Web3.to_checksum_address(UNISWAP_ROUTER), abi=ROUTER_ABI)

        bal_e = usdc_e_c.functions.balanceOf(account.address).call()
        if bal_e < amount_raw:
            log.warning("Insufficient USDC.e for rebalance ($%.2f available, $%.2f needed)",
                        bal_e/1e6, amount_usd)
            return False

        # Step 1: approve Uniswap router
        nonce = w3.eth.get_transaction_count(account.address, "pending")
        approve_tx = usdc_e_c.functions.approve(
            Web3.to_checksum_address(UNISWAP_ROUTER), amount_raw * 2
        ).build_transaction({"chainId": 137, "gas": 80_000, "gasPrice": gas_px, "nonce": nonce})
        _send_tx(w3, account, approve_tx, "Approve USDC.e")
        nonce += 1
        time.sleep(2)

        # Step 2: swap USDC.e → native USDC
        nat_before = native_c.functions.balanceOf(account.address).call()
        swap_tx = router_c.functions.exactInputSingle((
            Web3.to_checksum_address(USDC_E),
            Web3.to_checksum_address(NATIVE_USDC),
            UNISWAP_FEE_TIER,
            account.address,
            int(time.time()) + 300,
            amount_raw,
            int(amount_raw * SWAP_SLIPPAGE),
            0
        )).build_transaction({"chainId": 137, "gas": 220_000, "gasPrice": gas_px,
                               "nonce": nonce, "value": 0})
        _send_tx(w3, account, swap_tx, "Swap USDC.e→USDC")
        nonce += 1
        time.sleep(2)

        nat_after    = native_c.functions.balanceOf(account.address).call()
        received_raw = nat_after - nat_before
        received_usd = received_raw / 1e6
        log.info("Swap received: $%.4f native USDC", received_usd)

        if received_raw < 1:
            log.error("Swap produced 0 native USDC — aborting transfer")
            return False

        # Step 3: send native USDC to Kalshi
        gas_px2   = int(w3.eth.gas_price * 1.5)
        nonce     = w3.eth.get_transaction_count(account.address, "pending")
        transfer_tx = native_c.functions.transfer(
            Web3.to_checksum_address(KALSHI_DEPOSIT_ADDR), received_raw
        ).build_transaction({"chainId": 137, "gas": 80_000, "gasPrice": gas_px2, "nonce": nonce})
        _send_tx(w3, account, transfer_tx, "Transfer→Kalshi")

        expected_kalshi = received_usd * 0.996  # after 0.4% Zerohash fee
        log.info("Rebalance complete — ~$%.2f should appear in Kalshi within 5 min", expected_kalshi)

        _last_rebalance_ts = time.time()
        _save_last_rebalance_ts(_last_rebalance_ts)

        # Telegram alert
        if tg_token and tg_chat:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={"chat_id": tg_chat, "parse_mode": "HTML", "text": (
                        f"\u2696\ufe0f <b>[REBALANCE]</b> PM \u2192 Kalshi\n"
                        f"Swapped ${amount_usd:.2f} USDC.e \u2192 ${received_usd:.4f} native USDC\n"
                        f"~<b>${expected_kalshi:.2f}</b> arriving in Kalshi (~5 min)\n"
                        f"Kalshi was: ${kalshi_cash:.2f} | PM was: ${pm_usdc:.2f}"
                    )},
                    timeout=5,
                )
            except Exception:
                pass

        return True

    except Exception as e:
        log.error("Rebalance failed: %s", e, exc_info=True)
        return False


def should_rebalance(kalshi_cash: float, pm_usdc: float,
                     trigger_usd: float, target_usd: float,
                     pm_floor_usd: float, min_amount_usd: float) -> tuple:
    """
    Returns (should_rebalance: bool, amount_usd: float).
    amount = how much to send to bring Kalshi to target_usd.
    """
    if kalshi_cash >= trigger_usd:
        return False, 0.0

    amount = target_usd - kalshi_cash
    amount = max(amount, min_amount_usd)

    if pm_usdc - amount < pm_floor_usd:
        log.warning("Rebalance needed ($%.2f) but PM floor $%.2f would be breached "
                    "(PM has $%.2f) — skipping", amount, pm_floor_usd, pm_usdc)
        return False, 0.0

    return True, amount


# ── Reverse rebalancer: Kalshi → PM ──────────────────────────────────────────

def reverse_rebalance(amount_usd: float, kalshi_cash: float, pm_usdc: float,
                      tg_token: str = None, tg_chat: str = None) -> bool:
    """
    Kalshi has no programmatic withdrawal API — withdrawals must be done
    through kalshi.com web UI.

    This function fires a Telegram alert and sets the cooldown so it doesn't
    spam every check cycle. The alert tells you exactly what to do.

    Manual steps:
      1. Go to kalshi.com → Wallet → Withdraw
      2. Withdraw ~$amount_usd USDC to your Polygon wallet
      3. Native USDC arrives → bot's forward rebalancer won't be needed for a while
         (or manually swap native USDC → USDC.e on Uniswap)
    """
    global _last_rebalance_ts

    elapsed = time.time() - _last_rebalance_ts
    if elapsed < REBALANCE_COOLDOWN:
        log.info("Reverse rebalance cooldown active (%.1fh remaining) — skipping alert",
                 (REBALANCE_COOLDOWN - elapsed) / 3600)
        return False

    log.warning(
        "⚠️ PM wallet low ($%.2f) — manual Kalshi withdrawal needed. "
        "Go to kalshi.com → Wallet → Withdraw ~$%.2f USDC to Polygon wallet.",
        pm_usdc, amount_usd,
    )

    _last_rebalance_ts = time.time()
    _save_last_rebalance_ts(_last_rebalance_ts)

    if tg_token and tg_chat:
        try:
            requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={"chat_id": tg_chat, "parse_mode": "HTML", "text": (
                    f"⚠️ <b>[ACTION REQUIRED] PM Wallet Low</b>\n"
                    f"PM USDC.e: <b>${pm_usdc:.2f}</b> (below floor)\n"
                    f"Kalshi cash: <b>${kalshi_cash:.2f}</b>\n\n"
                    f"<b>Manual steps:</b>\n"
                    f"1. Go to <a href=\"https://kalshi.com\">kalshi.com</a> → Wallet → Withdraw\n"
                    f"2. Withdraw <b>~${amount_usd:.0f} USDC</b> to Polygon wallet\n"
                    f"   <code>{addr or 'see config/.env PM_FUNDER'}</code>\n"
                    f"3. Native USDC arrives → bot resumes arbs automatically\n\n"
                    f"<i>(Kalshi API has no programmatic withdrawal — manual only)</i>"
                )},
                timeout=5,
            )
        except Exception:
            pass

    return True


def should_reverse_rebalance(kalshi_cash: float, pm_usdc: float,
                              trigger_usd: float, target_usd: float,
                              kalshi_floor_usd: float, min_amount_usd: float) -> tuple:
    """
    Returns (should_reverse_rebalance: bool, amount_usd: float).
    Triggers when PM USDC.e < trigger_usd AND Kalshi has enough headroom above its floor.
    """
    if pm_usdc >= trigger_usd:
        return False, 0.0

    amount = target_usd - pm_usdc
    amount = max(amount, min_amount_usd)

    if kalshi_cash - amount < kalshi_floor_usd:
        available = kalshi_cash - kalshi_floor_usd
        if available >= min_amount_usd:
            amount = available  # send what we can without breaching floor
            log.info("Reverse rebalance: capping amount to $%.2f (Kalshi floor $%.2f)",
                     amount, kalshi_floor_usd)
        else:
            log.warning("Reverse rebalance needed ($%.2f) but Kalshi floor $%.2f would be breached "
                        "(Kalshi has $%.2f) — skipping", amount, kalshi_floor_usd, kalshi_cash)
            return False, 0.0

    return True, amount
