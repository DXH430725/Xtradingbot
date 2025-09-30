"""Standardized connector interface for all exchanges.

This is the ONLY connector interface that should be used.
Target: < 200 lines.
"""

from __future__ import annotations

from typing import Protocol, Optional, Tuple, Any, Dict, List


class IConnector(Protocol):
    """Standardized connector interface for all exchanges.

    All connector implementations MUST conform to this interface.
    This ensures consistency and enables easy swapping of exchanges.
    """

    # Lifecycle management
    async def start(self) -> None:
        """Start the connector (establish connections, start websocket, etc)."""
        ...

    async def stop(self) -> None:
        """Stop the connector (close connections, cleanup)."""
        ...

    async def close(self) -> None:
        """Close all resources and connections."""
        ...

    # Market data and rules
    async def get_price_size_decimals(self, symbol: str) -> Tuple[int, int]:
        """Get price and size decimal places for symbol.

        Args:
            symbol: Trading symbol (venue-specific format)

        Returns:
            Tuple of (price_decimals, size_decimals)
        """
        ...

    async def get_min_size_i(self, symbol: str) -> int:
        """Get minimum order size in integer format.

        Args:
            symbol: Trading symbol (venue-specific format)

        Returns:
            Minimum order size as integer
        """
        ...

    async def get_top_of_book(self, symbol: str) -> Tuple[Optional[int], Optional[int], int]:
        """Get top of order book prices.

        Args:
            symbol: Trading symbol (venue-specific format)

        Returns:
            Tuple of (bid_price_i, ask_price_i, price_scale)
            where prices are in integer format or None if not available
        """
        ...

    async def get_order_book(self, symbol: str, depth: int = 5) -> Dict[str, Any]:
        """Get order book data.

        Args:
            symbol: Trading symbol (venue-specific format)
            depth: Number of levels to retrieve

        Returns:
            Order book data with 'bids' and 'asks' lists
        """
        ...

    # Order placement and management
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
    ) -> Any:
        """Submit limit order.

        Args:
            symbol: Trading symbol (venue-specific format)
            client_order_index: Client-assigned order ID
            base_amount: Order size in integer format
            price: Order price in integer format
            is_ask: True for sell, False for buy
            post_only: Post-only flag
            reduce_only: Reduce-only flag (0 or 1)

        Returns:
            Order tracking object or exchange order ID
        """
        ...

    async def submit_market_order(
        self,
        *,
        symbol: str,
        client_order_index: int,
        size_i: int,
        is_ask: bool,
        reduce_only: int = 0
    ) -> Any:
        """Submit market order.

        Args:
            symbol: Trading symbol (venue-specific format)
            client_order_index: Client-assigned order ID
            size_i: Order size in integer format
            is_ask: True for sell, False for buy
            reduce_only: Reduce-only flag (0 or 1)

        Returns:
            Order tracking object or exchange order ID
        """
        ...

    async def cancel_by_client_id(self, symbol: str, client_order_index: int) -> None:
        """Cancel order by client order index.

        Args:
            symbol: Trading symbol (venue-specific format)
            client_order_index: Client order ID to cancel
        """
        ...

    async def cancel_all(self, timeout: Optional[float] = None) -> Any:
        """Cancel all open orders.

        Args:
            timeout: Maximum time to wait for cancellations

        Returns:
            Cancellation result or count
        """
        ...

    # Account and position queries
    async def get_order(self, symbol: str, client_order_index: int) -> Dict[str, Any]:
        """Get order status by client order index.

        Args:
            symbol: Trading symbol (venue-specific format)
            client_order_index: Client order ID

        Returns:
            Order data dictionary
        """
        ...

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get open orders.

        Args:
            symbol: Optional symbol filter (venue-specific format)

        Returns:
            List of open order data
        """
        ...

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Get current positions.

        Returns:
            List of position data
        """
        ...

    async def get_account_overview(self) -> Dict[str, Any]:
        """Get account overview including balances.

        Returns:
            Account data including available_balance, total_balance, etc.
        """
        ...

    # Optional: Performance metrics
    async def best_effort_latency_ms(self) -> float:
        """Get best-effort latency estimate in milliseconds.

        Returns:
            Estimated latency to exchange
        """
        return 100.0  # Default fallback

    # Optional: Symbol listing
    async def list_symbols(self) -> List[str]:
        """List available trading symbols.

        Returns:
            List of available symbols in venue-specific format
        """
        return []  # Default fallback


class ConnectorError(Exception):
    """Base exception for connector errors."""
    pass


class ConnectorTimeoutError(ConnectorError):
    """Raised when connector operations timeout."""
    pass


class ConnectorNotReadyError(ConnectorError):
    """Raised when connector is not ready for operations."""
    pass


class InsufficientBalanceError(ConnectorError):
    """Raised when account has insufficient balance for operation."""
    pass


class OrderNotFoundError(ConnectorError):
    """Raised when requested order is not found."""
    pass


class InvalidSymbolError(ConnectorError):
    """Raised when symbol is not supported by exchange."""
    pass