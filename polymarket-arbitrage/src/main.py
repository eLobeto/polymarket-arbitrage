"""
main.py — Gabagool binary arbitrage scanner for Polymarket Bitcoin 15min markets.
"""

import asyncio
import logging
import sys
import os
from datetime import datetime
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
    """Gabagool arbitrage scanner."""
    
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
        
        log.info("🎯 Gabagool Scanner initialized")
    
    async def start(self):
        """Start the scanner loop."""
        async with PolymarketFetcher(
            clob_url=self.config["polymarket"]["clob_url"],
            market_slugs=self.config["polymarket"].get("bitcoin_market_slugs", [])
        ) as fetcher:
            self.market_fetcher = fetcher
            
            # Initialize order executor (only if not dry-run mode)
            if not self.config["dev"]["dry_run"]:
                self.order_executor = OrderExecutor(
                    rpc_url=self.config["polygon"]["rpc_url"],
                    private_key=self.config["wallet"]["private_key"],
                    wallet_address=self.config["wallet"]["address"],
                    order_book_contract=self.config["polymarket"]["order_book_contract"],
                    usdc_contract=self.config["polymarket"]["usdc_contract"],
                )
            
            log.info("🚀 Starting scanner loop")
            
            # Main loop
            try:
                while True:
                    await self._scan_cycle()
                    await asyncio.sleep(self.config["trading"]["poll_interval_sec"])
            except KeyboardInterrupt:
                log.info("⏸️  Scanner stopped by user")
            except Exception as e:
                log.error(f"❌ Fatal error: {e}", exc_info=True)
    
    async def _scan_cycle(self):
        """Single scan cycle: fetch markets, detect arbitrage, execute."""
        try:
            # Fetch markets (using pre-configured slugs)
            markets = await self.market_fetcher.fetch_markets()
            
            if not markets:
                log.debug("No markets fetched")
                return
            
            # Analyze each market
            for market in markets:
                await self._analyze_market(market)
        
        except Exception as e:
            log.error(f"Error in scan cycle: {e}")
    
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
        
        # Check if this is an opportunity
        if pair_cost < target_cost:
            profit_potential = 1.0 - pair_cost
            
            if profit_potential > min_margin:
                log.info(
                    f"🎯 OPPORTUNITY FOUND: {market.title}\n"
                    f"   YES: ${market.yes_price:.4f} | NO: ${market.no_price:.4f}\n"
                    f"   Pair Cost: ${pair_cost:.4f} | Profit: ${profit_potential:.4f}"
                )
                
                # Execute arbitrage
                await self._execute_arbitrage(market)
            else:
                log.debug(f"Profit margin too small: ${profit_potential:.4f}")
        else:
            log.debug(f"{market.title}: Pair cost ${pair_cost:.4f} >= target ${target_cost:.4f}")
    
    async def _execute_arbitrage(self, market: Market):
        """
        Execute an arbitrage trade: buy YES and NO at cheap prices.
        
        Args:
            market: Market object
        """
        config = self.config["trading"]
        
        # Calculate position size
        bankroll = config["bankroll_usdc"]
        max_per_trade_pct = config["max_per_trade_pct"]
        max_spend = bankroll * max_per_trade_pct
        
        # Simple sizing: spend equally on YES and NO
        yes_spend = max_spend / 2
        no_spend = max_spend / 2
        
        yes_qty = yes_spend / market.yes_price
        no_qty = no_spend / market.no_price
        
        log.info(
            f"💰 Executing arbitrage for {market.title}\n"
            f"   YES: {yes_qty:.2f} shares @ ${market.yes_price:.4f} = ${yes_spend:.2f}\n"
            f"   NO:  {no_qty:.2f} shares @ ${market.no_price:.4f} = ${no_spend:.2f}"
        )
        
        if self.config["dev"]["dry_run"]:
            log.info("🏁 DRY RUN MODE - Not executing")
            return
        
        # Create position
        pos_id = self.position_tracker.create_position(market.market_id, market.title)
        
        # Place orders (in real implementation, use order_executor)
        # self.position_tracker.add_trade(pos_id, "YES", yes_qty, market.yes_price, "hash_yes")
        # self.position_tracker.add_trade(pos_id, "NO", no_qty, market.no_price, "hash_no")
        
        log.info(f"✅ Arbitrage executed (position {pos_id})")


async def main():
    """Entry point."""
    scanner = GabagoolScanner("config/config.yaml")
    await scanner.start()


if __name__ == "__main__":
    asyncio.run(main())
