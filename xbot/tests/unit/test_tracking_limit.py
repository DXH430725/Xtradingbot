"""Unit tests for tracking limit order implementation."""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock

from xbot.execution.tracking_limit import (
    place_tracking_limit_order,
    TrackingLimitTimeoutError,
    SimpleTrackingResult,
    _select_price,
    _price_to_int
)


class TestTrackingLimit:
    """Test tracking limit order functionality."""

    @pytest.mark.asyncio
    async def test_simple_tracking_result(self):
        """Test SimpleTrackingResult basic functionality."""
        result = SimpleTrackingResult(12345)

        assert result.client_order_id == 12345
        assert result.state == "NEW"
        assert result.filled_base_i == 0

        # Test wait_final with timeout
        start_time = asyncio.get_event_loop().time()
        await result.wait_final(timeout=0.1)
        elapsed = asyncio.get_event_loop().time() - start_time
        assert 0.09 <= elapsed <= 0.2  # Allow some tolerance

        # Test marking final
        result.mark_final("FILLED")
        assert result.state == "FILLED"

        # Should return immediately now
        start_time = asyncio.get_event_loop().time()
        await result.wait_final(timeout=1.0)
        elapsed = asyncio.get_event_loop().time() - start_time
        assert elapsed < 0.1

    @pytest.mark.asyncio
    async def test_place_tracking_limit_invalid_size(self):
        """Test that invalid size raises ValueError."""
        mock_connector = Mock()

        with pytest.raises(ValueError, match="size_i must be positive"):
            await place_tracking_limit_order(
                connector=mock_connector,
                symbol="BTC",
                size_i=0,
                price_i=50000,
                is_ask=False
            )

        with pytest.raises(ValueError, match="size_i must be positive"):
            await place_tracking_limit_order(
                connector=mock_connector,
                symbol="BTC",
                size_i=-100,
                price_i=50000,
                is_ask=False
            )

    @pytest.mark.asyncio
    async def test_place_tracking_limit_missing_decimals(self):
        """Test handling of missing get_price_size_decimals."""
        mock_connector = Mock()
        # Don't provide get_price_size_decimals

        mock_connector.submit_limit_order = AsyncMock()
        tracking_result = SimpleTrackingResult(123)
        tracking_result.mark_final("FILLED")
        mock_connector.submit_limit_order.return_value = tracking_result

        result = await place_tracking_limit_order(
            connector=mock_connector,
            symbol="BTC",
            size_i=1000000,
            price_i=5000000,
            is_ask=False,
            timeout_secs=1.0
        )

        assert result.state == "FILLED"

    @pytest.mark.asyncio
    async def test_place_tracking_limit_immediate_fill(self):
        """Test tracking limit that fills immediately."""
        mock_connector = Mock()
        mock_connector.get_price_size_decimals = AsyncMock(return_value=(2, 6))
        mock_connector.submit_limit_order = AsyncMock()

        # Create a result that's immediately filled
        tracking_result = SimpleTrackingResult(123)
        tracking_result.mark_final("FILLED")
        tracking_result.filled_base_i = 1000000
        mock_connector.submit_limit_order.return_value = tracking_result

        result = await place_tracking_limit_order(
            connector=mock_connector,
            symbol="BTC",
            size_i=1000000,
            price_i=5000000,
            is_ask=False,
            timeout_secs=30.0
        )

        assert result.state == "FILLED"
        assert result.filled_base_i == 1000000
        assert result.attempts == 1

    @pytest.mark.asyncio
    async def test_place_tracking_limit_timeout(self):
        """Test tracking limit timeout."""
        mock_connector = Mock()
        mock_connector.get_price_size_decimals = AsyncMock(return_value=(2, 6))
        mock_connector.submit_limit_order = AsyncMock()

        # Create a result that never fills
        tracking_result = SimpleTrackingResult(123)
        # Don't mark as final, so it will keep trying

        async def wait_final_mock(timeout=None):
            if timeout:
                await asyncio.sleep(timeout)
            return "OPEN"

        tracking_result.wait_final = wait_final_mock
        mock_connector.submit_limit_order.return_value = tracking_result

        # Mock cancel method
        mock_connector.cancel_by_client_id = AsyncMock()

        with pytest.raises(TrackingLimitTimeoutError):
            await place_tracking_limit_order(
                connector=mock_connector,
                symbol="BTC",
                size_i=1000000,
                price_i=5000000,
                is_ask=False,
                timeout_secs=0.2,  # Very short timeout
                interval_secs=0.1
            )

    @pytest.mark.asyncio
    async def test_place_tracking_limit_max_attempts(self):
        """Test tracking limit with max attempts."""
        mock_connector = Mock()
        mock_connector.get_price_size_decimals = AsyncMock(return_value=(2, 6))
        mock_connector.submit_limit_order = AsyncMock()

        # Create a result that never fills
        tracking_result = SimpleTrackingResult(123)

        async def wait_final_mock(timeout=None):
            if timeout:
                await asyncio.sleep(min(timeout, 0.05))  # Quick return
            return "OPEN"

        tracking_result.wait_final = wait_final_mock
        mock_connector.submit_limit_order.return_value = tracking_result

        # Mock cancel method
        mock_connector.cancel_by_client_id = AsyncMock()

        result = await place_tracking_limit_order(
            connector=mock_connector,
            symbol="BTC",
            size_i=1000000,
            price_i=5000000,
            is_ask=False,
            timeout_secs=10.0,
            interval_secs=0.1,
            max_attempts=2
        )

        assert result.attempts == 2
        assert result.state == "MAX_ATTEMPTS"

    @pytest.mark.asyncio
    async def test_place_tracking_limit_with_coi_provider(self):
        """Test tracking limit with custom COI provider."""
        mock_connector = Mock()
        mock_connector.get_price_size_decimals = AsyncMock(return_value=(2, 6))
        mock_connector.submit_limit_order = AsyncMock()

        tracking_result = SimpleTrackingResult(123)
        tracking_result.mark_final("FILLED")
        mock_connector.submit_limit_order.return_value = tracking_result

        coi_counter = [1000]

        def coi_provider():
            coi_counter[0] += 1
            return coi_counter[0]

        result = await place_tracking_limit_order(
            connector=mock_connector,
            symbol="BTC",
            size_i=1000000,
            price_i=5000000,
            is_ask=False,
            coi_provider=coi_provider
        )

        assert result.client_order_id == 1001  # First call increments to 1001
        mock_connector.submit_limit_order.assert_called_once()

    def test_price_to_int(self):
        """Test price conversion to integer."""
        assert _price_to_int("50.25", 100) == 5025
        assert _price_to_int(50.25, 100) == 5025
        assert _price_to_int("invalid", 100) == 0
        assert _price_to_int(None, 100) == 0

    def test_select_price(self):
        """Test price selection logic."""
        # Buy order (is_ask=False)
        price = _select_price(bid_i=5000, ask_i=5001, scale=100, offset_ticks=0, is_ask=False)
        assert price == 5000  # Should use bid

        price = _select_price(bid_i=5000, ask_i=5001, scale=100, offset_ticks=1, is_ask=False)
        assert price == 4999  # Should be bid - offset

        # Sell order (is_ask=True)
        price = _select_price(bid_i=5000, ask_i=5001, scale=100, offset_ticks=0, is_ask=True)
        assert price == 5001  # Should use ask

        price = _select_price(bid_i=5000, ask_i=5001, scale=100, offset_ticks=1, is_ask=True)
        assert price == 5002  # Should be ask + offset

        # No book data
        price = _select_price(bid_i=None, ask_i=None, scale=100, offset_ticks=0, is_ask=False)
        assert price == 2500000  # Should use fallback (25_000 * scale)

    @pytest.mark.asyncio
    async def test_tracking_limit_with_top_of_book(self):
        """Test tracking limit with dynamic price adjustment."""
        mock_connector = Mock()
        mock_connector.get_price_size_decimals = AsyncMock(return_value=(2, 6))
        mock_connector.get_top_of_book = AsyncMock(return_value=(4999, 5001, 100))
        mock_connector.submit_limit_order = AsyncMock()

        tracking_result = SimpleTrackingResult(123)
        tracking_result.mark_final("FILLED")
        mock_connector.submit_limit_order.return_value = tracking_result

        result = await place_tracking_limit_order(
            connector=mock_connector,
            symbol="BTC",
            size_i=1000000,
            price_i=5000000,  # This should be adjusted based on book
            is_ask=False,
            price_offset_ticks=2
        )

        # Should have called submit_limit_order with adjusted price
        call_args = mock_connector.submit_limit_order.call_args
        submitted_price = call_args.kwargs['price_i']
        # For buy with offset 2, should be bid - 2 = 4999 - 2 = 4997
        assert submitted_price == 4997

    @pytest.mark.asyncio
    async def test_interval_timing_accuracy(self):
        """Test that interval timing is reasonably accurate."""
        mock_connector = Mock()
        mock_connector.get_price_size_decimals = AsyncMock(return_value=(2, 6))
        mock_connector.submit_limit_order = AsyncMock()

        tracking_result = SimpleTrackingResult(123)

        attempt_times = []

        async def wait_final_mock(timeout=None):
            attempt_times.append(asyncio.get_event_loop().time())
            if len(attempt_times) >= 3:  # Fill after 3 attempts
                tracking_result.mark_final("FILLED")
                return "FILLED"
            if timeout:
                await asyncio.sleep(timeout)
            return "OPEN"

        tracking_result.wait_final = wait_final_mock
        mock_connector.submit_limit_order.return_value = tracking_result
        mock_connector.cancel_by_client_id = AsyncMock()

        start_time = asyncio.get_event_loop().time()
        result = await place_tracking_limit_order(
            connector=mock_connector,
            symbol="BTC",
            size_i=1000000,
            price_i=5000000,
            is_ask=False,
            interval_secs=0.2,
            timeout_secs=10.0
        )

        total_time = asyncio.get_event_loop().time() - start_time

        # Should have taken approximately 2 * interval_secs (2 waits before fill)
        expected_time = 2 * 0.2  # 0.4 seconds
        assert result.state == "FILLED"
        assert abs(total_time - expected_time) < 0.3  # Allow 300ms tolerance