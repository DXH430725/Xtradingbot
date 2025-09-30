"""Unit tests for ExecutionRouter."""

import pytest
import logging
from unittest.mock import Mock, AsyncMock

from xbot.execution.router import ExecutionRouter


class TestExecutionRouter:
    """Test ExecutionRouter basic functionality."""

    def test_router_initialization(self):
        """Test router initializes with all services."""
        router = ExecutionRouter()

        assert router.services is not None
        assert 'symbol' in router.services
        assert 'order' in router.services
        assert 'pos' in router.services
        assert 'risk' in router.services

        assert router._connectors == {}

    def test_router_with_custom_logger(self):
        """Test router initialization with custom logger."""
        logger = logging.getLogger("test")
        router = ExecutionRouter(logger=logger)

        assert router.log == logger

    def test_register_connector(self):
        """Test connector registration."""
        router = ExecutionRouter()
        mock_connector = Mock()

        # Mock the register_connector method on services
        for service in router.services.values():
            service.register_connector = Mock()

        router.register_connector("backpack", mock_connector, coi_limit=1000)

        assert router._connectors["backpack"] == mock_connector

        # Verify all services were called
        for service in router.services.values():
            service.register_connector.assert_called_once_with(
                "backpack", mock_connector, coi_limit=1000, api_key_index=None
            )

    def test_register_symbol(self):
        """Test symbol registration."""
        router = ExecutionRouter()
        router.services['symbol'].register_symbol = Mock()

        router.register_symbol("BTC", backpack="BTC_USDC_PERP", lighter="BTC")

        router.services['symbol'].register_symbol.assert_called_once_with(
            "BTC", backpack="BTC_USDC_PERP", lighter="BTC"
        )

    @pytest.mark.asyncio
    async def test_limit_order_delegation(self):
        """Test limit order is delegated to order service."""
        router = ExecutionRouter()
        router.services['order'].limit_order = AsyncMock(return_value="order_result")

        result = await router.limit_order(
            venue="backpack",
            symbol="BTC",
            size_i=1000000,
            price_i=4500000,
            is_ask=False
        )

        assert result == "order_result"
        router.services['order'].limit_order.assert_called_once_with(
            venue="backpack",
            symbol="BTC",
            size_i=1000000,
            price_i=4500000,
            is_ask=False,
            tracking=False
        )

    @pytest.mark.asyncio
    async def test_market_order_delegation(self):
        """Test market order is delegated to order service."""
        router = ExecutionRouter()
        router.services['order'].market_order = AsyncMock(return_value="market_result")

        result = await router.market_order(
            venue="lighter",
            symbol="ETH",
            size_i=500000,
            is_ask=True
        )

        assert result == "market_result"
        router.services['order'].market_order.assert_called_once_with(
            venue="lighter",
            symbol="ETH",
            size_i=500000,
            is_ask=True
        )

    @pytest.mark.asyncio
    async def test_cancel_delegation(self):
        """Test cancel is delegated to order service."""
        router = ExecutionRouter()
        router.services['order'].cancel = AsyncMock()

        await router.cancel("backpack", "BTC", 12345)

        router.services['order'].cancel.assert_called_once_with("backpack", "BTC", 12345)

    @pytest.mark.asyncio
    async def test_position_delegation(self):
        """Test position query is delegated to position service."""
        router = ExecutionRouter()
        router.services['pos'].get_positions = AsyncMock(return_value="position_data")

        result = await router.position("backpack")

        assert result == "position_data"
        router.services['pos'].get_positions.assert_called_once_with("backpack")

    def test_get_connector(self):
        """Test connector retrieval."""
        router = ExecutionRouter()
        mock_connector = Mock()
        router._connectors["backpack"] = mock_connector

        assert router.get_connector("backpack") == mock_connector
        assert router.get_connector("Backpack") == mock_connector  # Case insensitive
        assert router.get_connector("nonexistent") is None