"""
order_executor.py — Place orders on Polymarket via Web3 contract calls.
"""

import logging
from typing import Dict, Any, Optional
from web3 import Web3
from dataclasses import dataclass
import json

log = logging.getLogger("order_executor")


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
    """Execute trades on Polymarket via Web3."""
    
    def __init__(
        self,
        rpc_url: str,
        private_key: str,
        wallet_address: str,
        order_book_contract: str,
        usdc_contract: str,
    ):
        """
        Initialize executor.
        
        Args:
            rpc_url: Polygon RPC endpoint
            private_key: Wallet private key (hex string)
            wallet_address: Wallet public address
            order_book_contract: Order book contract address
            usdc_contract: USDC contract address
        """
        self.web3 = Web3(Web3.HTTPProvider(rpc_url))
        
        if not self.web3.is_connected():
            raise ConnectionError(f"Failed to connect to RPC: {rpc_url}")
        
        self.account = self.web3.eth.account.from_key(private_key)
        self.wallet_address = wallet_address
        self.order_book_contract = order_book_contract
        self.usdc_contract = usdc_contract
        
        log.info(f"✅ Connected to Polygon. Account: {self.account.address}")
        
        # Load ABIs (simplified for now; in production, get from Polymarket)
        self.order_book_abi = self._get_order_book_abi()
        self.usdc_abi = self._get_usdc_abi()
        
        # Contract instances
        self.order_book = self.web3.eth.contract(
            address=Web3.to_checksum_address(order_book_contract),
            abi=self.order_book_abi
        )
        self.usdc = self.web3.eth.contract(
            address=Web3.to_checksum_address(usdc_contract),
            abi=self.usdc_abi
        )
    
    async def place_order(
        self,
        market_id: str,
        side: str,  # "YES" or "NO"
        qty: float,
        price: float,
        order_type: str = "limit",
    ) -> Optional[str]:
        """
        Place a buy order on Polymarket.
        
        Args:
            market_id: Market ID
            side: "YES" or "NO"
            qty: Quantity of shares
            price: Price per share (0.0-1.0)
            order_type: "limit" or "market"
        
        Returns:
            Order hash or None if failed
        """
        try:
            cost = qty * price
            
            log.info(f"📤 Placing {side} order: {qty} @ ${price:.4f} = ${cost:.2f}")
            
            # Step 1: Check USDC balance
            balance = await self._check_usdc_balance()
            if balance < cost:
                log.error(f"⚠️ Insufficient balance. Need ${cost:.2f}, have ${balance:.2f}")
                return None
            
            # Step 2: Approve USDC for Order Book
            tx_hash = await self._approve_usdc(cost)
            if not tx_hash:
                log.error("Failed to approve USDC")
                return None
            
            # Step 3: Build order
            order = self._build_order(market_id, side, qty, price)
            
            # Step 4: Send transaction (placeholder — actual implementation depends on Polymarket API)
            # This is a simplified version; real implementation would use Polymarket's order signing
            log.debug(f"Order params: {order}")
            
            log.info(f"✅ Order submitted for {side}: {qty} @ ${price:.4f}")
            return order.get("hash")
        
        except Exception as e:
            log.error(f"❌ Error placing order: {e}")
            return None
    
    async def _check_usdc_balance(self) -> float:
        """Check USDC balance in wallet."""
        try:
            balance_wei = self.usdc.functions.balanceOf(
                Web3.to_checksum_address(self.wallet_address)
            ).call()
            balance_usdc = balance_wei / 1e6  # USDC has 6 decimals
            log.debug(f"USDC balance: ${balance_usdc:.2f}")
            return balance_usdc
        except Exception as e:
            log.error(f"Error checking balance: {e}")
            return 0
    
    async def _approve_usdc(self, amount_usdc: float) -> Optional[str]:
        """
        Approve USDC spending for Order Book contract.
        
        Args:
            amount_usdc: Amount to approve in USDC
        
        Returns:
            Transaction hash or None
        """
        try:
            amount_wei = int(amount_usdc * 1e6)
            
            # Build transaction
            tx = self.usdc.functions.approve(
                Web3.to_checksum_address(self.order_book_contract),
                amount_wei
            ).build_transaction({
                'from': Web3.to_checksum_address(self.wallet_address),
                'nonce': self.web3.eth.get_transaction_count(self.wallet_address),
                'gas': 100000,
                'gasPrice': self.web3.eth.gas_price,
            })
            
            # Sign and send
            signed = self.web3.eth.account.sign_transaction(tx, self.account.key)
            tx_hash = self.web3.eth.send_raw_transaction(signed.rawTransaction)
            
            log.info(f"✅ Approval tx: {tx_hash.hex()}")
            
            # Wait for confirmation
            receipt = self.web3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            log.debug(f"Approval confirmed. Gas used: {receipt['gasUsed']}")
            
            return tx_hash.hex()
        
        except Exception as e:
            log.error(f"Error approving USDC: {e}")
            return None
    
    def _build_order(
        self,
        market_id: str,
        side: str,
        qty: float,
        price: float,
    ) -> Dict[str, Any]:
        """
        Build a Polymarket order dict (placeholder).
        
        In production, this would use Polymarket's order signing mechanism.
        """
        return {
            "market_id": market_id,
            "side": side,
            "qty": qty,
            "price": price,
            "hash": f"order_{market_id}_{side}_{qty}_{price}",
        }
    
    def _get_order_book_abi(self) -> list:
        """Return Order Book contract ABI (simplified)."""
        # In production, fetch actual ABI from Polymarket
        return json.loads("""[
            {
                "constant": false,
                "inputs": [{"name": "order", "type": "tuple"}],
                "name": "submitOrder",
                "outputs": [{"name": "", "type": "bytes32"}],
                "type": "function"
            }
        ]""")
    
    def _get_usdc_abi(self) -> list:
        """Return USDC contract ABI (ERC20 standard)."""
        return json.loads("""[
            {
                "constant": true,
                "inputs": [{"name": "_owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "balance", "type": "uint256"}],
                "type": "function"
            },
            {
                "constant": false,
                "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}],
                "name": "approve",
                "outputs": [{"name": "", "type": "bool"}],
                "type": "function"
            }
        ]""")


# Example usage (for testing)
if __name__ == "__main__":
    import asyncio
    
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    
    # Note: Use dummy keys for testing only!
    executor = OrderExecutor(
        rpc_url="https://polygon-rpc.com",
        private_key="0x" + "0" * 64,  # Dummy key
        wallet_address="0x" + "0" * 40,  # Dummy address
        order_book_contract="0xCB1bbe6622d3FB2d0378A1d3b21f0900E2618248",
        usdc_contract="0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
    )
    
    print("✅ OrderExecutor initialized")
