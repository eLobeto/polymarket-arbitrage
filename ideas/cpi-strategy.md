# CPI / Inflation Trading Strategy 📉

## Thesis
Build a "Synthetic CPI" agent that aggregates real-time price data to predict the official BLS print before it happens.

## Target Markets (Kalshi)
- `KXCPICOREYOY` (Core CPI YoY) - Top liquidity
- `KXCPIGAS` (Gasoline CPI)
- `KXCPIUSEDCAR` (Used Cars CPI)

## Data Sources needed
1. **Gas:** AAA Daily Fuel Gauge (Scrape)
2. **Used Cars:** Manheim Index / CarGurus trends
3. **Rent:** Zillow Observed Rent Index (ZORI)
4. **Food:** USDA / Commodities futures
