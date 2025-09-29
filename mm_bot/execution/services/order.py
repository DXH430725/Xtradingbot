"""Order service - unified order placement, cancellation, and reconciliation."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from ..adapters import get_api_key_index
from ..ids import COIManager, NonceManager
from ..order import Order, OrderEvent, OrderState
from ..order_actions import place_tracking_market_order
from ..tracking_limit import place_tracking_limit_order
from .symbol import SymbolService


class OrderService:
    """Service for order placement, cancellation, and state reconciliation.

    Single source of truth for all order operations, enforcing use of
    tracking_limit.place_tracking_limit_order for tracking limit orders.
    """

    def __init__(self, *, logger: Optional[logging.Logger] = None) -> None:
        self.log = logger or logging.getLogger("mm_bot.execution.services.order")

        # Dependencies
        self._symbol_service = SymbolService(logger=self.log)

        # ID managers
        self.coi_manager = COIManager(logger=self.log)
        self.nonce_manager = NonceManager(logger=self.log)

        # Connectors and metadata
        self._connectors: Dict[str, Any] = {}
        self._api_keys: Dict[str, Optional[int]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

        # Order registry - single source of truth
        self._orders: Dict[str, Order] = {}

    def register_connector(
        self,
        venue: str,
        connector: Any,
        *,
        coi_limit: Optional[int] = None,
        api_key_index: Optional[int] = None,
    ) -> None:
        """Register connector and metadata."""
        key = venue.lower()
        self._connectors[key] = connector
        self._symbol_service.register_connector(key, connector)

        # Clear old lock (will be recreated)
        self._locks.pop(key, None)

        # Register COI limits
        if coi_limit:
            self.coi_manager.register_limit(key, coi_limit)

        # Register API key index
        if api_key_index is not None:
            self._api_keys[key] = api_key_index
        else:
            self._api_keys[key] = get_api_key_index(connector)

    async def market_order(
        self,
        venue: str,
        canonical_symbol: str,
        *,
        size_i: int,
        is_ask: bool,
        reduce_only: int = 0,
        max_slippage: Optional[float] = None,
        attempts: int = 1,
        retry_delay: float = 0.0,
        wait_timeout: float = 30.0,
        label: Optional[str] = None,
    ) -> Any:
        """Place market order using existing order_actions implementation."""
        connector = self._connectors[venue.lower()]
        venue_symbol = self._symbol_service.map_symbol(canonical_symbol, venue)
        lock = await self._get_lock(venue)
        api_key_index = self._api_keys.get(venue.lower())

        result = await place_tracking_market_order(
            connector,
            symbol=venue_symbol,
            size_i=size_i,
            is_ask=is_ask,
            reduce_only=reduce_only,
            max_slippage=max_slippage,
            attempts=attempts,
            retry_delay=retry_delay,
            wait_timeout=wait_timeout,
            coi_manager=self.coi_manager,
            nonce_manager=self.nonce_manager,
            api_key_index=api_key_index,
            lock=lock,
            label=label or f"{venue}:{venue_symbol}",
            logger=self.log,
        )

        # Register order in our registry if successful
        if result and hasattr(result, '_tracker'):
            self._register_order_from_tracker(result._tracker, venue, canonical_symbol)

        return result

    async def limit_order(
        self,
        venue: str,
        canonical_symbol: str,
        *,
        base_amount_i: int,
        is_ask: bool,
        **kwargs: Any,
    ) -> Any:
        """Place limit order - MUST use tracking_limit.place_tracking_limit_order."""
        connector = self._connectors[venue.lower()]
        venue_symbol = self._symbol_service.map_symbol(canonical_symbol, venue)
        lock = await self._get_lock(venue)

        # ENFORCED: Only call tracking_limit.place_tracking_limit_order
        result = await place_tracking_limit_order(
            connector,
            symbol=venue_symbol,
            base_amount_i=base_amount_i,
            is_ask=is_ask,
            coi_manager=self.coi_manager,
            lock=lock,
            logger=self.log,
            **kwargs,
        )

        # Register order in our registry if successful
        if result and hasattr(result, '_tracker'):
            self._register_order_from_tracker(result._tracker, venue, canonical_symbol)

        return result

    def reconcile(self, event_data: Dict[str, Any], source: str = "ws") -> None:
        """Reconcile order event from WebSocket or REST with enhanced timeline tracking.

        Implements fixed time-line reconciliation rule:
        - WS incremental events take priority for real-time updates
        - REST data used for periodic final consistency validation
        - Race conditions resolved by engine timestamp precedence

        Args:
            event_data: Raw event data from WS or REST
            source: Event source ("ws" or "rest") for timeline tracking
        """
        try:
            # Extract order identification - support multiple formats
            client_order_id = self._extract_client_order_id(event_data)
            exchange_order_id = self._extract_exchange_order_id(event_data)

            if not client_order_id and not exchange_order_id:
                self.log.warning("Received event without order IDs from %s: %s", source, event_data)
                return

            # Find or create order
            order = self._find_order(client_order_id, exchange_order_id)
            if not order:
                self.log.debug("Order not found for reconciliation: coi=%s, eid=%s, source=%s",
                             client_order_id, exchange_order_id, source)
                return

            # Create event from data with source tracking
            event = self._create_event_from_data(event_data, source=source)

            # Apply reconciliation rules
            if self._should_apply_event(order, event):
                order.append_event(event)

                # Enhanced logging with timing details
                self.log.debug(
                    "Applied %s event to order %s: %s -> %s (engine_ts=%s, cancel_ack_ts=%s, ws_seq=%s)",
                    source, order.id, order.state.value, event.state.value,
                    event.engine_ts, event.cancel_ack_ts, event.ws_seq
                )

                # Timeline analysis for final states
                if event.state in {OrderState.FILLED, OrderState.CANCELLED}:
                    self._analyze_order_timeline(order, event, source)

        except Exception as e:
            self.log.error("Error reconciling %s event: %s", source, e, exc_info=True)

    def _analyze_order_timeline(self, order: Order, final_event: OrderEvent, source: str) -> None:
        """Analyze order timeline and detect race conditions for arbitrage window analysis."""
        timeline = order.get_timeline_summary()
        races = order.detect_race_conditions()

        # Log race conditions with severity
        if races:
            self.log.warning("Race conditions detected for order %s: %s", order.id, races)

        # Enhanced timeline logging for arbitrage analysis
        timeline_msg = (
            f"Order timeline {order.id} ({source}): "
            f"duration={timeline.get('duration_ms', 0):.1f}ms, "
            f"events={timeline.get('event_count', 0)}, "
            f"final_state={timeline.get('final_state', 'unknown')}"
        )

        # Add timing details if available
        timing_details = []
        if timeline.get('engine_ts_first'):
            timing_details.append(f"engine_ts_range={timeline['engine_ts_last'] - timeline['engine_ts_first']:.3f}s")
        if timeline.get('cancel_ack_ts'):
            timing_details.append(f"cancel_ack_ts={timeline['cancel_ack_ts']}")
        if timeline.get('ws_seq_first'):
            timing_details.append(f"ws_seq_range={timeline['ws_seq_last'] - timeline['ws_seq_first']}")

        if timing_details:
            timeline_msg += f", {', '.join(timing_details)}"

        self.log.info(timeline_msg)

    def _extract_client_order_id(self, data: Dict[str, Any]) -> Optional[int]:
        """Extract client order ID from various event formats."""
        # Try different field names used by different venues
        for field in ["client_order_id", "clientId", "clientID", "c"]:
            value = data.get(field)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return None

    def _extract_exchange_order_id(self, data: Dict[str, Any]) -> Optional[str]:
        """Extract exchange order ID from various event formats."""
        # Try different field names used by different venues
        for field in ["exchange_order_id", "id", "orderId", "order_id", "i"]:
            value = data.get(field)
            if value is not None:
                return str(value)
        return None

    def _find_order(self, client_order_id: Optional[int], exchange_order_id: Optional[str]) -> Optional[Order]:
        """Find order by client or exchange ID."""
        # Search by client order ID first
        if client_order_id:
            for order in self._orders.values():
                if order.client_order_id == client_order_id:
                    return order

        # Search by exchange order ID
        if exchange_order_id:
            for order in self._orders.values():
                if order.exchange_order_id == exchange_order_id:
                    return order

        return None

    def _create_event_from_data(self, data: Dict[str, Any], source: str = "ws") -> OrderEvent:
        """Create OrderEvent from raw event data with enhanced timestamp extraction."""
        # Map status to OrderState with multiple field names
        status_fields = ["status", "X", "state"]
        status = ""
        for field in status_fields:
            status = str(data.get(field, "")).lower()
            if status:
                break

        state_map = {
            "new": OrderState.NEW,
            "created": OrderState.NEW,
            "pending": OrderState.SUBMITTING,
            "submitting": OrderState.SUBMITTING,
            "working": OrderState.OPEN,
            "open": OrderState.OPEN,
            "partiallyfilled": OrderState.PARTIALLY_FILLED,
            "partially_filled": OrderState.PARTIALLY_FILLED,
            "filled": OrderState.FILLED,
            "canceled": OrderState.CANCELLED,
            "cancelled": OrderState.CANCELLED,
            "expired": OrderState.CANCELLED,
            "rejected": OrderState.FAILED,
            "failed": OrderState.FAILED,
        }
        state = state_map.get(status, OrderState.OPEN)

        # Extract filled/remaining quantities with multiple field names
        filled_base_i = self._extract_quantity(data, ["filled_base_i", "filledQuantity", "z", "filled_qty"])
        remaining_base_i = self._extract_quantity(data, ["remaining_base_i", "remainingQuantity", "l", "remaining_qty"])

        # Extract timestamps with venue-specific field names
        engine_ts = self._extract_timestamp(data, ["engine_ts", "timestamp", "E", "T", "transactTime"])
        cancel_ack_ts = None
        if state == OrderState.CANCELLED:
            cancel_ack_ts = self._extract_timestamp(data, ["cancel_ack_ts", "cancelTime", "timestamp"])

        # Extract WebSocket sequence number
        ws_seq = None
        if source == "ws":
            ws_seq = data.get("ws_seq") or data.get("seq") or data.get("sequence")
            if ws_seq is not None:
                try:
                    ws_seq = int(ws_seq)
                except (TypeError, ValueError):
                    ws_seq = None

        return OrderEvent(
            state=state,
            filled_base_i=filled_base_i,
            remaining_base_i=remaining_base_i,
            engine_ts=engine_ts,
            cancel_ack_ts=cancel_ack_ts,
            ws_seq=ws_seq,
            timestamp=time.time(),
            info={**data, "source": source},  # Include source in info
        )

    def _extract_quantity(self, data: Dict[str, Any], field_names: List[str]) -> Optional[int]:
        """Extract quantity and convert to integer scaled format."""
        for field in field_names:
            value = data.get(field)
            if value is not None:
                try:
                    return int(float(value))
                except (TypeError, ValueError):
                    continue
        return None

    def _extract_timestamp(self, data: Dict[str, Any], field_names: List[str]) -> Optional[float]:
        """Extract timestamp and normalize to float seconds."""
        for field in field_names:
            value = data.get(field)
            if value is not None:
                try:
                    ts = float(value)
                    # Convert milliseconds to seconds if needed
                    if ts > 1e12:  # Likely milliseconds
                        ts = ts / 1000.0
                    return ts
                except (TypeError, ValueError):
                    continue
        return None

    def _should_apply_event(self, order: Order, event: OrderEvent) -> bool:
        """Determine if event should be applied based on reconciliation rules.

        Rules:
        1. Always accept first event
        2. For competing events, use engine timestamp precedence
        3. For FILLED->CANCELLED race, prefer FILLED (use engine_ts)
        """
        if not order.history:
            return True  # First event, always accept

        last_event = order.history[-1]

        # If same state, ignore duplicate
        if last_event.state == event.state:
            return False

        # Race condition: FILLED -> CANCELLED
        if (last_event.state == OrderState.FILLED and
            event.state == OrderState.CANCELLED):

            # Use engine timestamps to resolve
            last_ts = last_event.engine_ts or last_event.timestamp
            event_ts = event.engine_ts or event.timestamp

            if last_ts and event_ts and event_ts < last_ts:
                # Cancel came before fill, accept cancellation
                return True
            else:
                # Fill came first or timestamps unclear, keep filled
                self.log.info(
                    "Rejected CANCELLED event after FILLED for order %s (fill_ts=%s, cancel_ts=%s)",
                    order.id, last_ts, event_ts
                )
                return False

        # Default: accept state transitions
        return True

    def _register_order_from_tracker(self, tracker: Any, venue: str, canonical_symbol: str) -> Order:
        """Register order from legacy tracker object."""
        order = Order(
            id=f"{venue}_{tracker.client_order_id}",
            venue=venue,
            symbol=canonical_symbol,
            side="sell" if getattr(tracker, 'is_ask', False) else "buy",
            client_order_id=tracker.client_order_id,
            exchange_order_id=getattr(tracker, 'exchange_order_id', None),
        )

        # Import existing state from tracker
        if hasattr(tracker, 'state'):
            initial_event = OrderEvent(state=tracker.state, timestamp=time.time())
            order.append_event(initial_event)

        self._orders[order.id] = order
        return order

    async def _get_lock(self, venue: str) -> asyncio.Lock:
        """Get or create venue-specific lock."""
        key = venue.lower()
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock


__all__ = ["OrderService"]