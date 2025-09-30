"""End-to-end smoke test validation."""

import pytest
import asyncio
import logging
from unittest.mock import AsyncMock

from xbot.core.trading_core import TradingCore
from xbot.execution import ExecutionRouter
from xbot.connector import MockConnector, MockConfig
from xbot.strategy.smoke_test import SmokeTestStrategy, SmokeTestConfig


class TestSmokeTestE2E:
    """End-to-end tests for smoke test strategy."""

    @pytest.mark.asyncio
    async def test_smoke_test_tracking_limit_success(self):
        """Test successful tracking limit smoke test."""
        # Create mock connector with auto-fill
        config = MockConfig(
            venue_name="test_venue",
            latency_ms=10.0,
            auto_fill_rate=1.0  # Always fill orders
        )
        connector = MockConnector(config)

        # Create trading core
        core = TradingCore(tick_interval_ms=100.0, debug=True)
        core.add_connector("test_venue", connector)

        # Create smoke test strategy
        smoke_config = SmokeTestConfig(
            venue="test_venue",
            symbol="BTC",
            mode="tracking_limit",
            side="buy",
            size_multiplier=1.0,
            timeout_secs=10.0,
            interval_secs=1.0,
            max_attempts=2
        )

        strategy = SmokeTestStrategy(smoke_config, core.router)
        core.set_strategy(strategy)

        # Run the test
        await core.start()

        try:
            # Wait for test completion (should be quick with auto-fill)
            await asyncio.wait_for(core.wait_until_stopped(), timeout=15.0)

            # Check results
            assert strategy.overall_success is True
            assert strategy.result.attempts > 0
            assert strategy.result.order_count > 0
            assert len(strategy.result.events) > 0

        finally:
            await core.shutdown()

    @pytest.mark.asyncio
    async def test_smoke_test_limit_once(self):
        """Test limit once smoke test."""
        config = MockConfig(venue_name="test_venue", latency_ms=5.0)
        connector = MockConnector(config)

        core = TradingCore(tick_interval_ms=100.0)
        core.add_connector("test_venue", connector)

        smoke_config = SmokeTestConfig(
            venue="test_venue",
            symbol="ETH",
            mode="limit_once",
            side="sell",
            timeout_secs=5.0
        )

        strategy = SmokeTestStrategy(smoke_config, core.router)
        core.set_strategy(strategy)

        await core.start()

        try:
            await asyncio.wait_for(core.wait_until_stopped(), timeout=10.0)

            # Should succeed even if order doesn't fill (just places and cancels)
            assert strategy.overall_success is True
            assert strategy.result.order_count == 1

        finally:
            await core.shutdown()

    @pytest.mark.asyncio
    async def test_smoke_test_market_order(self):
        """Test market order smoke test."""
        config = MockConfig(venue_name="test_venue", latency_ms=5.0)
        connector = MockConnector(config)

        core = TradingCore(tick_interval_ms=100.0)
        core.add_connector("test_venue", connector)

        smoke_config = SmokeTestConfig(
            venue="test_venue",
            symbol="SOL",
            mode="market",
            side="buy",
            timeout_secs=5.0
        )

        strategy = SmokeTestStrategy(smoke_config, core.router)
        core.set_strategy(strategy)

        await core.start()

        try:
            await asyncio.wait_for(core.wait_until_stopped(), timeout=10.0)

            # Market orders should always succeed with mock connector
            assert strategy.overall_success is True
            assert strategy.result.order_count == 1

        finally:
            await core.shutdown()

    @pytest.mark.asyncio
    async def test_smoke_test_connector_error(self):
        """Test smoke test handling of connector errors."""
        # Create connector with high error rate
        config = MockConfig(
            venue_name="error_venue",
            latency_ms=5.0,
            error_rate=1.0  # Always error
        )
        connector = MockConnector(config)

        core = TradingCore(tick_interval_ms=100.0)
        core.add_connector("error_venue", connector)

        smoke_config = SmokeTestConfig(
            venue="error_venue",
            symbol="BTC",
            mode="limit_once",
            timeout_secs=5.0
        )

        strategy = SmokeTestStrategy(smoke_config, core.router)
        core.set_strategy(strategy)

        await core.start()

        try:
            await asyncio.wait_for(core.wait_until_stopped(), timeout=10.0)

            # Should fail due to connector errors
            assert strategy.overall_success is False
            assert len(strategy.result.errors) > 0

        finally:
            await core.shutdown()

    @pytest.mark.asyncio
    async def test_smoke_test_unknown_venue(self):
        """Test smoke test with unknown venue."""
        core = TradingCore(tick_interval_ms=100.0)
        # Don't add any connectors

        smoke_config = SmokeTestConfig(
            venue="unknown_venue",
            symbol="BTC",
            mode="tracking_limit",
            timeout_secs=5.0
        )

        strategy = SmokeTestStrategy(smoke_config, core.router)
        core.set_strategy(strategy)

        await core.start()

        try:
            await asyncio.wait_for(core.wait_until_stopped(), timeout=10.0)

            # Should fail due to missing connector
            assert strategy.overall_success is False
            assert any("No connector" in error for error in strategy.result.errors)

        finally:
            await core.shutdown()

    @pytest.mark.asyncio
    async def test_smoke_test_price_sampling(self):
        """Test price sampling functionality."""
        config = MockConfig(venue_name="test_venue", latency_ms=1.0)
        connector = MockConnector(config)

        core = TradingCore(tick_interval_ms=100.0)
        core.add_connector("test_venue", connector)

        smoke_config = SmokeTestConfig(
            venue="test_venue",
            symbol="BTC",
            mode="limit_once",
            timeout_secs=3.0
        )

        strategy = SmokeTestStrategy(smoke_config, core.router)
        core.set_strategy(strategy)

        await core.start()

        try:
            await asyncio.wait_for(core.wait_until_stopped(), timeout=8.0)

            # Should have captured price samples
            assert strategy.result.price_samples > 0
            assert strategy.result.price_stats is not None
            assert "avg_spread_bps" in strategy.result.price_stats

        finally:
            await core.shutdown()

    @pytest.mark.asyncio
    async def test_smoke_test_timeline_analysis(self):
        """Test timeline analysis features."""
        config = MockConfig(
            venue_name="test_venue",
            latency_ms=10.0,
            auto_fill_rate=1.0
        )
        connector = MockConnector(config)

        core = TradingCore(tick_interval_ms=100.0)
        core.add_connector("test_venue", connector)

        smoke_config = SmokeTestConfig(
            venue="test_venue",
            symbol="BTC",
            mode="tracking_limit",
            timeout_secs=5.0,
            interval_secs=0.5,
            max_attempts=1
        )

        strategy = SmokeTestStrategy(smoke_config, core.router)
        core.set_strategy(strategy)

        await core.start()

        try:
            await asyncio.wait_for(core.wait_until_stopped(), timeout=10.0)

            assert strategy.overall_success is True

            # Should have timeline summary
            assert strategy.result.timeline_summary is not None
            timeline = strategy.result.timeline_summary
            assert timeline["total_events"] > 0
            assert timeline["duration_secs"] >= 0

        finally:
            await core.shutdown()

    @pytest.mark.asyncio
    async def test_smoke_test_configuration_validation(self):
        """Test smoke test configuration edge cases."""
        config = MockConfig(venue_name="test_venue")
        connector = MockConnector(config)

        core = TradingCore(tick_interval_ms=100.0)
        core.add_connector("test_venue", connector)

        # Test invalid mode
        smoke_config = SmokeTestConfig(
            venue="test_venue",
            symbol="BTC",
            mode="invalid_mode",
            timeout_secs=5.0
        )

        strategy = SmokeTestStrategy(smoke_config, core.router)
        core.set_strategy(strategy)

        await core.start()

        try:
            await asyncio.wait_for(core.wait_until_stopped(), timeout=10.0)

            # Should fail due to invalid mode
            assert strategy.overall_success is False
            assert any("Unknown test mode" in error for error in strategy.result.errors)

        finally:
            await core.shutdown()

    def test_smoke_test_result_properties(self):
        """Test SmokeTestResult data structure."""
        from xbot.strategy.smoke_test import SmokeTestResult

        result = SmokeTestResult(
            success=True,
            attempts=2,
            duration_secs=5.5
        )

        assert result.success is True
        assert result.attempts == 2
        assert result.duration_secs == 5.5
        assert result.events == []
        assert result.errors == []
        assert result.warnings == []
        assert result.price_samples == 0
        assert result.order_count == 0

        # Test adding data
        result.events.append("Test event")
        result.errors.append("Test error")
        result.warnings.append("Test warning")

        assert len(result.events) == 1
        assert len(result.errors) == 1
        assert len(result.warnings) == 1