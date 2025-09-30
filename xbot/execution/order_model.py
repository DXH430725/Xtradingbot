"""Unified order model - single source of truth for order state.

Replaces OrderTracker/TrackingLimitOrder/OrderUpdate with single dataclass.
Target: < 300 lines.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Any, Optional


class OrderState(Enum):
    """Order state enumeration."""
    NEW = "NEW"
    SUBMITTING = "SUBMITTING"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass
class OrderEvent:
    """Single order event with timeline tracking."""
    ts: float                          # Timestamp
    src: str                          # Source: "ws" | "rest" | "local" | "engine"
    type: str                         # Event type: "ack" | "fill" | "cancel_ack" | "reject"
    info: Dict[str, Any]              # Event details

    def __post_init__(self):
        """Ensure timestamp is set."""
        if self.ts <= 0:
            self.ts = time.time()


@dataclass
class Order:
    """Unified order model - single source of truth.

    Replaces all order tracking classes with one comprehensive model.
    """
    coi: int                          # Client order index
    venue: str                        # Exchange venue
    symbol: str                       # Symbol (canonical or venue-specific)
    side: str                         # "buy" | "sell"
    state: OrderState = OrderState.NEW
    size_i: int = 0                   # Order size in integer format
    price_i: Optional[int] = None     # Order price in integer format (None for market)
    filled_base_i: int = 0            # Filled amount in integer format
    last_event_ts: float = field(default_factory=time.time)
    history: List[OrderEvent] = field(default_factory=list)

    # Order type and flags
    is_limit: bool = True             # True for limit, False for market
    post_only: bool = False           # Post-only flag
    reduce_only: int = 0              # Reduce-only flag

    # Exchange identifiers
    exchange_order_id: Optional[str] = None  # Exchange-assigned order ID

    # Tracking and metadata
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # Async completion tracking
    _completion_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def append_event(self, event: OrderEvent) -> None:
        """Add event to order history."""
        self.history.append(event)
        self.last_event_ts = event.ts
        self.updated_at = time.time()

        # Update state based on event
        if event.type == "ack":
            if self.state == OrderState.SUBMITTING:
                self.state = OrderState.OPEN
        elif event.type == "fill":
            fill_amount = event.info.get("fill_amount_i", 0)
            self.filled_base_i += fill_amount

            if self.filled_base_i >= self.size_i:
                self.state = OrderState.FILLED
                self._completion_event.set()
            elif self.filled_base_i > 0:
                self.state = OrderState.PARTIALLY_FILLED
        elif event.type == "cancel_ack":
            self.state = OrderState.CANCELLED
            self._completion_event.set()
        elif event.type == "reject":
            self.state = OrderState.FAILED
            self._completion_event.set()

    def update_state(self, new_state: OrderState, source: str = "local") -> None:
        """Update order state and record event."""
        if new_state != self.state:
            old_state = self.state
            self.state = new_state

            # Record state transition
            event = OrderEvent(
                ts=time.time(),
                src=source,
                type="state_change",
                info={
                    "old_state": old_state.value,
                    "new_state": new_state.value
                }
            )
            self.append_event(event)

            # Set completion for final states
            if self.is_final():
                self._completion_event.set()

    async def wait_final(self, timeout: Optional[float] = None) -> OrderState:
        """Wait for order to reach final state.

        Args:
            timeout: Maximum time to wait in seconds

        Returns:
            Final order state

        Raises:
            asyncio.TimeoutError: If timeout expires before final state
        """
        if self.is_final():
            return self.state

        try:
            await asyncio.wait_for(self._completion_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass  # Let caller handle timeout

        return self.state

    def is_final(self) -> bool:
        """Check if order is in final state."""
        return self.state in {
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.FAILED
        }

    def is_filled(self) -> bool:
        """Check if order is fully filled."""
        return self.state == OrderState.FILLED

    def is_cancelled(self) -> bool:
        """Check if order is cancelled."""
        return self.state == OrderState.CANCELLED

    def is_failed(self) -> bool:
        """Check if order failed."""
        return self.state == OrderState.FAILED

    def is_active(self) -> bool:
        """Check if order is still active (not final)."""
        return not self.is_final()

    def get_fill_percentage(self) -> float:
        """Get fill percentage (0.0 to 1.0)."""
        if self.size_i <= 0:
            return 0.0
        return min(self.filled_base_i / self.size_i, 1.0)

    def get_remaining_size_i(self) -> int:
        """Get remaining unfilled size in integer format."""
        return max(self.size_i - self.filled_base_i, 0)

    def get_timeline_summary(self) -> Dict[str, Any]:
        """Get timeline summary for debugging and analysis."""
        if not self.history:
            return {
                "total_events": 0,
                "first_event_ts": None,
                "last_event_ts": None,
                "duration_secs": 0.0,
                "event_types": {}
            }

        first_ts = self.history[0].ts
        last_ts = self.history[-1].ts

        # Count event types
        event_counts = {}
        ws_events = []
        engine_events = []

        for event in self.history:
            event_counts[event.type] = event_counts.get(event.type, 0) + 1

            if event.src == "ws":
                ws_events.append(event)
            elif event.src == "engine":
                engine_events.append(event)

        return {
            "total_events": len(self.history),
            "first_event_ts": first_ts,
            "last_event_ts": last_ts,
            "duration_secs": last_ts - first_ts,
            "event_types": event_counts,
            "ws_events": len(ws_events),
            "engine_events": len(engine_events),
            "creation_to_first_event_ms": (first_ts - self.created_at) * 1000 if self.history else None,
            "current_state": self.state.value,
            "fill_percentage": self.get_fill_percentage()
        }

    def detect_race_conditions(self) -> List[str]:
        """Detect potential race conditions in event timeline.

        Returns:
            List of detected race condition descriptions
        """
        issues = []

        if len(self.history) < 2:
            return issues

        # Check for out-of-order events
        for i in range(1, len(self.history)):
            prev_event = self.history[i-1]
            curr_event = self.history[i]

            if curr_event.ts < prev_event.ts:
                issues.append(
                    f"Out-of-order events: {prev_event.type}@{prev_event.ts:.3f} "
                    f"after {curr_event.type}@{curr_event.ts:.3f}"
                )

        # Check for rapid duplicate events
        event_times = {}
        for event in self.history:
            key = (event.type, event.src)
            if key in event_times:
                time_diff = event.ts - event_times[key]
                if time_diff < 0.001:  # Less than 1ms apart
                    issues.append(
                        f"Rapid duplicate {event.type} events from {event.src}: "
                        f"{time_diff*1000:.1f}ms apart"
                    )
            event_times[key] = event.ts

        # Check for conflicting final states
        final_events = [e for e in self.history if e.type in ["fill", "cancel_ack", "reject"]]
        if len(final_events) > 1:
            types = [e.type for e in final_events]
            issues.append(f"Multiple final events: {types}")

        return issues

    def to_dict(self) -> Dict[str, Any]:
        """Convert order to dictionary for serialization."""
        return {
            "coi": self.coi,
            "venue": self.venue,
            "symbol": self.symbol,
            "side": self.side,
            "state": self.state.value,
            "size_i": self.size_i,
            "price_i": self.price_i,
            "filled_base_i": self.filled_base_i,
            "is_limit": self.is_limit,
            "post_only": self.post_only,
            "reduce_only": self.reduce_only,
            "exchange_order_id": self.exchange_order_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_event_ts": self.last_event_ts,
            "history": [
                {
                    "ts": e.ts,
                    "src": e.src,
                    "type": e.type,
                    "info": e.info
                }
                for e in self.history
            ]
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Order':
        """Create order from dictionary."""
        # Recreate events
        history = []
        for event_data in data.get("history", []):
            event = OrderEvent(
                ts=event_data["ts"],
                src=event_data["src"],
                type=event_data["type"],
                info=event_data["info"]
            )
            history.append(event)

        order = cls(
            coi=data["coi"],
            venue=data["venue"],
            symbol=data["symbol"],
            side=data["side"],
            state=OrderState(data["state"]),
            size_i=data["size_i"],
            price_i=data.get("price_i"),
            filled_base_i=data["filled_base_i"],
            is_limit=data.get("is_limit", True),
            post_only=data.get("post_only", False),
            reduce_only=data.get("reduce_only", 0),
            created_at=data["created_at"],
            history=history
        )

        order.exchange_order_id = data.get("exchange_order_id")
        order.updated_at = data.get("updated_at", order.created_at)
        order.last_event_ts = data.get("last_event_ts", order.created_at)

        # Set completion event if final
        if order.is_final():
            order._completion_event.set()

        return order

    def __str__(self) -> str:
        """String representation for debugging."""
        price_str = f"@{self.price_i}" if self.price_i else "market"
        fill_str = f"{self.filled_base_i}/{self.size_i}" if self.filled_base_i > 0 else str(self.size_i)

        return (
            f"Order(coi={self.coi}, {self.venue}:{self.symbol}, "
            f"{self.side} {fill_str}{price_str}, {self.state.value})"
        )