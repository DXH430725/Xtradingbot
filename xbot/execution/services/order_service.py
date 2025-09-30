"""Order lifecycle management service.

Handles order placement, cancellation, and reconciliation.
Uses tracking_limit.py as the ONLY implementation for tracking limit orders.
Target: < 600 lines.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional, List

# Import will be fixed after order_model.py is created
# from ..order_model import Order, OrderEvent, OrderState


class OrderService:
    """Service for order lifecycle management and reconciliation."""

    def __init__(self, *, logger: Optional[logging.Logger] = None) -> None:
        self.log = logger or logging.getLogger("xbot.execution.order_service")

        # Connectors registry
        self._connectors: Dict[str, Any] = {}

        # Active orders: (venue, coi) -> Order
        self._active_orders: Dict[tuple[str, int], Any] = {}

        # COI management per venue
        self._coi_counters: Dict[str, int] = {}
        self._coi_limits: Dict[str, int] = {}

        # API key indices
        self._api_key_indices: Dict[str, Optional[int]] = {}

        # Locks for thread safety
        self._order_lock = asyncio.Lock()

    def register_connector(
        self,
        venue: str,
        connector: Any,
        *,
        coi_limit: Optional[int] = None,
        api_key_index: Optional[int] = None,
    ) -> None:
        """Register connector for this venue."""
        key = venue.lower()
        self._connectors[key] = connector

        if coi_limit:
            self._coi_limits[key] = coi_limit

        if api_key_index is not None:
            self._api_key_indices[key] = api_key_index

        # Initialize COI counter
        if key not in self._coi_counters:
            self._coi_counters[key] = int(time.time()) % 1000000

        self.log.debug(f"Order service registered connector: {key}")

    def _next_coi(self, venue: str) -> int:
        """Generate next client order index for venue."""
        key = venue.lower()
        current = self._coi_counters.get(key, 1)
        limit = self._coi_limits.get(key, 999999)

        next_coi = (current % limit) + 1
        self._coi_counters[key] = next_coi
        return next_coi

    async def limit_order(
        self,
        venue: str,
        symbol: str,
        *,
        size_i: int,
        price_i: int,
        is_ask: bool,
        tracking: bool = False,
        post_only: bool = False,
        reduce_only: int = 0,
        **kwargs
    ) -> Any:
        """Place limit order.

        Args:
            venue: Exchange venue
            symbol: Canonical symbol
            size_i: Size in integer format
            price_i: Price in integer format
            is_ask: True for sell, False for buy
            tracking: If True, use tracking limit implementation
            post_only: Post-only flag
            reduce_only: Reduce-only mode

        Returns:
            Order object or TrackingLimitOrder if tracking=True
        """
        venue_key = venue.lower()
        connector = self._connectors.get(venue_key)
        if not connector:
            raise ValueError(f"No connector for venue: {venue}")

        coi = self._next_coi(venue)

        if tracking:
            # Use ONLY tracking_limit.py implementation
            from ..tracking_limit import place_tracking_limit_order

            # Map symbol to venue format
            # This will be replaced with symbol service call
            venue_symbol = symbol  # Temporary

            return await place_tracking_limit_order(
                connector=connector,
                symbol=venue_symbol,
                coi=coi,
                size_i=size_i,
                price_i=price_i,
                is_ask=is_ask,
                post_only=post_only,
                reduce_only=reduce_only,
                **kwargs
            )
        else:
            # Direct order placement
            async with self._order_lock:
                # Create order object (placeholder for now)
                order = {
                    'coi': coi,
                    'venue': venue,
                    'symbol': symbol,
                    'side': 'sell' if is_ask else 'buy',
                    'size_i': size_i,
                    'price_i': price_i,
                    'state': 'NEW',
                    'created_at': time.time()
                }

                # Store in active orders
                self._active_orders[(venue_key, coi)] = order

                try:
                    # Map symbol to venue format
                    venue_symbol = symbol  # Temporary

                    # Submit to connector
                    if hasattr(connector, 'submit_limit_order'):
                        exchange_order_id = await connector.submit_limit_order(
                            symbol=venue_symbol,
                            client_order_index=coi,
                            base_amount=size_i,
                            price=price_i,
                            is_ask=is_ask,
                            post_only=post_only,
                            reduce_only=reduce_only
                        )
                        order['exchange_order_id'] = exchange_order_id
                        order['state'] = 'SUBMITTING'
                    else:
                        # Fallback to old interface
                        result = await connector.place_limit(
                            symbol=venue_symbol,
                            client_order_index=coi,
                            base_amount=size_i,
                            price=price_i,
                            is_ask=is_ask,
                            post_only=post_only,
                            reduce_only=reduce_only
                        )
                        order['result'] = result
                        order['state'] = 'SUBMITTING'

                    self.log.info(f"Placed limit order: {venue}:{symbol} coi={coi}")
                    return order

                except Exception as e:
                    order['state'] = 'FAILED'
                    order['error'] = str(e)
                    self.log.error(f"Failed to place limit order: {e}")
                    raise

    async def market_order(
        self,
        venue: str,
        symbol: str,
        *,
        size_i: int,
        is_ask: bool,
        reduce_only: int = 0,
        **kwargs
    ) -> Any:
        """Place market order."""
        venue_key = venue.lower()
        connector = self._connectors.get(venue_key)
        if not connector:
            raise ValueError(f"No connector for venue: {venue}")

        coi = self._next_coi(venue)

        async with self._order_lock:
            # Create order object
            order = {
                'coi': coi,
                'venue': venue,
                'symbol': symbol,
                'side': 'sell' if is_ask else 'buy',
                'size_i': size_i,
                'state': 'NEW',
                'created_at': time.time()
            }

            # Store in active orders
            self._active_orders[(venue_key, coi)] = order

            try:
                # Map symbol to venue format
                venue_symbol = symbol  # Temporary

                # Submit to connector
                if hasattr(connector, 'submit_market_order'):
                    exchange_order_id = await connector.submit_market_order(
                        symbol=venue_symbol,
                        client_order_index=coi,
                        size_i=size_i,
                        is_ask=is_ask,
                        reduce_only=reduce_only
                    )
                    order['exchange_order_id'] = exchange_order_id
                else:
                    # Fallback to old interface
                    result = await connector.place_market(
                        symbol=venue_symbol,
                        client_order_index=coi,
                        base_amount=size_i,
                        is_ask=is_ask,
                        reduce_only=reduce_only
                    )
                    order['result'] = result

                order['state'] = 'SUBMITTING'
                self.log.info(f"Placed market order: {venue}:{symbol} coi={coi}")
                return order

            except Exception as e:
                order['state'] = 'FAILED'
                order['error'] = str(e)
                self.log.error(f"Failed to place market order: {e}")
                raise

    async def cancel(self, venue: str, symbol: str, coi: int) -> None:
        """Cancel order by client order index."""
        venue_key = venue.lower()
        connector = self._connectors.get(venue_key)
        if not connector:
            raise ValueError(f"No connector for venue: {venue}")

        order_key = (venue_key, coi)
        order = self._active_orders.get(order_key)

        try:
            # Map symbol to venue format
            venue_symbol = symbol  # Temporary

            if hasattr(connector, 'cancel_by_client_id'):
                await connector.cancel_by_client_id(venue_symbol, coi)
            else:
                # Fallback to old interface
                await connector.cancel_order(coi)

            if order:
                order['state'] = 'CANCELLING'

            self.log.info(f"Cancelled order: {venue}:{symbol} coi={coi}")

        except Exception as e:
            self.log.error(f"Failed to cancel order {venue}:{coi}: {e}")
            if order:
                order['cancel_error'] = str(e)
            raise

    async def get_order(self, venue: str, coi: int) -> Optional[Any]:
        """Get order by venue and COI."""
        order_key = (venue.lower(), coi)
        return self._active_orders.get(order_key)

    async def get_active_orders(self, venue: Optional[str] = None) -> List[Any]:
        """Get all active orders, optionally filtered by venue."""
        if venue:
            venue_key = venue.lower()
            return [
                order for (v, _), order in self._active_orders.items()
                if v == venue_key
            ]
        return list(self._active_orders.values())

    async def reconcile(self, venue: str, symbol: str) -> List[Any]:
        """Reconcile orders with exchange state.

        This would typically:
        1. Query exchange for open orders
        2. Compare with local state
        3. Update local state based on exchange truth
        4. Detect and log discrepancies
        """
        venue_key = venue.lower()
        connector = self._connectors.get(venue_key)
        if not connector:
            self.log.warning(f"No connector for reconciliation: {venue}")
            return []

        try:
            # Map symbol to venue format
            venue_symbol = symbol  # Temporary

            # Get orders from exchange
            if hasattr(connector, 'get_open_orders'):
                exchange_orders = await connector.get_open_orders(venue_symbol)
            else:
                exchange_orders = []

            # Find discrepancies
            discrepancies = []
            venue_orders = [
                order for (v, _), order in self._active_orders.items()
                if v == venue_key and order.get('symbol') == symbol
            ]

            for order in venue_orders:
                # Check if order exists on exchange
                found = False
                coi = order['coi']

                for ex_order in exchange_orders:
                    if ex_order.get('client_order_id') == coi:
                        found = True
                        # Update local state if needed
                        ex_state = ex_order.get('status', '').upper()
                        if ex_state != order['state']:
                            self.log.info(f"State mismatch for {venue}:{coi}: "
                                        f"local={order['state']} exchange={ex_state}")
                            order['state'] = ex_state
                            discrepancies.append(order)
                        break

                if not found and order['state'] in ['OPEN', 'SUBMITTING']:
                    self.log.warning(f"Order {venue}:{coi} not found on exchange")
                    discrepancies.append(order)

            return discrepancies

        except Exception as e:
            self.log.error(f"Reconciliation failed for {venue}:{symbol}: {e}")
            return []

    def cleanup_completed_orders(self, max_age_seconds: float = 3600) -> int:
        """Clean up old completed orders."""
        current_time = time.time()
        completed_states = {'FILLED', 'CANCELLED', 'FAILED'}

        keys_to_remove = []
        for key, order in self._active_orders.items():
            if (order.get('state') in completed_states and
                current_time - order.get('created_at', 0) > max_age_seconds):
                keys_to_remove.append(key)

        for key in keys_to_remove:
            del self._active_orders[key]

        if keys_to_remove:
            self.log.debug(f"Cleaned up {len(keys_to_remove)} completed orders")

        return len(keys_to_remove)