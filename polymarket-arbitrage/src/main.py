"""
main.py — Gabagool binary arbitrage scanner for Polymarket Bitcoin 15min markets.

Core fixes:
- Proper async/await throughout
- Market expiry checks (skip old markets)
- Balanced position sizing (minimize waste)
- Error recovery with backoff
- Real order execution via OrderExecutor
"""

import asyncio
import logging
import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Load .env variables FIRST
from dotenv import load_dotenv
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from config import load_config, validate_config
from market_fetcher import PolymarketFetcher, Market
from position_tracker import PositionTracker
from order_executor import OrderExecutor


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("logs/polymarket.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("main")


class GabagoolScanner:
    """Gabagool arbitrage scanner with error recovery."""
    
    def __init__(self, config_path: str = None):
        """
        Initialize scanner.
        
        Args:
            config_path: Path to config.yaml
        """
        self.config = load_config(config_path)
        validate_config(self.config)
        
        self.position_tracker = PositionTracker(self.config["database"]["path"])
        self.market_fetcher = None
        self.order_executor = None
        
        # Error tracking
        self.consecutive_errors = 0
        self.max_consecutive_errors = 5
        self.last_error_time = None
        self.backoff_multiplier = 1.0
        
        # Market refresh timing
        self.last_market_discovery = None
        self.market_discovery_interval = 120  # 2 minutes
        self.price_refresh_interval = 10  # 10 seconds
        self.cached_markets = []
        
        # Stats
        self.cycle_count = 0
        self.opportunity_count = 0
        self.trade_count = 0
        
        log.info("🎯 Gabagool Scanner initialized")
        log.info(f"   Dry-run: {self.config['dev']['dry_run']}")
        log.info(f"   Bankroll: ${self.config['trading']['bankroll_usdc']}")
        log.info(f"   Target pair cost: ${self.config['trading']['target_combined_cost']}")
    
    async def start(self):
        """Start the scanner loop."""
        async with PolymarketFetcher(
            clob_url=self.config["polymarket"]["clob_url"]
        ) as fetcher:
            self.market_fetcher = fetcher
            
            # Initialize order executor (only if not dry-run mode)
            if not self.config["dev"]["dry_run"]:
                try:
                    self.order_executor = OrderExecutor(
                        private_key=self.config["wallet"]["private_key"],
                        wallet_address=self.config["wallet"]["address"],
                        clob_api_url=self.config["polymarket"]["clob_url"],
                    )
                    log.info("🚀 Live trading enabled with official Polymarket SDK")
                except Exception as e:
                    log.error(f"Failed to initialize order executor: {e}")
                    log.info("Falling back to dry-run mode")
                    self.config["dev"]["dry_run"] = True
            else:
                log.info("🏁 DRY-RUN MODE: Orders will not be executed")
            
            await self._main_loop()
    
    async def _main_loop(self):
        """Main scanner loop with error recovery."""
        log.info("🚀 Starting main scanner loop")
        
        try:
            while True:
                try:
                    await self._scan_cycle()
                    
                    # Reset error counter on successful cycle
                    if self.consecutive_errors > 0:
                        log.info(f"✅ Recovered from error state")
                        self.consecutive_errors = 0
                        self.backoff_multiplier = 1.0
                    
                    await asyncio.sleep(self.config["trading"]["poll_interval_sec"])
                
                except Exception as e:
                    await self._handle_cycle_error(e)
                    
                    # Backoff with exponential increase
                    backoff_time = (
                        self.config["trading"]["poll_interval_sec"] * 
                        (2 ** self.consecutive_errors)
                    )
                    
                    if self.consecutive_errors >= self.max_consecutive_errors:
                        log.critical(
                            f"❌ FATAL: {self.consecutive_errors} consecutive errors. "
                            f"Stopping scanner."
                        )
                        break
                    
                    log.warning(f"⏳ Backoff: waiting {backoff_time}s before retry")
                    await asyncio.sleep(backoff_time)
        
        except KeyboardInterrupt:
            log.info("⏸️  Scanner stopped by user")
        except Exception as e:
            log.error(f"❌ Fatal error in main loop: {e}", exc_info=True)
    
    async def _handle_cycle_error(self, e: Exception):
        """Handle error in scan cycle with logging."""
        self.consecutive_errors += 1
        self.last_error_time = datetime.now()
        
        log.error(
            f"❌ Scan cycle error ({self.consecutive_errors}/{self.max_consecutive_errors}): {e}",
            exc_info=True
        )
    
    async def _scan_cycle(self):
        """Single scan cycle: efficient market discovery + price refresh."""
        self.cycle_count += 1
        
        # Decide: full discovery or price refresh?
        now = datetime.now()
        needs_discovery = (
            self.last_market_discovery is None or
            (now - self.last_market_discovery).total_seconds() >= self.market_discovery_interval
        )
        
        if needs_discovery:
            # Full market discovery (every 2 mins)
            market_filter = self.config["polymarket"]["market_filter"]
            assets = market_filter.get("assets", ["Bitcoin"])
            
            markets = await self.market_fetcher.fetch_market_list(assets=assets)
            self.cached_markets = markets
            self.last_market_discovery = now
            
            if not markets:
                log.debug("No markets discovered this cycle")
                return
            
            log.info(f"Cycle {self.cycle_count}: Market discovery found {len(markets)} markets for {assets}")
        else:
            # Price refresh on cached markets (every 10 secs)
            markets = await self.market_fetcher.refresh_prices()
            
            if not markets:
                log.debug("No markets in cache, will rediscover at next interval")
                return
            
            log.debug(f"Cycle {self.cycle_count}: Price refresh on {len(markets)} cached markets")
        
        # Filter out expired markets
        active_markets = [m for m in markets if not self._is_market_expired(m)]
        
        if len(active_markets) < len(markets):
            expired_count = len(markets) - len(active_markets)
            log.debug(f"Filtered out {expired_count} expired markets")
        
        # Analyze each active market
        for market in active_markets:
            await self._analyze_market(market)
    
    def _is_market_expired(self, market: Market) -> bool:
        """Check if market has expired or is about to."""
        # If end_time is available, check it
        if hasattr(market, 'end_time') and market.end_time:
            # Use UTC now to avoid timezone comparison issues
            now_utc = datetime.now(timezone.utc)
            
            # Handle both naive and aware datetimes
            end_time = market.end_time
            if end_time.tzinfo is None:
                # Naive datetime - assume UTC
                end_time = end_time.replace(tzinfo=timezone.utc)
            
            # Skip if market ends in <2 minutes
            if end_time < now_utc + timedelta(minutes=2):
                log.debug(f"Skipping market ending soon: {market.slug}")
                return True
        
        return False
    
    async def _analyze_market(self, market: Market):
        """
        Analyze a single market for arbitrage opportunity.
        
        Args:
            market: Market object
        """
        # Calculate combined cost
        pair_cost = market.yes_price + market.no_price
        target_cost = self.config["trading"]["target_combined_cost"]
        min_margin = self.config["trading"]["min_profit_margin"]
        min_liquidity = self.config["polymarket"]["market_filter"]["min_liquidity_usdc"]
        
        # Check liquidity first
        if market.liquidity < min_liquidity:
            log.debug(
                f"Insufficient liquidity: {market.slug} "
                f"(${market.liquidity:.2f} < ${min_liquidity})"
            )
            return
        
        # Check if this is an opportunity
        if pair_cost < target_cost:
            profit_potential = 1.0 - pair_cost
            
            # Log all opportunities (for dry-run tracking)
            self.position_tracker.log_dry_run_opportunity(
                market_slug=market.slug,
                market_title=market.title,
                yes_price=market.yes_price,
                no_price=market.no_price
            )
            
            self.opportunity_count += 1
            
            if profit_potential > min_margin:
                log.info(
                    f"🎯 OPPORTUNITY #{self.opportunity_count}: {market.title}\n"
                    f"   YES: ${market.yes_price:.4f} | NO: ${market.no_price:.4f}\n"
                    f"   Pair Cost: ${pair_cost:.4f} | Profit: ${profit_potential:.4f}"
                )
                
                # Execute arbitrage
                await self._execute_arbitrage(market)
            else:
                log.debug(f"Margin too small: ${profit_potential:.4f} < ${min_margin}")
        else:
            log.debug(f"{market.slug}: ${pair_cost:.4f} >= ${target_cost}")
    
    async def _execute_arbitrage(self, market: Market):
        """
        Execute an arbitrage trade: buy YES and NO at cheap prices.
        
        Args:
            market: Market object
        """
        try:
            config = self.config["trading"]
            
            # Get actual USDC balance from wallet
            if not self.order_executor:
                log.error("Order executor not initialized")
                return
            
            wallet_balance = self.order_executor.get_balance("USDC")
            max_trade_spend = wallet_balance * config["max_wallet_utilization"]
            
            if max_trade_spend <= 0:
                log.error(f"Insufficient wallet balance: ${wallet_balance:.2f}")
                return
            
            log.debug(f"Wallet balance: ${wallet_balance:.2f} → Max trade spend: ${max_trade_spend:.2f}")
            
            # Calculate balanced position size (using 75% of wallet balance)
            yes_qty, no_qty, yes_spend, no_spend = self._calculate_balanced_size(
                market.yes_price,
                market.no_price,
                max_trade_spend,
                config["qty_balance_tolerance"],
            )
            
            log.info(
                f"💰 Executing: {market.title}\n"
                f"   YES: {yes_qty:.2f} @ ${market.yes_price:.4f} = ${yes_spend:.2f}\n"
                f"   NO:  {no_qty:.2f} @ ${market.no_price:.4f} = ${no_spend:.2f}\n"
                f"   Total Cost: ${yes_spend + no_spend:.2f}"
            )
            
            if self.config["dev"]["dry_run"]:
                log.info("🏁 DRY RUN — Not executing real orders")
                self.trade_count += 1
                return
            
            # Create position in DB first
            pos_id = self.position_tracker.create_position(market.market_id, market.title)
            
            # Place YES order
            yes_hash = await self.order_executor.place_order(
                market_id=market.market_id,
                condition_id=market.condition_id,
                side="YES",
                qty=yes_qty,
                price=market.yes_price,
            )
            
            if not yes_hash:
                log.error(f"Failed to place YES order for position {pos_id}")
                return
            
            # Place NO order
            no_hash = await self.order_executor.place_order(
                market_id=market.market_id,
                condition_id=market.condition_id,
                side="NO",
                qty=no_qty,
                price=market.no_price,
            )
            
            if not no_hash:
                log.error(f"Failed to place NO order for position {pos_id}")
                return
            
            # Track trades in position
            self.position_tracker.add_trade(pos_id, "YES", yes_qty, market.yes_price, yes_hash)
            self.position_tracker.add_trade(pos_id, "NO", no_qty, market.no_price, no_hash)
            
            log.info(
                f"✅ Position {pos_id} executed!\n"
                f"   Guaranteed profit: ${min(yes_qty, no_qty) * (1.0 - (market.yes_price + market.no_price)):.2f}"
            )
            
            self.trade_count += 1
        
        except Exception as e:
            log.error(f"Error executing arbitrage: {e}", exc_info=True)
    
    def _calculate_balanced_size(
        self,
        yes_price: float,
        no_price: float,
        max_spend: float,
        tolerance_pct: float = 0.05,
    ) -> tuple:
        """
        Calculate balanced YES/NO quantities to minimize waste.
        
        Returns:
            (yes_qty, no_qty, yes_spend, no_spend)
        """
        # Binary search for balanced allocation
        for spend in range(int(max_spend * 100), 0, -1):
            spend = spend / 100.0
            
            yes_qty = spend / yes_price
            no_qty = spend / no_price
            
            # Check if balanced within tolerance
            if yes_qty > 0 and no_qty > 0:
                balance_ratio = min(yes_qty, no_qty) / max(yes_qty, no_qty)
                if balance_ratio >= (1.0 - tolerance_pct):
                    return (yes_qty, no_qty, spend, spend)
        
        # Fallback to equal spend if search fails
        return (
            max_spend / yes_price,
            max_spend / no_price,
            max_spend,
            max_spend,
        )


async def main():
    """Entry point."""
    scanner = GabagoolScanner("config/config.yaml")
    await scanner.start()


if __name__ == "__main__":
    asyncio.run(main())
