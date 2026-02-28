"""
market_fetcher.py — Fetch Bitcoin 15m markets from Polymarket Gamma API events endpoint.
"""

import aiohttp
import asyncio
import logging
import json
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
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
    end_time: Optional[datetime] = None  # When market expires


class PolymarketFetcher:
    """Fetch and monitor Polymarket Bitcoin 15m markets using events endpoint."""
    
    def __init__(self, clob_url: str = "https://gamma-api.polymarket.com"):
        """
        Initialize fetcher.
        
        Args:
            clob_url: Polymarket Gamma API base URL
        """
        self.clob_url = clob_url
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
        Fetch all active Bitcoin UP OR DOWN markets from events endpoint.
        
        Args:
            keyword: Filter events by keyword (e.g., "Bitcoin", "BTC")
        
        Returns:
            List of Market objects with current prices
        """
        if not self.session:
            raise RuntimeError("Fetcher not initialized. Use async context manager.")
        
        try:
            url = f"{self.clob_url}/events"
            params = {
                'limit': 100,
                'order': 'startDate',
                'ascending': 'false'
            }
            log.debug(f"Fetching from: {url} with params: {params}")
            
            async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    log.error(f"Failed to fetch events: {resp.status}")
                    return []
                
                events = await resp.json()
                log.debug(f"Fetched {len(events)} events from Polymarket")
                
                # Filter for Bitcoin/crypto UP OR DOWN events
                bitcoin_events = [
                    e for e in events
                    if keyword.lower() in e.get("title", "").lower()
                    and "up or down" in e.get("title", "").lower()
                    and e.get("active") and not e.get("closed")
                ]
                
                log.info(f"Found {len(bitcoin_events)} active Bitcoin UP OR DOWN events")
                
                # Extract markets from events
                markets = []
                for event in bitcoin_events:
                    for market in event.get("markets", []):
                        # Only process active markets
                        if market.get("active") and not market.get("closed"):
                            market_obj = self._parse_market(market)
                            if market_obj:
                                markets.append(market_obj)
                
                log.info(f"Found {len(markets)} active Bitcoin markets across events")
                return markets
        
        except asyncio.TimeoutError:
            log.error("Timeout fetching events")
            return []
        except Exception as e:
            log.error(f"Error fetching events: {e}")
            return []
    
    def _parse_market(self, raw_market: Dict[str, Any]) -> Optional[Market]:
        """Parse raw market data from events into Market object."""
        try:
            # Parse prices (they come as JSON string)
            outcome_prices_raw = raw_market.get("outcomePrices", "[]")
            if isinstance(outcome_prices_raw, str):
                try:
                    outcome_prices = json.loads(outcome_prices_raw)
                except:
                    log.warning(f"{raw_market.get('slug')}: Could not parse outcomePrices")
                    return None
            else:
                outcome_prices = outcome_prices_raw
            
            if not outcome_prices or len(outcome_prices) < 2:
                log.warning(f"{raw_market.get('slug')}: Invalid outcome prices")
                return None
            
            yes_price = float(outcome_prices[0])
            no_price = float(outcome_prices[1])
            
            # Parse end time if available
            end_time = None
            end_time_str = raw_market.get("endDate")
            if end_time_str:
                try:
                    end_time = datetime.fromisoformat(end_time_str.replace("Z", "+00:00"))
                except Exception as e:
                    log.debug(f"Could not parse end time: {e}")
            
            return Market(
                market_id=raw_market.get("id", ""),
                title=raw_market.get("question", ""),
                description=raw_market.get("description", ""),
                yes_price=yes_price,
                no_price=no_price,
                timestamp=datetime.now(),
                liquidity=float(raw_market.get("liquidity", 0)),
                volume_24h=float(raw_market.get("volume24hr", 0)),
                condition_id=raw_market.get("conditionId", ""),
                slug=raw_market.get("slug", ""),
                end_time=end_time,
            )
        except Exception as e:
            log.warning(f"Error parsing market: {e}")
            return None


# Example usage (for testing)
if __name__ == "__main__":
    import asyncio
    
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    
    async def main():
        async with PolymarketFetcher() as fetcher:
            markets = await fetcher.fetch_markets("Bitcoin")
            
            print(f"\n✅ Found {len(markets)} markets\n")
            
            for market in markets[:10]:
                pair_cost = market.yes_price + market.no_price
                profit = 1.0 - pair_cost
                
                print(f"Q: {market.title[:70]}")
                print(f"  YES: ${market.yes_price:.4f} | NO: ${market.no_price:.4f}")
                print(f"  Pair Cost: ${pair_cost:.4f} | Profit: ${profit:.4f}")
                print()
    
    asyncio.run(main())
