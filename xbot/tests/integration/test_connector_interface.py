"""Integration tests for connector interface compliance."""

import pytest
import asyncio
from typing import Any

from xbot.connector.interface import IConnector
from xbot.connector.mock_connector import MockConnector, MockConfig
from xbot.execution.order_model import OrderState


class TestConnectorInterfaceCompliance:
    """Test that connectors properly implement the IConnector interface."""

    @pytest.fixture
    async def mock_connector(self) -> MockConnector:
        """Create and start a mock connector."""
        config = MockConfig(venue_name="test_mock", latency_ms=1.0)
        connector = MockConnector(config)
        await connector.start()
        yield connector
        await connector.close()

    @pytest.mark.asyncio
    async def test_lifecycle_methods(self, mock_connector: MockConnector):
        """Test connector lifecycle methods."""
        # Should already be started by fixture
        assert mock_connector._started is True

        # Test stop/start cycle
        await mock_connector.stop()
        assert mock_connector._started is False

        await mock_connector.start()
        assert mock_connector._started is True

        # Test close
        await mock_connector.close()
        assert mock_connector._closed is True

    @pytest.mark.asyncio
    async def test_market_data_methods(self, mock_connector: MockConnector):
        """Test market data retrieval methods."""
        symbol = "BTC"

        # Test price/size decimals
        price_dec, size_dec = await mock_connector.get_price_size_decimals(symbol)
        assert isinstance(price_dec, int)
        assert isinstance(size_dec, int)
        assert price_dec > 0
        assert size_dec > 0

        # Test minimum size
        min_size = await mock_connector.get_min_size_i(symbol)
        assert isinstance(min_size, int)
        assert min_size > 0

        # Test top of book
        bid_i, ask_i, scale = await mock_connector.get_top_of_book(symbol)
        assert bid_i is not None
        assert ask_i is not None
        assert isinstance(scale, int)
        assert bid_i < ask_i  # Basic sanity check

        # Test order book
        book = await mock_connector.get_order_book(symbol, depth=3)
        assert "bids" in book
        assert "asks" in book
        assert len(book["bids"]) <= 3
        assert len(book["asks"]) <= 3

    @pytest.mark.asyncio
    async def test_limit_order_lifecycle(self, mock_connector: MockConnector):
        """Test limit order placement and lifecycle."""
        symbol = "BTC"
        coi = 12345
        size_i = 1000000
        price_i = 4500000

        # Submit limit order
        order = await mock_connector.submit_limit_order(
            symbol=symbol,
            client_order_index=coi,
            base_amount=size_i,
            price=price_i,
            is_ask=False
        )

        assert order.coi == coi
        assert order.symbol == symbol
        assert order.side == "buy"
        assert order.size_i == size_i
        assert order.price_i == price_i
        assert order.is_limit is True

        # Order should become OPEN after submission
        await asyncio.sleep(0.1)  # Allow for async state update
        assert order.state in [OrderState.SUBMITTING, OrderState.OPEN]

        # Test order query
        order_data = await mock_connector.get_order(symbol, coi)
        assert order_data["coi"] == coi
        assert order_data["symbol"] == symbol

        # Test order in open orders list
        open_orders = await mock_connector.get_open_orders(symbol)
        cois = [o["coi"] for o in open_orders]
        assert coi in cois

        # Cancel the order
        await mock_connector.cancel_by_client_id(symbol, coi)

        # Wait for cancellation to process
        await asyncio.sleep(0.1)
        assert order.state == OrderState.CANCELLED

    @pytest.mark.asyncio
    async def test_market_order(self, mock_connector: MockConnector):
        """Test market order placement."""
        symbol = "BTC"
        coi = 54321
        size_i = 500000

        order = await mock_connector.submit_market_order(
            symbol=symbol,
            client_order_index=coi,
            size_i=size_i,
            is_ask=True
        )

        assert order.coi == coi
        assert order.symbol == symbol
        assert order.side == "sell"
        assert order.size_i == size_i
        assert order.price_i is None  # Market order
        assert order.is_limit is False

        # Market orders should fill immediately
        await asyncio.sleep(0.1)
        assert order.state == OrderState.FILLED
        assert order.filled_base_i == size_i

    @pytest.mark.asyncio
    async def test_cancel_all(self, mock_connector: MockConnector):
        """Test cancel all orders functionality."""
        symbol = "BTC"

        # Submit multiple orders
        orders = []
        for i in range(3):
            order = await mock_connector.submit_limit_order(
                symbol=symbol,
                client_order_index=1000 + i,
                base_amount=100000,
                price=4500000 + i * 1000,
                is_ask=False
            )
            orders.append(order)

        # Wait for orders to become open
        await asyncio.sleep(0.1)

        # Cancel all
        cancelled_count = await mock_connector.cancel_all()
        assert cancelled_count >= 3

        # Wait for cancellations
        await asyncio.sleep(0.1)

        # All orders should be cancelled
        for order in orders:
            assert order.state == OrderState.CANCELLED

    @pytest.mark.asyncio
    async def test_account_methods(self, mock_connector: MockConnector):
        """Test account and position methods."""
        # Test account overview
        account = await mock_connector.get_account_overview()
        assert "available_balance" in account
        assert "total_balance" in account
        assert isinstance(account["available_balance"], (int, float))

        # Test positions
        positions = await mock_connector.get_positions()
        assert isinstance(positions, list)

    @pytest.mark.asyncio
    async def test_optional_methods(self, mock_connector: MockConnector):
        """Test optional interface methods."""
        # Test latency measurement
        latency = await mock_connector.best_effort_latency_ms()
        assert isinstance(latency, (int, float))
        assert latency >= 0

        # Test symbol listing
        symbols = await mock_connector.list_symbols()
        assert isinstance(symbols, list)
        assert "BTC" in symbols

    @pytest.mark.asyncio
    async def test_error_handling(self, mock_connector: MockConnector):
        """Test error handling for invalid operations."""
        # Test invalid symbol
        with pytest.raises(Exception):
            await mock_connector.get_price_size_decimals("INVALID_SYMBOL")

        # Test order not found
        with pytest.raises(Exception):
            await mock_connector.get_order("BTC", 999999)

        # Test cancelling non-existent order
        with pytest.raises(Exception):
            await mock_connector.cancel_by_client_id("BTC", 999999)

    @pytest.mark.asyncio
    async def test_interface_type_checking(self):
        """Test that MockConnector implements IConnector interface."""
        config = MockConfig()
        connector = MockConnector(config)

        # This should not raise any type checking errors
        # if MockConnector properly implements IConnector
        def use_connector(conn: IConnector) -> None:
            pass

        use_connector(connector)

    @pytest.mark.asyncio
    async def test_order_auto_fill_simulation(self, mock_connector: MockConnector):
        """Test the auto-fill simulation feature."""
        # Create connector with high auto-fill rate
        config = MockConfig(auto_fill_rate=1.0, latency_ms=1.0)
        connector = MockConnector(config)
        await connector.start()

        try:
            symbol = "BTC"
            coi = 99999
            size_i = 1000000

            order = await connector.submit_limit_order(
                symbol=symbol,
                client_order_index=coi,
                base_amount=size_i,
                price=4500000,
                is_ask=False
            )

            # Wait for auto-fill (should happen within a few seconds)
            final_state = await order.wait_final(timeout=5.0)
            assert final_state == OrderState.FILLED
            assert order.filled_base_i == size_i

        finally:
            await connector.close()

    @pytest.mark.asyncio
    async def test_concurrent_operations(self, mock_connector: MockConnector):
        """Test concurrent connector operations."""
        symbol = "BTC"

        # Submit multiple orders concurrently
        async def submit_order(coi: int) -> Any:
            return await mock_connector.submit_limit_order(
                symbol=symbol,
                client_order_index=coi,
                base_amount=100000,
                price=4500000 + coi,
                is_ask=False
            )

        # Submit 5 orders concurrently
        orders = await asyncio.gather(*[
            submit_order(2000 + i) for i in range(5)
        ])

        assert len(orders) == 5
        for i, order in enumerate(orders):
            assert order.coi == 2000 + i

        # Cancel them all concurrently
        async def cancel_order(order: Any) -> None:
            await mock_connector.cancel_by_client_id(order.symbol, order.coi)

        await asyncio.gather(*[cancel_order(order) for order in orders])

        # All should be cancelled
        await asyncio.sleep(0.1)
        for order in orders:
            assert order.state == OrderState.CANCELLED