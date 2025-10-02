from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, Tuple


class IConnector(Protocol):
    """Async exchange connector contract required by the execution layer."""

    venue: str

    async def start(self) -> None:
        """Connect all underlying transports (REST/WS) and bootstrap state."""

    async def stop(self) -> None:
        """Tear down transports and release resources."""

    async def get_price_size_decimals(self, symbol: str) -> Tuple[int, int]:
        """Return (price_decimals, size_decimals) for a venue specific symbol."""

    async def get_min_size_i(self, symbol: str) -> int:
        """Return the minimum allowed order size expressed in integer units."""

    async def get_top_of_book(self, symbol: str) -> Tuple[Optional[int], Optional[int], int]:
        """Return best bid/ask and price scale for the provided symbol."""

    async def submit_limit_order(
        self,
        *,
        symbol: str,
        client_order_index: int,
        base_amount: int,
        price: int,
        is_ask: bool,
        post_only: bool = False,
        reduce_only: int = 0,
    ) -> str:
        """Create a limit order and return the exchange-generated order id."""

    async def submit_market_order(
        self,
        *,
        symbol: str,
        client_order_index: int,
        size_i: int,
        is_ask: bool,
        reduce_only: int = 0,
    ) -> str:
        """Create a market order and return the exchange-generated order id."""

    async def cancel_by_client_id(self, symbol: str, client_order_index: int) -> None:
        """Cancel an order previously created with the provided client order index."""

    async def get_order(self, symbol: str, client_order_index: int) -> Dict[str, Any]:
        """Return the latest order information for the provided identifiers."""

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Return current open positions as dictionaries (venue specific schema)."""

    async def get_margin(self) -> Dict[str, Any]:
        """Return the current margin snapshot when supported by the venue."""


__all__ = ["IConnector"]
