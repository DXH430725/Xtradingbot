"""Unit tests for unified order model."""

import pytest
import asyncio
import time
from unittest.mock import patch

from xbot.execution.order_model import Order, OrderEvent, OrderState


class TestOrderEvent:
    """Test OrderEvent functionality."""

    def test_order_event_creation(self):
        """Test basic OrderEvent creation."""
        event = OrderEvent(
            ts=1234567890.5,
            src="ws",
            type="ack",
            info={"order_id": "12345"}
        )

        assert event.ts == 1234567890.5
        assert event.src == "ws"
        assert event.type == "ack"
        assert event.info["order_id"] == "12345"

    def test_order_event_auto_timestamp(self):
        """Test OrderEvent with automatic timestamp."""
        with patch('time.time', return_value=1234567890.5):
            event = OrderEvent(
                ts=0,  # Should be replaced
                src="local",
                type="submit",
                info={}
            )

        assert event.ts == 1234567890.5


class TestOrder:
    """Test Order functionality."""

    def test_order_creation(self):
        """Test basic Order creation."""
        order = Order(
            coi=12345,
            venue="backpack",
            symbol="BTC",
            side="buy",
            size_i=1000000,
            price_i=5000000
        )

        assert order.coi == 12345
        assert order.venue == "backpack"
        assert order.symbol == "BTC"
        assert order.side == "buy"
        assert order.state == OrderState.NEW
        assert order.size_i == 1000000
        assert order.price_i == 5000000
        assert order.filled_base_i == 0
        assert order.is_limit is True
        assert order.history == []

    def test_order_state_queries(self):
        """Test order state query methods."""
        order = Order(coi=123, venue="test", symbol="BTC", side="buy")

        # Initial state
        assert order.is_active() is True
        assert order.is_final() is False
        assert order.is_filled() is False
        assert order.is_cancelled() is False
        assert order.is_failed() is False

        # Test filled state
        order.state = OrderState.FILLED
        assert order.is_active() is False
        assert order.is_final() is True
        assert order.is_filled() is True

        # Test cancelled state
        order.state = OrderState.CANCELLED
        assert order.is_final() is True
        assert order.is_cancelled() is True

        # Test failed state
        order.state = OrderState.FAILED
        assert order.is_final() is True
        assert order.is_failed() is True

    def test_fill_percentage(self):
        """Test fill percentage calculation."""
        order = Order(coi=123, venue="test", symbol="BTC", side="buy", size_i=1000000)

        assert order.get_fill_percentage() == 0.0

        order.filled_base_i = 500000
        assert order.get_fill_percentage() == 0.5

        order.filled_base_i = 1000000
        assert order.get_fill_percentage() == 1.0

        # Test overfill protection
        order.filled_base_i = 1200000
        assert order.get_fill_percentage() == 1.0

    def test_remaining_size(self):
        """Test remaining size calculation."""
        order = Order(coi=123, venue="test", symbol="BTC", side="buy", size_i=1000000)

        assert order.get_remaining_size_i() == 1000000

        order.filled_base_i = 300000
        assert order.get_remaining_size_i() == 700000

        order.filled_base_i = 1000000
        assert order.get_remaining_size_i() == 0

    def test_append_event_ack(self):
        """Test appending acknowledgment event."""
        order = Order(coi=123, venue="test", symbol="BTC", side="buy")
        order.state = OrderState.SUBMITTING

        event = OrderEvent(
            ts=time.time(),
            src="ws",
            type="ack",
            info={"exchange_order_id": "ex123"}
        )

        order.append_event(event)

        assert len(order.history) == 1
        assert order.history[0] == event
        assert order.state == OrderState.OPEN
        assert order.last_event_ts == event.ts

    def test_append_event_fill(self):
        """Test appending fill events."""
        order = Order(coi=123, venue="test", symbol="BTC", side="buy", size_i=1000000)
        order.state = OrderState.OPEN

        # Partial fill
        fill_event = OrderEvent(
            ts=time.time(),
            src="ws",
            type="fill",
            info={"fill_amount_i": 300000}
        )

        order.append_event(fill_event)

        assert order.filled_base_i == 300000
        assert order.state == OrderState.PARTIALLY_FILLED

        # Complete fill
        fill_event2 = OrderEvent(
            ts=time.time(),
            src="ws",
            type="fill",
            info={"fill_amount_i": 700000}
        )

        order.append_event(fill_event2)

        assert order.filled_base_i == 1000000
        assert order.state == OrderState.FILLED
        assert order.is_final() is True

    def test_append_event_cancel(self):
        """Test appending cancel event."""
        order = Order(coi=123, venue="test", symbol="BTC", side="buy")
        order.state = OrderState.OPEN

        cancel_event = OrderEvent(
            ts=time.time(),
            src="ws",
            type="cancel_ack",
            info={}
        )

        order.append_event(cancel_event)

        assert order.state == OrderState.CANCELLED
        assert order.is_final() is True

    def test_update_state(self):
        """Test direct state updates."""
        order = Order(coi=123, venue="test", symbol="BTC", side="buy")

        order.update_state(OrderState.SUBMITTING)

        assert order.state == OrderState.SUBMITTING
        assert len(order.history) == 1
        assert order.history[0].type == "state_change"
        assert order.history[0].info["old_state"] == "NEW"
        assert order.history[0].info["new_state"] == "SUBMITTING"

    @pytest.mark.asyncio
    async def test_wait_final_immediate(self):
        """Test wait_final when already in final state."""
        order = Order(coi=123, venue="test", symbol="BTC", side="buy")
        order.state = OrderState.FILLED

        start_time = time.time()
        result = await order.wait_final(timeout=1.0)
        elapsed = time.time() - start_time

        assert result == OrderState.FILLED
        assert elapsed < 0.1  # Should return immediately

    @pytest.mark.asyncio
    async def test_wait_final_timeout(self):
        """Test wait_final with timeout."""
        order = Order(coi=123, venue="test", symbol="BTC", side="buy")

        start_time = time.time()
        result = await order.wait_final(timeout=0.1)
        elapsed = time.time() - start_time

        assert result == OrderState.NEW  # Still in original state
        assert 0.09 <= elapsed <= 0.2  # Should timeout after ~0.1s

    @pytest.mark.asyncio
    async def test_wait_final_event_completion(self):
        """Test wait_final with event triggering completion."""
        order = Order(coi=123, venue="test", symbol="BTC", side="buy", size_i=1000000)

        # Start waiting in background
        async def wait_task():
            return await order.wait_final(timeout=2.0)

        task = asyncio.create_task(wait_task())

        # Give the wait a moment to start
        await asyncio.sleep(0.01)

        # Trigger completion with fill event
        fill_event = OrderEvent(
            ts=time.time(),
            src="ws",
            type="fill",
            info={"fill_amount_i": 1000000}
        )
        order.append_event(fill_event)

        # Wait should complete quickly now
        result = await task
        assert result == OrderState.FILLED

    def test_timeline_summary(self):
        """Test timeline summary generation."""
        order = Order(coi=123, venue="test", symbol="BTC", side="buy")

        # Empty timeline
        summary = order.get_timeline_summary()
        assert summary["total_events"] == 0
        assert summary["duration_secs"] == 0.0

        # Add some events
        events = [
            OrderEvent(ts=1000.0, src="local", type="submit", info={}),
            OrderEvent(ts=1000.1, src="ws", type="ack", info={}),
            OrderEvent(ts=1000.5, src="ws", type="fill", info={"fill_amount_i": 500000}),
            OrderEvent(ts=1001.0, src="engine", type="cancel", info={})
        ]

        for event in events:
            order.append_event(event)

        summary = order.get_timeline_summary()
        assert summary["total_events"] == 4
        assert summary["first_event_ts"] == 1000.0
        assert summary["last_event_ts"] == 1001.0
        assert summary["duration_secs"] == 1.0
        assert summary["ws_events"] == 2
        assert summary["engine_events"] == 1
        assert summary["event_types"]["submit"] == 1
        assert summary["event_types"]["ack"] == 1

    def test_race_condition_detection(self):
        """Test race condition detection."""
        order = Order(coi=123, venue="test", symbol="BTC", side="buy")

        # Add out-of-order events
        events = [
            OrderEvent(ts=1000.0, src="ws", type="ack", info={}),
            OrderEvent(ts=999.5, src="ws", type="submit", info={}),  # Out of order
        ]

        for event in events:
            order.history.append(event)  # Direct append to avoid state updates

        issues = order.detect_race_conditions()
        assert len(issues) > 0
        assert "Out-of-order events" in issues[0]

    def test_to_dict_serialization(self):
        """Test order serialization to dictionary."""
        order = Order(
            coi=123,
            venue="backpack",
            symbol="BTC",
            side="buy",
            size_i=1000000,
            price_i=5000000
        )

        # Add an event
        event = OrderEvent(ts=1000.0, src="ws", type="ack", info={"test": "data"})
        order.append_event(event)

        data = order.to_dict()

        assert data["coi"] == 123
        assert data["venue"] == "backpack"
        assert data["symbol"] == "BTC"
        assert data["side"] == "buy"
        assert data["size_i"] == 1000000
        assert data["price_i"] == 5000000
        assert len(data["history"]) == 1
        assert data["history"][0]["src"] == "ws"
        assert data["history"][0]["type"] == "ack"

    def test_from_dict_deserialization(self):
        """Test order deserialization from dictionary."""
        data = {
            "coi": 123,
            "venue": "backpack",
            "symbol": "BTC",
            "side": "buy",
            "state": "OPEN",
            "size_i": 1000000,
            "price_i": 5000000,
            "filled_base_i": 0,
            "is_limit": True,
            "post_only": False,
            "reduce_only": 0,
            "created_at": 1000.0,
            "updated_at": 1001.0,
            "last_event_ts": 1001.0,
            "history": [
                {
                    "ts": 1000.5,
                    "src": "ws",
                    "type": "ack",
                    "info": {"test": "data"}
                }
            ]
        }

        order = Order.from_dict(data)

        assert order.coi == 123
        assert order.venue == "backpack"
        assert order.symbol == "BTC"
        assert order.side == "buy"
        assert order.state == OrderState.OPEN
        assert order.size_i == 1000000
        assert order.price_i == 5000000
        assert len(order.history) == 1
        assert order.history[0].src == "ws"
        assert order.history[0].type == "ack"
        assert order.history[0].info["test"] == "data"

    def test_order_string_representation(self):
        """Test order string representation."""
        order = Order(
            coi=123,
            venue="backpack",
            symbol="BTC",
            side="buy",
            size_i=1000000,
            price_i=5000000
        )

        str_repr = str(order)
        assert "Order(coi=123" in str_repr
        assert "backpack:BTC" in str_repr
        assert "buy 1000000@5000000" in str_repr
        assert "NEW)" in str_repr

        # Test with partial fill
        order.filled_base_i = 300000
        str_repr = str(order)
        assert "buy 300000/1000000@5000000" in str_repr

        # Test market order
        order.price_i = None
        str_repr = str(order)
        assert "buy 300000/1000000market" in str_repr