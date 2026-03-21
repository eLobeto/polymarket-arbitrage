#!/usr/bin/env python3
"""
Bond Scanner — High-Probability PM Market Hunter

Scans Polymarket for markets priced 80–94¢ where our LLM estimates true
probability is materially higher than the market price. Structural edge
comes from tail-risk premium and LP spread compression near resolution.

Strategy: buy YES in underpriced near-certain markets, hold to resolution.
No stop-loss (unlike news trades) — exit only on severe information shock.

Signal flow: bond_scanner → bond_signals.jsonl → executor (signal_source="bond")
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from daemon import daemonize as _daemonize

BASE_DIR     = Path(__file__).parent.parent
LOG_DIR      = BASE_DIR / "logs"
CONFIG_PATH  = BASE_DIR / "config" / "executor_config.yaml"
SIGNALS_OUT  = LOG_DIR / "bond_signals.jsonl"

LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [bond_scanner] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    handlers=[
        logging.FileHandler(LOG_DIR / "bond_scanner.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("bond_scanner")

GAMMA_BASE      = "https://gamma-api.polymarket.com"
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL    = "gemini-flash-latest"
GEMINI_URL      = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
GEMINI_DELAY_S  = 6       # respect rate limit between calls
GEMINI_RETRY_S  = 60      # sleep on 429


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    import yaml
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    return cfg.get("bond", {})


# ── Gamma API ─────────────────────────────────────────────────────────────────

def fetch_candidate_markets(price_min: float, price_max: float,
                             min_volume: float, max_days: float,
                             max_pages: int = 40) -> list[dict]:
    """
    Fetch PM markets where YES is in [price_min, price_max] with sufficient
    volume and within the resolution window.
    """
    candidates = []
    offset = 0
    limit  = 100
    pages  = 0

    while pages < max_pages:
        pages += 1
        try:
            r = requests.get(
                f"{GAMMA_BASE}/markets",
                params={
                    "active":  "true",
                    "closed":  "false",
                    "limit":   limit,
                    "offset":  offset,
                },
                timeout=15,
            )
            if not r.ok:
                log.warning(f"Gamma fetch failed: {r.status_code}")
                break

            batch = r.json()
            if not batch:
                break

            for m in batch:
                # Price filter — YES price in target range
                yes_str = m.get("outcomePrices", "[]")
                try:
                    prices = json.loads(yes_str) if isinstance(yes_str, str) else yes_str
                    yes_p = float(prices[0]) if prices else None
                except Exception:
                    yes_p = None

                if yes_p is None or not (price_min <= yes_p <= price_max):
                    continue

                # Volume filter
                vol = float(m.get("volume", 0) or 0)
                if vol < min_volume:
                    continue

                # TTL filter
                end_str = m.get("endDate") or m.get("endDateIso") or ""
                if not end_str:
                    continue
                try:
                    end_dt  = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    days_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400
                except Exception:
                    continue

                min_days = 1.0
                if not (min_days <= days_left <= max_days):
                    continue

                # Extract YES token ID
                yes_token_id = None
                try:
                    ids = m.get("clobTokenIds")
                    if isinstance(ids, str):
                        ids = json.loads(ids)
                    if isinstance(ids, list) and len(ids) >= 1:
                        yes_token_id = str(ids[0])
                except Exception:
                    pass

                if not yes_token_id:
                    continue

                candidates.append({
                    "market_id":    m.get("conditionId") or m.get("id", ""),
                    "market_title": m.get("question") or m.get("title", ""),
                    "event_id":     m.get("slug", ""),
                    "yes_price":    round(yes_p, 4),
                    "volume_usd":   round(vol, 2),
                    "days_to_close": round(days_left, 1),
                    "end_date":     end_str[:10],
                    "token_id":     yes_token_id,
                    "category":     (m.get("category") or "").lower(),
                })

            if len(batch) < limit:
                break
            offset += limit
            time.sleep(0.5)

        except Exception as e:
            log.error(f"Gamma fetch error: {e}")
            break

    return candidates


# ── LLM Assessment ────────────────────────────────────────────────────────────

BOND_PROMPT = """Analyze if this market resolves YES.
Market: {title}
Price: {price:.0%}
Closes: {end_date} ({days_left:.1f}d)
Today: {today}

Estimate TRUE probability of YES.
JSON: {{"prob": 0.XX, "conf": "high|medium|low"}}"""

def assess_with_llm(market: dict) -> tuple[float | None, str]:
    if not GEMINI_API_KEY:
        return None, "no_api_key"

    prompt = BOND_PROMPT.format(
        title    = market["market_title"],
        price    = market["yes_price"],
        end_date = market["end_date"],
        days_left = market["days_to_close"],
        today    = datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 100,
        }
    }

    try:
        r = requests.post(GEMINI_URL, params={"key": GEMINI_API_KEY}, json=payload, timeout=20)
        if not r.ok: return None, f"error_{r.status_code}"

        text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        # Simple extraction to avoid JSON parse errors on tiny outputs
        prob_match = re.search(r'"prob":\s*(0\.\d+)', text)
        conf_match = re.search(r'"conf":\s*"(\w+)"', text)

        if not prob_match:
            log.debug(f"LLM no prob in response: {text!r}")
            return None, "no_prob_in_response"

        prob = float(prob_match.group(1))
        conf = conf_match.group(1) if conf_match else "low"

        log.info(f"LLM assess: prob={prob:.0%} conf={conf} raw={text!r}")

        if conf == "low": return None, "low_confidence"
        return prob, conf  # return conf string for rationale

    except Exception as e:
        return None, str(e)


# ── Signal Writer ─────────────────────────────────────────────────────────────

def load_seen_market_ids() -> set:
    seen = set()
    if not SIGNALS_OUT.exists(): return seen
    cutoff = time.time() - (8 * 3600)
    with open(SIGNALS_OUT) as f:
        for line in f:
            try:
                sig = json.loads(line)
                if sig.get("ts_unix", 0) > cutoff:
                    seen.add(sig["market_id"])
            except Exception: pass
    return seen


def write_signal(market: dict, llm_prob: float, llm_conf: str = ""):
    edge_pct = round((llm_prob - market["yes_price"]) * 100, 1)
    signal = {
        "ts":            datetime.now(timezone.utc).isoformat(),
        "ts_unix":       int(time.time()),
        "signal_source": "bond",
        "market_id":     market["market_id"],
        "market_title":  market["market_title"],
        "event_id":      market["event_id"],
        "direction":     "YES",
        "pm_price":      market["yes_price"],
        "llm_confidence": llm_prob,
        "llm_conf_label": llm_conf,
        "llm_rationale": f"Gemini: prob={llm_prob:.0%} conf={llm_conf} | PM={market['yes_price']:.0%} edge=+{edge_pct:.1f}¢",
        "edge_pct":      edge_pct,
        "days_to_close": market["days_to_close"],
        "volume_usd":    market["volume_usd"],
        "category":      market["category"],
        "token_id":      market["token_id"],
    }
    with open(SIGNALS_OUT, "a") as f:
        f.write(json.dumps(signal) + "\n")

    log.info(
        f"💰 BOND SIGNAL | {market['market_title'][:55]} | "
        f"PM={market['yes_price']:.0%} → LLM={llm_prob:.0%} (+{edge_pct:.1f}¢ edge)"
    )


# ── Main scan loop ────────────────────────────────────────────────────────────

def run():
    log.info("Bond scanner started")

    while True:
        cfg = load_config()
        p_min = cfg.get("price_min", 0.80)
        p_max = cfg.get("price_max", 0.94)
        v_min = cfg.get("min_volume_usd", 5000)
        d_max = cfg.get("max_days_to_close", 90)
        e_min = cfg.get("min_edge_pct", 5.0) / 100
        l_min = cfg.get("llm_confidence_floor", 0.85)
        poll  = cfg.get("poll_interval_hours", 4)

        try:
            log.info(f"Scanning for bonds (YES {p_min:.0%}-{p_max:.0%}, vol>${v_min:,.0f}, ≤{d_max}d)...")
            candidates = fetch_candidate_markets(p_min, p_max, v_min, d_max)
            seen = load_seen_market_ids()
            candidates = [c for c in candidates if c["market_id"] not in seen]
            log.info(f"  Found {len(candidates)} new candidates to assess")

            signals_fired = 0
            for mkt in candidates:
                time.sleep(GEMINI_DELAY_S)
                llm_prob, llm_conf = assess_with_llm(mkt)

                if llm_prob is None:
                    log.debug(f"  skip {mkt['market_title'][:40]}: {llm_conf}")
                    continue

                edge = llm_prob - mkt["yes_price"]
                if llm_prob < l_min:
                    log.info(f"  skip prob={llm_prob:.0%} < floor={l_min:.0%}: {mkt['market_title'][:40]}")
                    continue

                if edge < e_min:
                    log.info(f"  skip edge={edge*100:.1f}¢ < min={e_min*100:.1f}¢: {mkt['market_title'][:40]}")
                    continue

                write_signal(mkt, llm_prob, llm_conf)
                signals_fired += 1

            log.info(f"Scan complete — {signals_fired} bond signals fired. Next in {poll}h.")

        except Exception as e:
            log.error(f"Bond scanner error: {e}", exc_info=True)

        time.sleep(poll * 3600)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true")
    args = parser.parse_args()
    if args.daemon:
        _daemonize(LOG_DIR / "bond_scanner.pid", LOG_DIR / "bond_scanner.log")
    run()
