"""
position_tracker.py — Track YES/NO positions and calculate arbitrage profits.
"""

import sqlite3
import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, Tuple
from pathlib import Path

log = logging.getLogger("position_tracker")


@dataclass
class Position:
    """Represents a YES/NO position pair."""
    market_id: str
    market_title: str
    qty_yes: float = 0.0
    cost_yes: float = 0.0  # Total USDC spent on YES
    qty_no: float = 0.0
    cost_no: float = 0.0   # Total USDC spent on NO
    created_at: datetime = field(default_factory=datetime.now)
    profit_locked: bool = False
    
    @property
    def avg_yes(self) -> float:
        """Average cost per YES share."""
        return self.cost_yes / self.qty_yes if self.qty_yes > 0 else 0
    
    @property
    def avg_no(self) -> float:
        """Average cost per NO share."""
        return self.cost_no / self.qty_no if self.qty_no > 0 else 0
    
    @property
    def pair_cost(self) -> float:
        """Combined average cost: avg_YES + avg_NO."""
        return self.avg_yes + self.avg_no
    
    @property
    def guaranteed_profit(self) -> float:
        """Risk-free profit if market resolves."""
        if self.qty_yes <= 0 or self.qty_no <= 0:
            return 0
        # Minimum of two quantities times $1 payout minus total cost
        min_qty = min(self.qty_yes, self.qty_no)
        return min_qty - (self.cost_yes + self.cost_no)
    
    @property
    def is_balanced(self, tolerance_pct: float = 0.05) -> bool:
        """Check if YES/NO quantities are balanced within tolerance."""
        if self.qty_yes <= 0 or self.qty_no <= 0:
            return False
        ratio = min(self.qty_yes, self.qty_no) / max(self.qty_yes, self.qty_no)
        return ratio >= (1 - tolerance_pct)


class PositionTracker:
    """Track open positions and arbitrage opportunities."""
    
    def __init__(self, db_path: str = "data/polymarket_trades.db"):
        """
        Initialize tracker with SQLite backend.
        
        Args:
            db_path: Path to SQLite database
        """
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self):
        """Create tables if they don't exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT NOT NULL,
                    market_title TEXT,
                    qty_yes REAL DEFAULT 0,
                    cost_yes REAL DEFAULT 0,
                    qty_no REAL DEFAULT 0,
                    cost_no REAL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP,
                    profit_locked INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'open'  -- 'open', 'closed', 'settled'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id INTEGER NOT NULL,
                    side TEXT,  -- 'YES' or 'NO'
                    qty REAL,
                    price REAL,
                    cost REAL,  -- qty * price
                    order_hash TEXT UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    tx_hash TEXT,
                    status TEXT DEFAULT 'pending',  -- 'pending', 'filled', 'failed'
                    FOREIGN KEY (position_id) REFERENCES positions(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS settlements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT NOT NULL,
                    winner TEXT,  -- 'YES' or 'NO'
                    payout REAL,
                    profit REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dry_run_opportunities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_slug TEXT,
                    market_title TEXT,
                    yes_price REAL,
                    no_price REAL,
                    pair_cost REAL,
                    guaranteed_profit REAL,
                    profit_pct REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'detected'  -- 'detected', 'executed', 'expired'
                )
            """)
            conn.commit()
            log.info("✅ Position database initialized")
    
    def create_position(self, market_id: str, market_title: str) -> int:
        """
        Create a new position pair.
        
        Args:
            market_id: Polymarket ID
            market_title: Market title
        
        Returns:
            Position ID
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """INSERT INTO positions (market_id, market_title, status)
                   VALUES (?, ?, 'open')""",
                (market_id, market_title)
            )
            conn.commit()
            position_id = cursor.lastrowid
            log.info(f"✅ Created position {position_id} for {market_title}")
            return position_id
    
    def add_trade(
        self,
        position_id: int,
        side: str,
        qty: float,
        price: float,
        order_hash: str,
        filled_qty: float = None,
    ):
        """
        Record a buy of YES or NO shares (handles partial fills).
        
        Args:
            position_id: Position ID
            side: 'YES' or 'NO'
            qty: Original quantity ordered
            price: Price per share
            order_hash: Polymarket order hash (for tracking)
            filled_qty: Actual quantity filled (defaults to qty if None)
        """
        # Handle partial fills
        actual_qty = filled_qty if filled_qty is not None else qty
        cost = actual_qty * price
        
        with sqlite3.connect(self.db_path) as conn:
            # Determine fill status
            fill_status = "filled"
            if filled_qty and filled_qty < qty * 0.99:  # Less than 99% filled
                fill_status = "partial"
                log.warning(
                    f"⚠️ Partial fill for {side}: {actual_qty:.2f}/{qty:.2f} shares "
                    f"({actual_qty/qty*100:.1f}%)"
                )
            
            conn.execute(
                """INSERT INTO trades (position_id, side, qty, price, cost, order_hash, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (position_id, side, actual_qty, price, cost, order_hash, fill_status)
            )
            
            # Update position totals with ACTUAL filled qty
            if side == "YES":
                conn.execute(
                    """UPDATE positions SET qty_yes = qty_yes + ?, cost_yes = cost_yes + ?
                       WHERE id = ?""",
                    (actual_qty, cost, position_id)
                )
            else:  # NO
                conn.execute(
                    """UPDATE positions SET qty_no = qty_no + ?, cost_no = cost_no + ?
                       WHERE id = ?""",
                    (actual_qty, cost, position_id)
                )
            
            conn.commit()
            log.info(
                f"💰 Added {side} trade: {actual_qty:.2f} @ ${price:.4f} = ${cost:.2f} "
                f"[{fill_status}]"
            )
    
    def get_position(self, position_id: int) -> Optional[Position]:
        """
        Fetch a position by ID.
        
        Args:
            position_id: Position ID
        
        Returns:
            Position object or None
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT * FROM positions WHERE id = ?",
                (position_id,)
            )
            row = cursor.fetchone()
        
        if not row:
            return None
        
        return Position(
            market_id=row[1],
            market_title=row[2],
            qty_yes=row[3],
            cost_yes=row[4],
            qty_no=row[5],
            cost_no=row[6],
            created_at=datetime.fromisoformat(row[7]),
            profit_locked=bool(row[10]),
        )
    
    def lock_profit(self, position_id: int):
        """Mark position as profit-locked."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE positions SET profit_locked = 1 WHERE id = ?",
                (position_id,)
            )
            conn.commit()
            log.info(f"🔒 Position {position_id} profit locked!")
    
    def close_position(self, position_id: int):
        """Close a position after settlement."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE positions SET status = 'closed', closed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (position_id,)
            )
            conn.commit()
            log.info(f"✅ Position {position_id} closed")
    
    def get_all_open(self) -> list[Position]:
        """Get all open positions."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT id FROM positions WHERE status = 'open'")
            ids = [row[0] for row in cursor.fetchall()]
        
        return [self.get_position(pid) for pid in ids if self.get_position(pid)]
    
    def log_dry_run_opportunity(self, market_slug: str, market_title: str, 
                                yes_price: float, no_price: float):
        """Log a dry-run arbitrage opportunity detected."""
        pair_cost = yes_price + no_price
        guaranteed_profit = 1.0 - pair_cost
        profit_pct = (guaranteed_profit / (yes_price + no_price)) * 100 if pair_cost > 0 else 0
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO dry_run_opportunities 
                   (market_slug, market_title, yes_price, no_price, pair_cost, guaranteed_profit, profit_pct, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'detected')""",
                (market_slug, market_title, yes_price, no_price, pair_cost, guaranteed_profit, profit_pct)
            )
            conn.commit()
            log.info(f"📊 Logged dry-run opportunity: {market_slug} | ${guaranteed_profit:.4f} ({profit_pct:.2f}%)")
    
    def get_dry_run_stats(self) -> dict:
        """Get statistics on dry-run opportunities."""
        with sqlite3.connect(self.db_path) as conn:
            # Total opportunities
            cursor = conn.execute("SELECT COUNT(*) as count FROM dry_run_opportunities")
            total = cursor.fetchone()[0]
            
            # Total potential profit
            cursor = conn.execute("SELECT SUM(guaranteed_profit) as total FROM dry_run_opportunities")
            total_profit = cursor.fetchone()[0] or 0
            
            # Best opportunity
            cursor = conn.execute(
                "SELECT market_title, guaranteed_profit, profit_pct FROM dry_run_opportunities ORDER BY guaranteed_profit DESC LIMIT 1"
            )
            best = cursor.fetchone()
            
            # Average profit per opportunity
            avg_profit = total_profit / total if total > 0 else 0
            
            return {
                "total_opportunities": total,
                "total_potential_profit": round(total_profit, 4),
                "avg_profit_per_opp": round(avg_profit, 4),
                "best_opportunity": {
                    "title": best[0] if best else None,
                    "profit": round(best[1], 4) if best else 0,
                    "profit_pct": round(best[2], 2) if best else 0
                } if best else None
            }


# Example usage (for testing)
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    
    tracker = PositionTracker()
    
    # Create a position
    pos_id = tracker.create_position("btc_15min_001", "Bitcoin: 4:00-4:15pm")
    
    # Add trades
    tracker.add_trade(pos_id, "YES", 100, 0.52, "hash_001")
    tracker.add_trade(pos_id, "NO", 100, 0.45, "hash_002")
    
    # Check position
    pos = tracker.get_position(pos_id)
    print(f"\nPosition: {pos.market_title}")
    print(f"  Qty YES: {pos.qty_yes} @ avg ${pos.avg_yes:.4f}")
    print(f"  Qty NO: {pos.qty_no} @ avg ${pos.avg_no:.4f}")
    print(f"  Pair Cost: ${pos.pair_cost:.4f} (target < $1.00)")
    print(f"  Guaranteed Profit: ${pos.guaranteed_profit:.2f}")
