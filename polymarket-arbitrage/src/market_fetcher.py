"""
market_fetcher.py — Fetch Bitcoin 15m markets from Polymarket Gamma API.
"""

import aiohttp
import asyncio
import logging
import json
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger("market_fetcher")


@dataclass
class Market:
    """Represents a Polymarket market."""
    market_id: str
    title: str
    description: str
    yes_price: float  # Current YES price
    no_price: float   # Current NO price
    timestamp: datetime
    liquidity: float  # Total liquidity in USDC
    volume_24h: float  # Volume last 24h
    condition_id: str  # For price fetching
    slug: str  # Market slug


class PolymarketFetcher:
    """Fetch and monitor Polymarket Bitcoin 15m markets using slug-based discovery."""
    
    def __init__(self, clob_url: str = "https://gamma-api.polymarket.com", market_slugs: List[str] = None):
        """
        Initialize fetcher.
        
        Args:
            clob_url: Polymarket Gamma API base URL
            market_slugs: List of market slugs to monitor (e.g., ["btc-updown-5m-1234567890"])
        """
        self.clob_url = clob_url
        self.market_slugs = market_slugs or []
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def __aenter__(self):
        """Async context manager entry."""
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self.session:
            await self.session.close()
    
    async def fetch_markets(self, keyword: str = "Bitcoin") -> List[Market]:
        """
        Fetch all configured market slugs and return active markets.
        
        Args:
            keyword: Filter markets by title keyword (not used here, kept for compatibility)
        
        Returns:
            List of Market objects with current prices
        """
        if not self.session:
            raise RuntimeError("Fetcher not initialized. Use async context manager.")
        
        if not self.market_slugs:
            log.warning("No market slugs configured. Add slugs to config.yaml")
            return []
        
        markets = []
        
        for slug in self.market_slugs:
            try:
                market = await self._fetch_market_by_slug(slug)
                if market:
                    markets.append(market)
            except Exception as e:
                log.warning(f"Error fetching {slug}: {e}")
        
        log.info(f"Found {len(markets)} active Bitcoin markets")
        return markets
    
    async def _fetch_market_by_slug(self, slug: str) -> Optional[Market]:
        """
        Fetch a single market by slug.
        
        Args:
            slug: Market slug (e.g., "btc-updown-5m-1234567890")
        
        Returns:
            Market object or None if inactive/closed
        """
        if not self.session:
            return None
        
        try:
            url = f"{self.clob_url}/markets/slug/{slug}"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    log.debug(f"Failed to fetch {slug}: {resp.status}")
                    return None
                
                data = await resp.json()
                
                # Check if market is active and not closed
                if not data.get("active") or data.get("closed"):
                    log.debug(f"{slug}: Market not active or closed")
                    return None
                
                # Parse prices (they come as JSON string)
                outcome_prices_raw = data.get("outcomePrices", "[]")
                if isinstance(outcome_prices_raw, str):
                    try:
                        outcome_prices = json.loads(outcome_prices_raw)
                    except:
                        log.warning(f"{slug}: Could not parse outcomePrices")
                        return None
                else:
                    outcome_prices = outcome_prices_raw
                
                if not outcome_prices or len(outcome_prices) < 2:
                    log.warning(f"{slug}: Invalid outcome prices")
                    return None
                
                yes_price = float(outcome_prices[0])
                no_price = float(outcome_prices[1])
                
                return Market(
                    market_id=data.get("id", ""),
                    title=data.get("question", ""),
                    description=data.get("description", ""),
                    yes_price=yes_price,
                    no_price=no_price,
                    timestamp=datetime.now(),
                    liquidity=float(data.get("liquidity", 0)),
                    volume_24h=float(data.get("volume24hr", 0)),
                    condition_id=data.get("conditionId", ""),
                    slug=slug,
                )
        
        except asyncio.TimeoutError:
            log.debug(f"{slug}: Timeout")
            return None
        except Exception as e:
            log.error(f"Error fetching {slug}: {e}")
            return None


# Example usage (for testing)
if __name__ == "__main__":
    import asyncio
    
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    
    async def main():
        async with PolymarketFetcher(
            market_slugs=["btc-updown-5m-1772241000"]
        ) as fetcher:
            markets = await fetcher.fetch_markets()
            for market in markets:
                pair_cost = market.yes_price + market.no_price
                profit = 1.0 - pair_cost
                print(f"Market: {market.title}")
                print(f"  YES: ${market.yes_price:.4f} | NO: ${market.no_price:.4f}")
                print(f"  Pair Cost: ${pair_cost:.4f} | Profit: ${profit:.4f}")
                print()
    
    asyncio.run(main())
