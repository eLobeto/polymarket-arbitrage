"""notifier.py — Telegram alerts for cross-candle arb bot."""
import logging
import requests
from config import TG_TOKEN, TG_CHAT_ID
from event_log import write as event_write

log = logging.getLogger("notifier")


def _send(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
        if not r.ok:
            log.warning("Telegram API error %d: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("Telegram send error: %s", e)


def arb_detected(window: dict):
    pass  # silent — only notify on completed fills


def both_filled(result: dict, window: dict):
    asset     = window.get("asset", "?")
    tf        = window.get("timeframe", "15m")
    pm_side   = window.get("pm_side", "?")
    kal_side  = window.get("kal_side", "?")
    pm_p      = result.get("pm_price", 0)
    kal_p     = result.get("kal_price", 0)
    contracts = result.get("contracts", 0)
    pm_usd    = result.get("pm_usd", pm_p / 100 * contracts)
    kal_usd   = result.get("kal_usd", kal_p / 100 * contracts)
    combined  = pm_p + kal_p

    # Risk context
    dz      = window.get("dead_zone")
    dz_str  = f"${dz:.0f}" if dz is not None and dz >= 0 else ("n/a" if dz == -1.0 else "0")
    div     = window.get("oracle_divergence", 0)
    allow   = window.get("oracle_allowed", False)
    warning = " ⚠️" if (allow or div > 20) else ""

    log.info("Sending fill alert: %s %s combined=%.1f¢", asset, tf, combined)
    _send(
        f"🔄 <b>ARB ENTERED</b> — {asset} {tf}\n"
        f"PM {pm_side} @ {pm_p:.1f}¢ (${pm_usd:.2f}) + Kal {kal_side} @ {kal_p:.1f}¢ (${kal_usd:.2f})\n"
        f"Combined: {combined:.1f}¢ | Contracts: {contracts}\n"
        f"Dead zone: {dz_str} | Oracle div: ${div:.0f}{warning}\n"
        f"<i>Awaiting settlement…</i>"
    )


def arb_won(asset: str, tf: str, winning_side: str, profit_usd: float,
            kal_ticker: str = "", kalshi_fee: float = 0.0):
    """Alert when an arb resolves as a confirmed win."""
    side_label = "PM redeemed" if winning_side == "pm" else "Kalshi settled"
    fee_note = f" (fee: ${kalshi_fee:.2f})" if kalshi_fee > 0.01 else ""
    _send(
        f"✅ <b>ARB WON</b> — {asset} {tf}\n"
        f"{side_label}{fee_note}\n"
        f"Profit: <b>${profit_usd:.2f}</b>"
    )


def div_fade_won(asset: str, signal: str, shares: float, fill_price_cents: float,
                 cost_usd: float, profit_usd: float):
    """Alert when a live div fade position is redeemed as a win."""
    roi_pct = (profit_usd / cost_usd * 100) if cost_usd > 0 else 0.0
    _send(
        f"🎯 <b>DIV FADE WIN</b> — {asset} 5m {signal}\n"
        f"{shares:.1f} shares @ {fill_price_cents:.1f}¢ fill\n"
        f"Profit: <b>+${profit_usd:.2f}</b> ({roi_pct:.0f}% ROI)"
    )


def arb_middled(asset: str, tf: str, pm_loss: float, kal_loss: float,
                kal_ticker: str = ""):
    """Alert when an arb is middled (both sides lost)."""
    total = pm_loss + kal_loss
    _send(
        f"💀 <b>ARB MIDDLED</b> — {asset} {tf}\n"
        f"Both sides lost! PM: -${pm_loss:.2f} | Kal: -${kal_loss:.2f}\n"
        f"Total loss: <b>-${total:.2f}</b>"
    )


def one_sided(result: dict, window: dict):
    pm_price  = result.get("pm_price", window.get("pm_price", 0))
    contracts = result.get("contracts", 0)
    exposure  = pm_price / 100 * contracts
    signal    = window.get("profit_cents", 0)
    asset     = window.get("asset", "?")
    timeframe = window.get("timeframe", "15m")
    pm_side   = window.get("pm_side", "?")
    err       = result.get("error", "Kalshi no-fill")

    if result.get("depth_gate_directional"):
        # Intentional: Kalshi had zero depth, bot entered PM-only by design
        _send(
            f"🎯 <b>[DIRECTIONAL]</b> Depth-gate PM-only entry\n"
            f"{asset} {timeframe} | {pm_side} @ {pm_price:.1f}¢\n"
            f"Signal: {signal:.1f}¢ | Shares: {contracts} | Exposure: ~${exposure:.2f}\n"
            f"<i>Kalshi depth = 0 — entered PM directionally</i>"
        )
        return

    # Check rollback status (present when Kalshi failed after PM filled)
    rollback_ok   = result.get("directional") is False and "rolled back" in err
    rollback_fail = result.get("directional") is True  and "STILL OPEN" in err

    if rollback_ok:
        pm_result = result.get("pm_result") or {}
        cost = float(pm_result.get("cost", exposure))
        recovered = float(result.get("rollback_proceeds", cost))
        slippage = cost - recovered
        event_write(
            bot="kalshi-pm-arb", event="rollback_ok",
            asset=asset, side=pm_side,
            size_usdc=exposure,
            profit=-abs(slippage),
            note=f"rollback success {timeframe} @ {pm_price:.1f}¢ slippage=${slippage:.3f}",
        )
        _send(
            f"↩️ <b>[ROLLBACK OK]</b> {asset} {timeframe} {pm_side}\n"
            f"Kalshi failed → PM sold back\n"
            f"Cost: ${cost:.2f} | Recovered: ${recovered:.2f} | Friction: <b>${slippage:.2f}</b>"
        )
        return

    if rollback_fail:
        # Both Kalshi leg AND rollback failed — naked position
        pm_token = (result.get("pm_result") or {}).get("token_id", "")
        _send(
            f"🚨 <b>PM ROLLBACK FAILED</b>\n"
            f"Naked: <b>{contracts:.0f} shares @ {pm_price:.1f}¢</b>\n"
            f"Token: <code>{pm_token[:20]}…</code>\n"
            f"Asset: {asset} {timeframe} | Side: {pm_side}\n"
            f"Action: sell manually via CLOB or wait for expiry"
        )
        event_write(
            bot="kalshi-pm-arb", event="rollback_failed",
            asset=asset, side=pm_side,
            size_usdc=exposure,
            profit=-exposure,
            note=f"NAKED {err}",
        )
        return

    # Accidental one-sided fill (non-rollback path)
    _send(
        f"⚠️ <b>[ONE-SIDED]</b> PM filled, Kalshi failed\n"
        f"{asset} {timeframe} | PM {pm_side} @ {pm_price:.1f}¢\n"
        f"Reason: {err}\n"
        f"Directional exposure: ~${exposure:.2f} at risk"
    )


def paper_window(window: dict):
    _send(
        f"📋 [ARB PAPER] Window found (not executed)\n"
        f"{window['asset']} {window['timeframe']} | {window['minutes_left']:.1f} min left\n"
        f"PM {window['pm_side']} @ {window['pm_price']:.1f}¢ + "
        f"Kalshi {window['kal_side']} @ {window['kal_price']:.1f}¢\n"
        f"Profit: {window['profit_cents']:.1f}¢"
    )

def directional_outcome(pos: dict, profit_usd: float, won: bool, already_redeemed: bool = False):
    """Alert when a directional PM position resolves."""
    asset     = pos.get("asset", "?")
    timeframe = pos.get("timeframe", "15m")
    pm_side   = pos.get("pm_side", "?")
    entry_usd = pos.get("usd", 0)
    contracts = pos.get("contracts", 0)
    intentional = pos.get("intentional", False)
    tag = "DIRECTIONAL" if intentional else "ONE-SIDED"

    if won:
        note = " (redeemed)" if already_redeemed else ""
        _send(
            f"\U0001f3af\u2705 <b>[{tag} WIN]</b>{note}\n"
            f"{asset} {timeframe} | {pm_side} | {contracts} shares\n"
            f"Cost: ${entry_usd:.2f} | Profit: <b>+${profit_usd:.2f}</b>"
        )
    else:
        _send(
            f"\U0001f3af\u274c <b>[{tag} LOSS]</b>\n"
            f"{asset} {timeframe} | {pm_side} | {contracts} shares\n"
            f"Cost: ${entry_usd:.2f} | Loss: <b>-${abs(profit_usd):.2f}</b>"
        )


def alive_heartbeat(cycle: int):
    import datetime
    t = datetime.datetime.utcnow().strftime("%H:%M UTC")
    _send(f"\u2705 <b>cross-candle-arb alive</b> (cycle {cycle} | {t})")


def pm_buy_outcome(result: dict, window: dict):
    """Alert on EVERY PM buy — success, one-sided, or failed rollback."""
    pm_price  = result.get("pm_price", 0)
    contracts = result.get("contracts", 0)
    asset     = window.get("asset", "?")
    tf        = window.get("timeframe", "15m")
    pm_side   = window.get("pm_side", "?")
    cost      = pm_price / 100 * contracts
    err       = result.get("error", "")

    if result.get("success"):
        return  # both_filled() handles this case

    if result.get("kal_filled") is False and result.get("pm_filled"):
        # PM bought, Kalshi didn't fill (or rollback failed)
        rolled = "rolled back" in err.lower() and "PM STILL OPEN" not in err
        if rolled:
            icon = "↩️"
            tag  = "PM ROLLED BACK"
        else:
            icon = "🚨"
            tag  = "NAKED PM POSITION"
        _send(
            f"{icon} <b>[{tag}]</b>\n"
            f"{asset} {tf} | {pm_side} @ {pm_price:.1f}¢ | {contracts} shares | ${cost:.2f}\n"
            f"Reason: {err[:120]}"
        )


def daily_summary(pm_bal: float, kal_bal: float, total: float,
                  fills_today: int, profit_today: float):
    _send(
        f"📊 <b>Daily P&L Summary</b>\n"
        f"PM: ${pm_bal:.2f}  Kalshi: ${kal_bal:.2f}  Total: <b>${total:.2f}</b>\n"
        f"Fills today: {fills_today}  Locked profit: ${profit_today:.2f}"
    )
