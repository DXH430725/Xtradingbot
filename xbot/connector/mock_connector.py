"""Mock connector implementation for testing.

Shows how to implement the IConnector interface.
Target: < 400 lines (for testing purposes).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional, Tuple, Any, Dict, List
from dataclasses import dataclass

from .interface import IConnector, ConnectorError, OrderNotFoundError
from ..execution.order_model import Order, OrderState


@dataclass
class MockConfig:
    """Configuration for mock connector."""
    venue_name: str = "mock"
    latency_ms: float = 50.0
    error_rate: float = 0.0  # 0.0 to 1.0
    auto_fill_rate: float = 0.8  # Rate at which orders auto-fill


class MockConnector:
    """Mock connector for testing and demonstration.

    Implements the IConnector interface with simulated behavior.
    """

    def __init__(self, config: MockConfig, *, debug: bool = False):
        self.config = config
        self.debug_enabled = debug
        self.log = logging.getLogger(f"xbot.connector.mock.{config.venue_name}")

        # Connection state
        self._started = False
        self._closed = False

        # Simulated market data
        self._symbols = {
            "BTC": {"price_scale": 2, "size_scale": 6, "min_size_i": 1000},
            "ETH": {"price_scale": 2, "size_scale": 6, "min_size_i": 1000},
            "SOL": {"price_scale": 3, "size_scale": 6, "min_size_i": 100},
        }

        # Simulated account
        self._account = {
            "available_balance": 10000.0,
            "total_balance": 10000.0,
            "positions": {}
        }

        # Order tracking
        self._orders: Dict[Tuple[str, int], Order] = {}  # (symbol, coi) -> Order
        self._next_exchange_id = 1

        # Auto-fill simulation
        self._auto_fill_tasks: List[asyncio.Task] = []

    async def start(self) -> None:
        """Start the connector."""
        if self._started:
            return

        self._started = True
        self.log.info(f"Mock connector started: {self.config.venue_name}")

    async def stop(self) -> None:
        """Stop the connector."""
        if not self._started:
            return

        # Cancel auto-fill tasks
        for task in self._auto_fill_tasks:
            if not task.done():
                task.cancel()

        try:
            await asyncio.gather(*self._auto_fill_tasks, return_exceptions=True)
        except Exception:
            pass

        self._auto_fill_tasks.clear()
        self._started = False
        self.log.info("Mock connector stopped")

    async def close(self) -> None:
        """Close the connector."""
        await self.stop()
        self._closed = True
        self.log.info("Mock connector closed")

    async def get_price_size_decimals(self, symbol: str) -> Tuple[int, int]:
        """Get price and size decimal places."""
        await self._simulate_latency()

        symbol_data = self._symbols.get(symbol.upper())
        if not symbol_data:
            raise ConnectorError(f"Unknown symbol: {symbol}")

        return symbol_data["price_scale"], symbol_data["size_scale"]

    async def get_min_size_i(self, symbol: str) -> int:
        """Get minimum order size."""
        await self._simulate_latency()

        symbol_data = self._symbols.get(symbol.upper())
        if not symbol_data:
            raise ConnectorError(f"Unknown symbol: {symbol}")

        return symbol_data["min_size_i"]

    async def get_top_of_book(self, symbol: str) -> Tuple[Optional[int], Optional[int], int]:
        """Get simulated top of book."""
        await self._simulate_latency()

        symbol_data = self._symbols.get(symbol.upper())
        if not symbol_data:
            return None, None, 100

        # Simulate prices
        scale = 10 ** symbol_data["price_scale"]
        base_price = {"BTC": 45000, "ETH": 2500, "SOL": 100}.get(symbol.upper(), 100)

        bid_i = int(base_price * scale)
        ask_i = int(base_price * scale * 1.001)  # 0.1% spread

        return bid_i, ask_i, scale

    async def get_order_book(self, symbol: str, depth: int = 5) -> Dict[str, Any]:
        """Get simulated order book."""
        await self._simulate_latency()

        bid_i, ask_i, scale = await self.get_top_of_book(symbol)
        if bid_i is None or ask_i is None:
            return {"bids": [], "asks": []}

        # Generate fake order book
        bids = []
        asks = []

        for i in range(depth):
            bid_price = (bid_i - i * scale // 1000) / scale
            ask_price = (ask_i + i * scale // 1000) / scale
            size = 1.0 + i * 0.5

            bids.append([bid_price, size])
            asks.append([ask_price, size])

        return {"bids": bids, "asks": asks}

    async def submit_limit_order(
        self,
        *,
        symbol: str,
        client_order_index: int,
        base_amount: int,
        price: int,
        is_ask: bool,
        post_only: bool = False,
        reduce_only: int = 0
    ) -> Order:
        """Submit simulated limit order."""
        await self._simulate_latency()
        await self._maybe_simulate_error()

        order = Order(
            coi=client_order_index,
            venue=self.config.venue_name,
            symbol=symbol,
            side="sell" if is_ask else "buy",
            size_i=base_amount,
            price_i=price,
            state=OrderState.SUBMITTING,
            is_limit=True,
            post_only=post_only,
            reduce_only=reduce_only,
            exchange_order_id=str(self._next_exchange_id)
        )

        self._next_exchange_id += 1

        # Store order
        key = (symbol, client_order_index)
        self._orders[key] = order

        # Simulate acknowledgment
        await asyncio.sleep(0.01)  # Small delay for ack
        order.update_state(OrderState.OPEN, "exchange_ack")

        # Maybe start auto-fill
        if random.random() < self.config.auto_fill_rate:
            task = asyncio.create_task(self._auto_fill_order(order))
            self._auto_fill_tasks.append(task)

        self.log.info(f"Submitted limit order: {symbol} {client_order_index}")
        return order

    async def submit_market_order(
        self,
        *,
        symbol: str,
        client_order_index: int,
        size_i: int,
        is_ask: bool,
        reduce_only: int = 0
    ) -> Order:
        """Submit simulated market order."""
        await self._simulate_latency()
        await self._maybe_simulate_error()

        order = Order(
            coi=client_order_index,
            venue=self.config.venue_name,
            symbol=symbol,
            side="sell" if is_ask else "buy",
            size_i=size_i,
            price_i=None,  # Market order
            state=OrderState.SUBMITTING,
            is_limit=False,
            reduce_only=reduce_only,
            exchange_order_id=str(self._next_exchange_id)
        )

        self._next_exchange_id += 1

        # Store order
        key = (symbol, client_order_index)
        self._orders[key] = order

        # Market orders fill immediately
        await asyncio.sleep(0.02)
        order.filled_base_i = size_i
        order.update_state(OrderState.FILLED, "market_fill")

        self.log.info(f"Submitted market order: {symbol} {client_order_index}")
        return order

    async def cancel_by_client_id(self, symbol: str, client_order_index: int) -> None:
        """Cancel order by client ID."""
        await self._simulate_latency()

        key = (symbol, client_order_index)
        order = self._orders.get(key)

        if not order:
            raise OrderNotFoundError(f"Order not found: {symbol} {client_order_index}")

        if order.is_final():
            self.log.warning(f"Cannot cancel final order: {order.state}")
            return

        order.update_state(OrderState.CANCELLED, "manual_cancel")
        self.log.info(f"Cancelled order: {symbol} {client_order_index}")

    async def cancel_all(self, timeout: Optional[float] = None) -> int:
        """Cancel all open orders."""
        cancelled_count = 0

        for order in list(self._orders.values()):
            if not order.is_final():
                order.update_state(OrderState.CANCELLED, "cancel_all")
                cancelled_count += 1

        self.log.info(f"Cancelled {cancelled_count} orders")
        return cancelled_count

    async def get_order(self, symbol: str, client_order_index: int) -> Dict[str, Any]:
        """Get order data."""
        await self._simulate_latency()

        key = (symbol, client_order_index)
        order = self._orders.get(key)

        if not order:
            raise OrderNotFoundError(f"Order not found: {symbol} {client_order_index}")

        return order.to_dict()

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get open orders."""
        await self._simulate_latency()

        open_orders = []
        for order in self._orders.values():
            if not order.is_final():
                if symbol is None or order.symbol == symbol:
                    open_orders.append(order.to_dict())

        return open_orders

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Get positions."""
        await self._simulate_latency()

        positions = []
        for symbol, position_data in self._account["positions"].items():
            positions.append({
                "symbol": symbol,
                "size": position_data.get("size", 0.0),
                "value": position_data.get("value", 0.0),
                "unrealized_pnl": position_data.get("unrealized_pnl", 0.0)
            })

        return positions

    async def get_account_overview(self) -> Dict[str, Any]:
        """Get account overview."""
        await self._simulate_latency()
        return self._account.copy()

    async def best_effort_latency_ms(self) -> float:
        """Get simulated latency."""
        return self.config.latency_ms

    async def list_symbols(self) -> List[str]:
        """List available symbols."""
        return list(self._symbols.keys())

    async def _simulate_latency(self) -> None:
        """Simulate network latency."""
        if self.config.latency_ms > 0:
            await asyncio.sleep(self.config.latency_ms / 1000.0)

    async def _maybe_simulate_error(self) -> None:
        """Maybe simulate an error."""
        if self.config.error_rate > 0 and random.random() < self.config.error_rate:
            raise ConnectorError("Simulated error")

    async def _auto_fill_order(self, order: Order) -> None:
        """Simulate order filling over time."""
        try:
            # Wait a bit before starting to fill
            await asyncio.sleep(0.5 + random.random() * 2.0)

            if order.is_final():
                return

            # Fill the order
            order.filled_base_i = order.size_i
            order.update_state(OrderState.FILLED, "auto_fill")

            self.log.info(f"Auto-filled order: {order.symbol} {order.coi}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.log.error(f"Auto-fill error: {e}")