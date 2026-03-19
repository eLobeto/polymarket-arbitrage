"""
fee_regime.py — Single source of truth for all fee assumptions.

Why this exists
---------------
Fee constants were previously scattered across trade_logger.py (0.07),
redeemer.py (0.07 / 0.93 hardcoded), and implicitly embedded in config.py
thresholds. A fee change required hunting 3+ files with no safety net.

Usage
-----
    from fee_regime import FeeRegime

    # Profit calc
    net = FeeRegime.net_profit_usd(gross_profit_usd, mode="taker")

    # Startup check — warns if config thresholds are stale after a fee change
    warnings = FeeRegime.validate(MIN_ARB_CENTS, MAX_PAIR_COST)
    for w in warnings:
        log.critical("[FEE REGIME] %s", w)

Fee model (as of Mar 2026)
--------------------------
  Kalshi:  0% maker / 7% taker on WINNING profit only
  PM CLOB: 0% taker (changed Mar 2026 — was non-zero before)
  Zerohash bridge: 0.4% on USDC → Kalshi transfers
"""

from __future__ import annotations


class FeeRegime:
    # ── Kalshi ────────────────────────────────────────────────────────────────
    KALSHI_TAKER_FEE_PCT   = 0.07   # 7% of gross profit on winning Kalshi contracts
    KALSHI_MAKER_FEE_PCT   = 0.00   # 0% maker fee (as of Mar 2026)

    # ── Polymarket ────────────────────────────────────────────────────────────
    PM_TAKER_FEE_PCT       = 0.00   # 0% CLOB taker fee (changed Mar 2026)
    PM_MAKER_FEE_PCT       = 0.00   # 0% CLOB maker fee

    # ── Rebalancing ───────────────────────────────────────────────────────────
    ZEROHASH_BRIDGE_FEE_PCT = 0.004  # 0.4% on USDC → Kalshi via Zerohash

    # ── Internal floor (used by validate) ─────────────────────────────────────
    # Minimum net edge we want to clear after fees. Raise if the strategy's
    # alpha assumption changes.
    _MIN_NET_EDGE_CENTS = 5.0

    # ── Core calculations ─────────────────────────────────────────────────────

    @classmethod
    def kalshi_fee_usd(cls, gross_profit_usd: float, mode: str = "taker") -> float:
        """
        Kalshi settlement fee on a winning trade.
        Fee only applies when Kalshi side wins (taker mode = 7%, maker mode = 0%).
        """
        if mode == "maker":
            return 0.0
        return round(gross_profit_usd * cls.KALSHI_TAKER_FEE_PCT, 4)

    @classmethod
    def net_profit_usd(cls, gross_profit_usd: float, mode: str = "taker") -> float:
        """Net profit after Kalshi settlement fee on a Kalshi-win outcome."""
        return round(gross_profit_usd - cls.kalshi_fee_usd(gross_profit_usd, mode), 4)

    @classmethod
    def net_multiplier(cls, mode: str = "taker") -> float:
        """
        Convenience multiplier: net_profit = gross * net_multiplier(mode).
        e.g. net_multiplier("taker") = 0.93
        """
        return 1.0 - (cls.KALSHI_TAKER_FEE_PCT if mode == "taker" else cls.KALSHI_MAKER_FEE_PCT)

    @classmethod
    def expected_net_edge_cents(
        cls,
        combined_cost_cents: float,
        kal_price_cents: float,
        mode: str = "maker",
    ) -> float:
        """
        Expected net edge per $1 payout, in cents, after fees.

        The taker fee only fires when Kalshi wins (~50% of a balanced arb).
        Fee per trade = TAKER_PCT × (100 − kal_price)¢  [applied on Kalshi-win leg only]
        Expected fee  = fee × P(Kalshi wins) ≈ fee × 0.5

        Args:
            combined_cost_cents: pm_price + kal_price in cents
            kal_price_cents:     Kalshi entry price in cents (determines fee magnitude)
            mode:                "maker" (0% fee) or "taker" (KALSHI_TAKER_FEE_PCT)

        Returns:
            Expected net edge in cents (negative = expected loss).
        """
        gross = 100.0 - combined_cost_cents
        if gross <= 0:
            return round(gross, 4)

        fee_pct = cls.KALSHI_TAKER_FEE_PCT if mode == "taker" else 0.0
        # Fee = fee_pct × (100 − kal_price)¢, expected half the time
        expected_fee = fee_pct * (100.0 - kal_price_cents) * 0.5
        return round(gross - expected_fee, 4)

    @classmethod
    def max_combined_for_edge(
        cls,
        min_net_edge_cents: float,
        kal_price_cents: float,
        mode: str = "maker",
    ) -> float:
        """
        Back-solve: maximum combined cost that still yields ≥ min_net_edge after fees.

        Inverse of expected_net_edge_cents():
            net_edge = (100 − combined) − expected_fee ≥ min_net_edge
            combined ≤ 100 − min_net_edge − expected_fee
        """
        fee_pct = cls.KALSHI_TAKER_FEE_PCT if mode == "taker" else 0.0
        expected_fee = fee_pct * (100.0 - kal_price_cents) * 0.5
        return round(100.0 - min_net_edge_cents - expected_fee, 4)

    # ── Startup validation ─────────────────────────────────────────────────────

    @classmethod
    def validate(
        cls,
        min_arb_cents: float,
        max_pair_cost: float,
        mode: str = "taker",
    ) -> list[str]:
        """
        Check that config thresholds are still consistent with current fee assumptions.
        Call at startup. Log any returned warnings as CRITICAL and consider pausing.

        Args:
            min_arb_cents:  config.MIN_ARB_CENTS — minimum gross profit to enter
            max_pair_cost:  config.MAX_PAIR_COST  — max combined cost to consider
            mode:           "taker" for worst-case check (default)

        Returns:
            List of warning strings. Empty list = regime healthy.
        """
        warnings: list[str] = []

        # 1. MIN_ARB_CENTS should comfortably clear the expected fee burden.
        #    Worst-case: kal_price ≈ 50¢ (balanced arb near-strike)
        #    Expected fee ≈ TAKER_FEE_PCT × 50¢ × 0.5 = 1.75¢
        worst_case_fee = cls.KALSHI_TAKER_FEE_PCT * 50.0 * 0.5
        if min_arb_cents <= worst_case_fee and mode == "taker":
            warnings.append(
                f"MIN_ARB_CENTS={min_arb_cents}¢ ≤ worst-case expected fee ({worst_case_fee:.2f}¢). "
                f"Net edge in taker mode may be ≤0 for balanced arbs."
            )

        # 2. MAX_PAIR_COST: net edge at max allowed combined cost
        net_at_max = cls.expected_net_edge_cents(
            max_pair_cost, kal_price_cents=max_pair_cost / 2, mode=mode
        )
        if net_at_max <= 0:
            warnings.append(
                f"MAX_PAIR_COST={max_pair_cost}¢ allows zero/negative net edge in {mode} mode "
                f"(net={net_at_max:.2f}¢ when kal_price≈{max_pair_cost/2:.0f}¢). "
                f"Consider lowering MAX_PAIR_COST."
            )
        elif net_at_max < cls._MIN_NET_EDGE_CENTS:
            warnings.append(
                f"MAX_PAIR_COST={max_pair_cost}¢ allows thin net edge "
                f"({net_at_max:.2f}¢ < desired {cls._MIN_NET_EDGE_CENTS}¢) in {mode} mode."
            )

        # 3. Sanity: maker fee should never exceed taker fee
        if cls.KALSHI_MAKER_FEE_PCT > cls.KALSHI_TAKER_FEE_PCT:
            warnings.append(
                f"FeeRegime inconsistency: MAKER_FEE ({cls.KALSHI_MAKER_FEE_PCT}) > "
                f"TAKER_FEE ({cls.KALSHI_TAKER_FEE_PCT}). Verify Kalshi fee schedule."
            )

        # 4. PM fee check — if PM introduces taker fees, edge shrinks
        if cls.PM_TAKER_FEE_PCT > 0:
            warnings.append(
                f"PM_TAKER_FEE_PCT={cls.PM_TAKER_FEE_PCT} is non-zero. "
                f"Edge calculations do not yet account for PM fees — update expected_net_edge_cents()."
            )

        return warnings

    @classmethod
    def summary(cls) -> str:
        """Human-readable fee regime summary for log on startup."""
        return (
            f"FeeRegime | "
            f"Kalshi: maker={cls.KALSHI_MAKER_FEE_PCT*100:.0f}% "
            f"taker={cls.KALSHI_TAKER_FEE_PCT*100:.0f}% | "
            f"PM: taker={cls.PM_TAKER_FEE_PCT*100:.0f}% | "
            f"Zerohash bridge: {cls.ZEROHASH_BRIDGE_FEE_PCT*100:.1f}%"
        )
