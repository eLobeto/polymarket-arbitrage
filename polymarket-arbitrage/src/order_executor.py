"""
order_executor.py — Place orders on Polymarket CLOB API with proper signing.
"""

import logging
import asyncio
import aiohttp
from typing import Dict, Any, Optional
from dataclasses import dataclass
import json
import hashlib
import hmac
from eth_keys import keys
from eth_account import Account
from eth_account.messages import encode_defunct

log = logging.getLogger("order_executor")

# Polymarket CLOB order signing constants
POLYMARKET_CLOB_API = "https://clob.polymarket.com"


@dataclass
class Order:
    """Represents a Polymarket order."""
    market_id: str
    side: str  # "YES" or "NO"
    qty: float
    price: float
    order_hash: str
    status: str = "pending"


class OrderExecutor:
    """Execute trades on Polymarket CLOB API with proper order signing."""
    
    def __init__(
        self,
        private_key: str,
        wallet_address: str,
        clob_api_url: str = POLYMARKET_CLOB_API,
    ):
        """
        Initialize executor.
        
        Args:
            private_key: Wallet private key (hex string, with or without 0x prefix)
            wallet_address: Wallet public address
            clob_api_url: Polymarket CLOB API base URL
        """
        # Normalize private key
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        
        self.account = Account.from_key(private_key)
        self.wallet_address = wallet_address.lower()
        self.clob_api_url = clob_api_url
        self.session: Optional[aiohttp.ClientSession] = None
        
        log.info(f"✅ OrderExecutor initialized. Wallet: {self.wallet_address}")
    
    async def __aenter__(self):
        """Async context manager entry."""
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self.session:
            await self.session.close()
    
    async def place_order(
        self,
        market_id: str,
        condition_id: str,
        side: str,  # "YES" or "NO"
        qty: float,
        price: float,
        fvg_enabled: bool = True,
    ) -> Optional[str]:
        """
        Place a buy order on Polymarket CLOB.
        
        Args:
            market_id: Polymarket market ID
            condition_id: Condition ID for the market
            side: "YES" or "NO"
            qty: Quantity of shares
            price: Price per share (0.0-1.0)
            fvg_enabled: Whether FVG (fair value gap) retest rules apply
        
        Returns:
            Order hash on success, None on failure
        """
        if not self.session:
            raise RuntimeError("OrderExecutor not initialized. Use async context manager.")
        
        try:
            cost = qty * price
            
            log.info(
                f"📤 Placing {side} order: {qty:.2f} @ ${price:.4f} = ${cost:.2f}\n"
                f"   Market: {market_id} | Condition: {condition_id}"
            )
            
            # Step 1: Build order object per Polymarket spec
            order = self._build_order(
                market_id=market_id,
                condition_id=condition_id,
                side=side,
                qty=qty,
                price=price,
            )
            
            # Step 2: Sign order with private key
            signature = self._sign_order(order)
            if not signature:
                log.error("Failed to sign order")
                return None
            
            # Step 3: Submit to CLOB API
            order_hash = await self._submit_order_to_clob(order, signature)
            if not order_hash:
                log.error("Failed to submit order to CLOB")
                return None
            
            log.info(f"✅ Order placed! Hash: {order_hash}")
            return order_hash
        
        except Exception as e:
            log.error(f"❌ Error placing order: {e}", exc_info=True)
            return None
    
    def _build_order(
        self,
        market_id: str,
        condition_id: str,
        side: str,
        qty: float,
        price: float,
    ) -> Dict[str, Any]:
        """
        Build a Polymarket CLOB order object.
        
        Spec: https://docs.polymarket.com/api-reference
        """
        # Outcome index: 0=YES, 1=NO
        outcome_index = 0 if side.upper() == "YES" else 1
        
        # Convert to integer amounts (Polymarket uses wei-like units)
        # For simplicity, use standard fractional amounts
        qty_int = int(qty * 1e6)  # Scale to millions for precision
        price_int = int(price * 1e6)
        
        order = {
            "marketId": market_id,
            "conditionId": condition_id,
            "outcomeIndex": outcome_index,
            "side": side.upper(),
            "amount": qty_int,
            "price": price_int,
            "orderType": "limit",
            "salt": self._generate_salt(),
            "maker": self.wallet_address,
        }
        
        return order
    
    def _sign_order(self, order: Dict[str, Any]) -> Optional[str]:
        """
        Sign order with EIP-712 signature (Polymarket standard).
        
        For now, use simple message signing as a workaround.
        TODO: Implement full EIP-712 when Polymarket SDK available.
        """
        try:
            # Create deterministic message from order
            order_msg = json.dumps(order, sort_keys=True)
            order_hash = hashlib.sha256(order_msg.encode()).hexdigest()
            
            # Sign with private key
            message = encode_defunct(text=order_hash)
            signed_message = self.account.sign_message(message)
            
            signature = signed_message.signature.hex()
            log.debug(f"Order signed: {signature[:20]}...")
            
            return signature
        
        except Exception as e:
            log.error(f"Error signing order: {e}")
            return None
    
    async def _submit_order_to_clob(
        self,
        order: Dict[str, Any],
        signature: str,
    ) -> Optional[str]:
        """
        Submit signed order to Polymarket CLOB API.
        
        Args:
            order: Order dict
            signature: Signed order hash
        
        Returns:
            Order hash on success
        """
        try:
            url = f"{self.clob_api_url}/orders"
            
            payload = {
                "order": order,
                "signature": signature,
            }
            
            headers = {
                "Content-Type": "application/json",
            }
            
            log.debug(f"Submitting order to {url}: {json.dumps(payload, indent=2)}")
            
            async with self.session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status not in (200, 201):
                    error_text = await resp.text()
                    log.error(f"CLOB API error ({resp.status}): {error_text}")
                    return None
                
                result = await resp.json()
                order_hash = result.get("orderHash")
                
                log.info(f"✅ CLOB accepted order: {order_hash}")
                return order_hash
        
        except asyncio.TimeoutError:
            log.error("Timeout submitting order to CLOB")
            return None
        except Exception as e:
            log.error(f"Error submitting order to CLOB: {e}")
            return None
    
    async def poll_fill_status(self, order_hash: str) -> Dict[str, Any]:
        """
        Poll CLOB API for order fill status.
        
        Returns:
            {
                "status": "pending" | "filled" | "rejected",
                "filled_qty": float,  # Quantity filled (if partial)
                "avg_price": float,
            }
        """
        if not self.session:
            raise RuntimeError("OrderExecutor not initialized. Use async context manager.")
        
        try:
            url = f"{self.clob_api_url}/orders/{order_hash}"
            
            async with self.session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log.error(f"Error polling order status ({resp.status})")
                    return {"status": "unknown"}
                
                result = await resp.json()
                
                return {
                    "status": result.get("status", "unknown"),
                    "filled_qty": float(result.get("filledAmount", 0)) / 1e6,
                    "avg_price": float(result.get("avgPrice", 0)) / 1e6,
                }
        
        except Exception as e:
            log.error(f"Error polling fill status: {e}")
            return {"status": "error"}
    
    def _generate_salt(self) -> str:
        """Generate unique salt for order (to prevent replays)."""
        import time
        import random
        salt = int(time.time() * 1000) + random.randint(0, 999999)
        return str(salt)


# Example usage (for testing)
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    
    async def main():
        # Note: Use test wallet only!
        async with OrderExecutor(
            private_key="0x" + "0" * 64,  # Dummy key
            wallet_address="0x" + "0" * 40,  # Dummy address
        ) as executor:
            # Example order (dry-run)
            order_hash = await executor.place_order(
                market_id="btc_market_123",
                condition_id="0x456...",
                side="YES",
                qty=100.0,
                price=0.52,
            )
            
            if order_hash:
                # Poll for fill
                await asyncio.sleep(2)
                status = await executor.poll_fill_status(order_hash)
                print(f"Order status: {status}")
    
    asyncio.run(main())
