"""Unit tests for execution services."""

import pytest
import time
from unittest.mock import Mock, AsyncMock

from xbot.execution.services.symbol_service import SymbolService
from xbot.execution.services.order_service import OrderService
from xbot.execution.services.position_service import PositionService
from xbot.execution.services.risk_service import RiskService, RiskLimits


class TestSymbolService:
    """Test SymbolService functionality."""

    def test_initialization(self):
        """Test service initializes correctly."""
        service = SymbolService()
        assert service._canonical_to_venue == {}
        assert service._venue_to_canonical == {}
        assert service._connectors == {}

    def test_register_symbol(self):
        """Test symbol registration."""
        service = SymbolService()
        service.register_symbol("BTC", backpack="BTC_USDC_PERP", lighter="BTC")

        assert service._canonical_to_venue["BTC"]["backpack"] == "BTC_USDC_PERP"
        assert service._canonical_to_venue["BTC"]["lighter"] == "BTC"
        assert service._venue_to_canonical["backpack"]["BTC_USDC_PERP"] == "BTC"
        assert service._venue_to_canonical["lighter"]["BTC"] == "BTC"

    def test_map_symbol(self):
        """Test symbol mapping."""
        service = SymbolService()
        service.register_symbol("BTC", backpack="BTC_USDC_PERP")

        assert service.map_symbol("BTC", "backpack") == "BTC_USDC_PERP"
        assert service.map_symbol("BTC", "lighter") == "BTC"  # No mapping, returns canonical
        assert service.map_symbol("ETH", "backpack") == "ETH"  # No mapping, returns canonical

    def test_register_connector(self):
        """Test connector registration."""
        service = SymbolService()
        mock_connector = Mock()

        service.register_connector("backpack", mock_connector)
        assert service._connectors["backpack"] == mock_connector


class TestOrderService:
    """Test OrderService functionality."""

    def test_initialization(self):
        """Test service initializes correctly."""
        service = OrderService()
        assert service._connectors == {}
        assert service._active_orders == {}
        assert service._coi_counters == {}

    def test_register_connector(self):
        """Test connector registration."""
        service = OrderService()
        mock_connector = Mock()

        service.register_connector("backpack", mock_connector, coi_limit=1000)

        assert service._connectors["backpack"] == mock_connector
        assert service._coi_limits["backpack"] == 1000
        assert "backpack" in service._coi_counters

    def test_next_coi_generation(self):
        """Test COI generation."""
        service = OrderService()
        service.register_connector("backpack", Mock())

        coi1 = service._next_coi("backpack")
        coi2 = service._next_coi("backpack")

        assert isinstance(coi1, int)
        assert isinstance(coi2, int)
        assert coi2 == coi1 + 1

    @pytest.mark.asyncio
    async def test_get_order(self):
        """Test order retrieval."""
        service = OrderService()

        # Add a mock order
        order = {"coi": 123, "venue": "backpack", "state": "OPEN"}
        service._active_orders[("backpack", 123)] = order

        result = await service.get_order("backpack", 123)
        assert result == order

        result = await service.get_order("backpack", 999)
        assert result is None


class TestPositionService:
    """Test PositionService functionality."""

    def test_initialization(self):
        """Test service initializes correctly."""
        service = PositionService()
        assert service._connectors == {}
        assert service._position_cache == {}
        assert service._collateral_cache == {}

    def test_register_connector(self):
        """Test connector registration."""
        service = PositionService()
        mock_connector = Mock()

        service.register_connector("backpack", mock_connector)
        assert service._connectors["backpack"] == mock_connector

    @pytest.mark.asyncio
    async def test_get_collateral_no_connector(self):
        """Test collateral query with no connector."""
        service = PositionService()
        result = await service.get_collateral("nonexistent")
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_get_net_exposure_no_connectors(self):
        """Test net exposure calculation with no connectors."""
        service = PositionService()
        result = await service.get_net_exposure("BTC")

        assert result["symbol"] == "BTC"
        assert result["net_exposure"] == 0.0
        assert result["venue_exposures"] == {}


class TestRiskService:
    """Test RiskService functionality."""

    def test_initialization(self):
        """Test service initializes correctly."""
        service = RiskService()
        assert service._connectors == {}
        assert service._risk_limits == {}
        assert service._circuit_breaker_active == {}

    def test_register_connector(self):
        """Test connector registration."""
        service = RiskService()
        mock_connector = Mock()

        service.register_connector("backpack", mock_connector)

        assert service._connectors["backpack"] == mock_connector
        assert isinstance(service._risk_limits["backpack"], RiskLimits)
        assert service._circuit_breaker_active["backpack"] is False

    def test_set_risk_limits(self):
        """Test risk limits configuration."""
        service = RiskService()
        custom_limits = RiskLimits(max_order_size=500000.0)

        service.set_risk_limits("backpack", custom_limits)
        assert service._risk_limits["backpack"] == custom_limits

    def test_circuit_breaker_reset(self):
        """Test circuit breaker reset."""
        service = RiskService()
        service._circuit_breaker_active["backpack"] = True

        service.reset_circuit_breaker("backpack")
        assert service._circuit_breaker_active["backpack"] is False

    @pytest.mark.asyncio
    async def test_check_order_risk_circuit_breaker_active(self):
        """Test risk check fails when circuit breaker is active."""
        service = RiskService()
        service._circuit_breaker_active["backpack"] = True

        result = await service.check_order_risk("backpack", "BTC", 1000000)
        assert result is False

    def test_record_error(self):
        """Test error recording."""
        service = RiskService()
        service.register_connector("backpack", Mock())

        initial_count = len(service._error_events)
        service._record_error("backpack", "test_error", "Test message")

        assert len(service._error_events) == initial_count + 1
        assert service._error_events[-1].venue == "backpack"
        assert service._error_events[-1].error_type == "test_error"

    def test_cleanup_old_errors(self):
        """Test error cleanup."""
        service = RiskService()

        # Add an old error
        old_time = time.time() - 7200  # 2 hours ago
        service._error_events.append(
            type('ErrorEvent', (), {
                'timestamp': old_time,
                'venue': 'backpack',
                'error_type': 'old_error',
                'message': 'Old error'
            })()
        )

        removed = service.cleanup_old_errors(max_age_seconds=3600)  # 1 hour
        assert removed == 1
        assert len(service._error_events) == 0