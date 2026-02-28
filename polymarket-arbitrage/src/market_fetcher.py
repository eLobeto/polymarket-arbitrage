"""
market_fetcher.py — Fetch crypto UP OR DOWN markets from Polymarket Gamma API events endpoint.

Supports multiple assets (Bitcoin, Solana, Ethereum) and timeframes (5m, 10m, 15m).
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
    """Fetch and monitor Polymarket crypto UP OR DOWN markets using events endpoint.
    
    Supports multiple assets (Bitcoin, Solana, Ethereum) and timeframes (5m, 10m, 15m).
    Uses efficient discovery + price refresh pattern:
    - Market discovery (expensive): every 2 minutes
    - Price refresh (cheap): every 10 seconds
    """
    
    def __init__(self, clob_url: str = "https://gamma-api.polymarket.com"):
        """
        Initialize fetcher.
        
        Args:
            clob_url: Polymarket Gamma API base URL
        """
        self.clob_url = clob_url
        self.session: Optional[aiohttp.ClientSession] = None
        self.cached_markets: List[Market] = []  # Cache for price refreshes
    
    async def __aenter__(self):
        """Async context manager entry."""
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self.session:
            await self.session.close()
    
    async def fetch_market_list(self, assets: List[str] = None) -> List[Market]:
        """
        Discover all active crypto UP OR DOWN markets (expensive, run every 2 mins).
        
        Args:
            assets: List of assets to filter (e.g., ["Bitcoin", "Solana", "Ethereum"])
                   If None, defaults to ["Bitcoin"]
        
        Returns:
            List of Market objects with current prices
        """
        if not self.session:
            raise RuntimeError("Fetcher not initialized. Use async context manager.")
        
        # Default to Bitcoin if not specified
        if assets is None:
            assets = ["Bitcoin"]
        
        assets_lower = [a.lower() for a in assets]
        
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
                
                # Filter for crypto UP OR DOWN events matching assets
                # Trades all timeframes (5m, 10m, 15m, etc.) for these assets
                filtered_events = []
                for e in events:
                    title = e.get("title", "").lower()
                    
                    # Must contain "up or down"
                    if "up or down" not in title:
                        continue
                    
                    # Must match at least one asset
                    asset_match = any(asset.lower() in title for asset in assets_lower)
                    if not asset_match:
                        continue
                    
                    # Must be active
                    if not (e.get("active") and not e.get("closed")):
                        continue
                    
                    filtered_events.append(e)
                
                log.info(f"Found {len(filtered_events)} active UP OR DOWN events for {assets}")
                
                # Extract markets from events
                markets = []
                for event in filtered_events:
                    for market in event.get("markets", []):
                        # Only process active markets
                        if market.get("active") and not market.get("closed"):
                            market_obj = self._parse_market(market)
                            if market_obj:
                                markets.append(market_obj)
                
                log.info(f"Found {len(markets)} active markets across all filtered events")
                
                # Cache the markets for price refreshes
                self.cached_markets = markets
                return markets
        
        except asyncio.TimeoutError:
            log.error("Timeout fetching events")
            return []
        except Exception as e:
            log.error(f"Error fetching events: {e}")
            return []
    
    async def refresh_prices(self, assets: List[str] = None) -> List[Market]:
        """
        Refresh prices on cached market list (cheap, run every 10 secs).
        
        Returns most recently cached markets with updated prices.
        If cache is empty, falls back to full discovery.
        
        Args:
            assets: List of assets (used only if cache is empty for fallback)
        
        Returns:
            List of Market objects with current prices
        """
        if not self.session:
            raise RuntimeError("Fetcher not initialized. Use async context manager.")
        
        # If no cached markets, do full discovery
        if not self.cached_markets:
            if assets is None:
                assets = ["Bitcoin"]
            return await self.fetch_market_list(assets)
        
        # Re-fetch prices for cached markets (lightweight)
        try:
            url = f"{self.clob_url}/events"
            params = {
                'limit': 100,
                'order': 'startDate',
                'ascending': 'false'
            }
            
            async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    log.error(f"Failed to refresh prices: {resp.status}")
                    return self.cached_markets  # Return stale cache
                
                events = await resp.json()
                
                # Build market map from events
                market_map = {}
                for event in events:
                    for market in event.get("markets", []):
                        market_id = market.get("id")
                        if market_id:
                            market_map[market_id] = market
                
                # Update cached markets with new prices
                updated = []
                for cached_market in self.cached_markets:
                    if cached_market.market_id in market_map:
                        raw = market_map[cached_market.market_id]
                        updated_market = self._parse_market(raw)
                        if updated_market:
                            updated.append(updated_market)
                    else:
                        # Market no longer exists, skip it
                        pass
                
                log.debug(f"Refreshed prices for {len(updated)}/{len(self.cached_markets)} cached markets")
                return updated
        
        except Exception as e:
            log.error(f"Error refreshing prices: {e}")
            return self.cached_markets  # Return stale cache on error
    
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
