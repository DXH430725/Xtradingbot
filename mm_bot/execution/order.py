"""Unified Order aggregate replacing OrderTracker/TrackingLimitOrder/OrderUpdate."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Literal, Optional


class OrderState(str, Enum):
    """Order state enumeration."""
    NEW = "new"
    SUBMITTING = "submitting"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class OrderEvent:
    """Single order event with timing and reconciliation data."""
    state: OrderState
    filled_base_i: Optional[int] = None
    remaining_base_i: Optional[int] = None
    engine_ts: Optional[float] = None  # Exchange engine timestamp
    cancel_ack_ts: Optional[float] = None  # Cancel acknowledgment timestamp
    ws_seq: Optional[int] = None  # WebSocket sequence number
    timestamp: float = field(default_factory=time.time)
    info: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Order:
    """Unified order aggregate - single source of truth for order state.

    Replaces OrderTracker/TrackingLimitOrder/OrderUpdate with single dataclass.
    """
    id: str
    venue: str
    symbol: str
    side: Literal["buy", "sell"]
    state: OrderState = OrderState.NEW
    filled_base_i: int = 0
    last_event_ts: float = field(default_factory=time.time)
    history: List[OrderEvent] = field(default_factory=list)

    # Optional fields for specific order types
    price_i: Optional[int] = None
    size_i: Optional[int] = None
    client_order_id: Optional[int] = None
    exchange_order_id: Optional[str] = None

    # Completion tracking
    _completion_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _final_states = {OrderState.FILLED, OrderState.CANCELLED, OrderState.FAILED}

    def append_event(self, event: OrderEvent) -> None:
        """Add event to history and update current state."""
        self.history.append(event)
        self.state = event.state
        self.last_event_ts = event.timestamp

        if event.filled_base_i is not None:
            self.filled_base_i = event.filled_base_i

        # Signal completion if final state reached
        if self.state in self._final_states:
            if not self._completion_event.is_set():
                self._completion_event.set()

    async def wait_final(self, timeout: Optional[float] = None) -> OrderState:
        """Wait for order to reach final state (FILLED/CANCELLED/FAILED).

        Args:
            timeout: Maximum wait time in seconds, None for no timeout

        Returns:
            Final OrderState

        Raises:
            asyncio.TimeoutError: If timeout exceeded before reaching final state
        """
        if self.state in self._final_states:
            return self.state

        try:
            await asyncio.wait_for(self._completion_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            # Don't re-raise, just return current state
            pass

        return self.state

    @property
    def is_final(self) -> bool:
        """Check if order is in final state."""
        return self.state in self._final_states

    @property
    def is_filled(self) -> bool:
        """Check if order is fully filled."""
        return self.state == OrderState.FILLED

    @property
    def is_cancelled(self) -> bool:
        """Check if order is cancelled."""
        return self.state == OrderState.CANCELLED

    @property
    def is_failed(self) -> bool:
        """Check if order failed."""
        return self.state == OrderState.FAILED

    def get_timeline_summary(self) -> Dict[str, Any]:
        """Get timeline summary for reconciliation analysis."""
        if not self.history:
            return {}

        first_event = self.history[0]
        last_event = self.history[-1]

        summary = {
            "order_id": self.id,
            "venue": self.venue,
            "symbol": self.symbol,
            "side": self.side,
            "initial_state": first_event.state.value,
            "final_state": last_event.state.value,
            "event_count": len(self.history),
            "duration_ms": (last_event.timestamp - first_event.timestamp) * 1000,
        }

        # Add timing fields for reconciliation
        engine_timestamps = [e.engine_ts for e in self.history if e.engine_ts]
        cancel_ack_timestamps = [e.cancel_ack_ts for e in self.history if e.cancel_ack_ts]
        ws_sequences = [e.ws_seq for e in self.history if e.ws_seq]

        if engine_timestamps:
            summary["engine_ts_first"] = min(engine_timestamps)
            summary["engine_ts_last"] = max(engine_timestamps)

        if cancel_ack_timestamps:
            summary["cancel_ack_ts"] = max(cancel_ack_timestamps)

        if ws_sequences:
            summary["ws_seq_first"] = min(ws_sequences)
            summary["ws_seq_last"] = max(ws_sequences)

        return summary

    def detect_race_conditions(self) -> List[str]:
        """Detect potential race conditions in order timeline."""
        issues = []

        if len(self.history) < 2:
            return issues

        # Check for FILLED -> CANCELLED race condition
        for i in range(len(self.history) - 1):
            current = self.history[i]
            next_event = self.history[i + 1]

            if (current.state == OrderState.FILLED and
                next_event.state == OrderState.CANCELLED):
                issues.append(
                    f"FILLED->CANCELLED race: filled at {current.engine_ts}, "
                    f"cancelled at {next_event.cancel_ack_ts}"
                )

        return issues


__all__ = ["Order", "OrderEvent", "OrderState"]