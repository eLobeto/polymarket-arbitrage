"""
order_executor.py — Place orders on Polymarket using official py-clob-client SDK.

Uses the official Polymarket SDK which handles:
- EIP-712 signing for authentication
- L1 API credential generation
- L2 HMAC-SHA256 request signing
- Order creation and submission
"""

import logging
import os
from typing import Optional, Dict, Any
from pathlib import Path

log = logging.getLogger("order_executor")


class OrderExecutor:
    """Execute trades on Polymarket using official CLOB SDK."""
    
    def __init__(
        self,
        private_key: str,
        wallet_address: str,
        clob_api_url: str = "https://clob.polymarket.com",
    ):
        """
        Initialize executor with official Polymarket SDK.
        
        Args:
            private_key: Wallet private key (hex string, with or without 0x prefix)
            wallet_address: Wallet public address
            clob_api_url: Polymarket CLOB API base URL
        """
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import OrderArgs, OrderType
        except ImportError:
            raise RuntimeError(
                "py-clob-client not installed. Run: pip install py-clob-client==1.8.0"
            )
        
        # Normalize private key
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        
        self.private_key = private_key
        self.wallet_address = wallet_address.lower()
        self.clob_api_url = clob_api_url
        
        self.ClobClient = ClobClient
        self.OrderArgs = OrderArgs
        self.OrderType = OrderType
        
        # Initialize SDK client
        try:
            self.client = ClobClient(
                host=clob_api_url,
                chain_id=137,  # Polygon mainnet
                key=private_key,
            )
            
            log.info(f"✅ OrderExecutor initialized with official SDK")
            log.info(f"   Wallet: {self.wallet_address}")
            log.info(f"   CLOB API: {clob_api_url}")
            
        except Exception as e:
            log.error(f"Failed to initialize ClobClient: {e}")
            raise
    
    async def _ensure_api_credentials(self):
        """Generate or retrieve API credentials for L2 authentication."""
        try:
            # Try to derive existing credentials
            creds = self.client.create_or_derive_api_creds()
            
            if creds:
                log.info("✅ API credentials ready for L2 authentication")
                log.debug(f"   API Key: {creds.get('apiKey', '')[:8]}...")
                return creds
            else:
                log.error("Failed to get API credentials")
                return None
                
        except Exception as e:
            log.error(f"Error getting API credentials: {e}")
            return None
    
    async def place_order(
        self,
        market_id: str,
        condition_id: str,
        side: str,  # "YES" or "NO"
        qty: float,
        price: float,
    ) -> Optional[str]:
        """
        Place a buy order on Polymarket using official SDK.
        
        Args:
            market_id: Polymarket market ID (token_id)
            condition_id: Condition ID (unused by SDK, but kept for interface)
            side: "YES" or "NO"
            qty: Quantity of shares
            price: Price per share (0.0-1.0)
        
        Returns:
            Order ID on success, None on failure
        """
        try:
            # Ensure we have API credentials
            creds = await self._ensure_api_credentials()
            if not creds:
                log.error("Cannot place order without API credentials")
                return None
            
            cost = qty * price
            
            log.info(
                f"📤 Placing {side} order: {qty:.2f} @ ${price:.4f} = ${cost:.2f}\n"
                f"   Market: {market_id}"
            )
            
            # Convert side to SDK format
            from py_clob_client.order_builder.constants import BUY, SELL
            sdk_side = BUY if side.upper() == "YES" else SELL
            
            # Create and post order using official SDK
            response = self.client.create_and_post_order(
                self.OrderArgs(
                    token_id=market_id,
                    price=price,
                    size=qty,
                    side=sdk_side,
                ),
                options={
                    "tick_size": "0.01",
                    "neg_risk": False,
                },
                order_type=self.OrderType.GTC,  # Good-Till-Cancelled
            )
            
            if response and response.get("success"):
                order_id = response.get("orderID")
                status = response.get("status")
                
                log.info(
                    f"✅ Order placed successfully!\n"
                    f"   Order ID: {order_id}\n"
                    f"   Status: {status}"
                )
                
                return order_id
            else:
                error_msg = response.get("errorMsg") if response else "Unknown error"
                log.error(f"Order submission failed: {error_msg}")
                return None
        
        except Exception as e:
            log.error(f"❌ Error placing order: {e}", exc_info=True)
            return None
    
    async def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """
        Get order status from CLOB API.
        
        Returns:
            {
                "status": "live" | "filled" | "cancelled" | "error",
                "filled_qty": float,
                "avg_price": float,
            }
        """
        try:
            # SDK may have method to get order details
            response = self.client.get_order(order_id)
            
            if response:
                return {
                    "status": response.get("status", "unknown"),
                    "filled_qty": float(response.get("filledAmount", 0)),
                    "avg_price": float(response.get("avgPrice", 0)),
                }
            else:
                return {"status": "error"}
        
        except Exception as e:
            log.error(f"Error getting order status: {e}")
            return {"status": "error"}
    
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        try:
            response = self.client.cancel_order(order_id)
            
            if response and response.get("success"):
                log.info(f"✅ Order {order_id} cancelled")
                return True
            else:
                log.error(f"Failed to cancel order {order_id}")
                return False
        
        except Exception as e:
            log.error(f"Error cancelling order: {e}")
            return False
    
    def get_balance(self, token: str = "USDC") -> float:
        """Get wallet USDC balance (synchronous)."""
        try:
            # Get balances using SDK
            balances = self.client.get_balances()
            
            if token in balances:
                return float(balances[token])
            else:
                log.warning(f"Token {token} not found in balances")
                return 0.0
        
        except Exception as e:
            log.error(f"Error getting balance: {e}")
            return 0.0


# Example usage (for testing)
if __name__ == "__main__":
    import asyncio
    
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    
    async def main():
        executor = OrderExecutor(
            private_key=os.getenv("WALLET_PRIVATE_KEY", "0x" + "0" * 64),
            wallet_address=os.getenv("WALLET_ADDRESS", "0x" + "0" * 40),
        )
        
        print("✅ OrderExecutor initialized with official SDK")
        
        # Test: get balance
        balance = await executor.get_balance("USDC")
        print(f"USDC Balance: ${balance:.2f}")
    
    asyncio.run(main())
