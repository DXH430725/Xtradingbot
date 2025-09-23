"""Common connector base class providing order state tracking and event fan-out."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional

from mm_bot.execution.orders import OrderState, OrderTracker, OrderUpdate, TrackingLimitOrder, TrackingMarketOrder


class ConnectorEventType(str, Enum):
    ORDER = "order"
    TRADE = "trade"
    POSITION = "position"
    ACCOUNT = "account"
    BOOK = "book"


@dataclass
class ConnectorEvent:
    type: ConnectorEventType
    payload: Any
    meta: Optional[Dict[str, Any]] = None


Listener = Callable[[ConnectorEvent], None]


class BaseConnector:
    """Base class wiring order tracking + listener helpers for connectors."""

    STATUS_MAP: Dict[str, OrderState] = {
        "new": OrderState.NEW,
        "created": OrderState.NEW,
        "pending": OrderState.SUBMITTING,
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

    def __init__(self, name: str, *, debug: bool = False, logger: Optional[logging.Logger] = None) -> None:
        self.name = name
        self.debug_enabled = debug
        self.log = logger or logging.getLogger(f"mm_bot.connector.{name}")
        self._listeners: List[Listener] = []
        self._order_trackers_by_client: Dict[str, OrderTracker] = {}
        self._order_trackers_by_exchange: Dict[str, OrderTracker] = {}
        self._order_lock = asyncio.Lock()
        self._callbacks: Dict[str, Optional[Callable[[Dict[str, Any]], None]]] = {
            "order_filled": None,
            "order_cancelled": None,
            "trade": None,
            "position": None,
        }

    # ------------------------------------------------------------------
    # Listener management
    # ------------------------------------------------------------------
    def register_listener(self, listener: Listener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _broadcast(self, event: ConnectorEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                self.log.exception("connector listener crashed")

    # ------------------------------------------------------------------
    # Event handler compatibility layer
    # ------------------------------------------------------------------
    def set_event_handlers(
        self,
        *,
        on_order_filled: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_order_cancelled: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_trade: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_position_update: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._callbacks["order_filled"] = on_order_filled
        self._callbacks["order_cancelled"] = on_order_cancelled
        self._callbacks["trade"] = on_trade
        self._callbacks["position"] = on_position_update

    # ------------------------------------------------------------------
    # Order tracking helpers
    # ------------------------------------------------------------------
    def _track_order(self, client_order_id: Optional[int], *, symbol: Optional[str] = None, is_ask: Optional[bool] = None) -> OrderTracker:
        key = self._client_key(client_order_id)
        tracker = self._order_trackers_by_client.get(key)
        if not tracker:
            tracker = OrderTracker(self.name, client_order_id)
            self._order_trackers_by_client[key] = tracker
        if symbol:
            tracker.symbol = symbol
        if is_ask is not None:
            tracker.is_ask = is_ask
        return tracker

    def _client_key(self, client_order_id: Optional[int]) -> str:
        return str(client_order_id) if client_order_id is not None else ""

    def _exchange_key(self, exchange_order_id: Optional[str]) -> str:
        return str(exchange_order_id) if exchange_order_id is not None else ""

    def _lookup_tracker(
        self,
        *,
        client_order_id: Optional[int] = None,
        exchange_order_id: Optional[str] = None,
    ) -> Optional[OrderTracker]:
        if client_order_id is not None:
            tracker = self._order_trackers_by_client.get(self._client_key(client_order_id))
            if tracker:
                return tracker
        if exchange_order_id is not None:
            return self._order_trackers_by_exchange.get(self._exchange_key(exchange_order_id))
        return None

    def _status_to_state(self, status: Optional[str], raw: Optional[Dict[str, Any]] = None) -> OrderState:
        if not status:
            return OrderState.OPEN
        return self.STATUS_MAP.get(str(status).lower(), OrderState.OPEN)

    def _ensure_tracker(
        self,
        *,
        client_order_id: Optional[int] = None,
        exchange_order_id: Optional[str] = None,
        symbol: Optional[str] = None,
        is_ask: Optional[bool] = None,
    ) -> OrderTracker:
        tracker = self._lookup_tracker(client_order_id=client_order_id, exchange_order_id=exchange_order_id)
        if tracker is None:
            tracker = self._track_order(client_order_id, symbol=symbol, is_ask=is_ask)
        if exchange_order_id:
            self._order_trackers_by_exchange[self._exchange_key(exchange_order_id)] = tracker
        if symbol:
            tracker.symbol = symbol
        if is_ask is not None:
            tracker.is_ask = is_ask
        return tracker

    def _update_order_state(
        self,
        *,
        client_order_id: Optional[int] = None,
        exchange_order_id: Optional[str] = None,
        state: Optional[OrderState] = None,
        status: Optional[str] = None,
        symbol: Optional[str] = None,
        is_ask: Optional[bool] = None,
        filled_base: Optional[float] = None,
        remaining_base: Optional[float] = None,
        info: Optional[Dict[str, Any]] = None,
    ) -> OrderTracker:
        resolved_state = state or self._status_to_state(status, info)
        tracker = self._ensure_tracker(
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
            symbol=symbol,
            is_ask=is_ask,
        )
        update = OrderUpdate(
            state=resolved_state,
            client_order_id=tracker.client_order_id,
            exchange_order_id=tracker.exchange_order_id or exchange_order_id,
            symbol=tracker.symbol or symbol,
            is_ask=tracker.is_ask if is_ask is None else is_ask,
            filled_base=filled_base,
            remaining_base=remaining_base,
            info=info or {},
        )
        tracker.apply(update)
        self._handle_order_callbacks(update)
        self._broadcast(ConnectorEvent(ConnectorEventType.ORDER, update))
        self._debug_order(update)
        return tracker

    def _debug_order(self, update: OrderUpdate) -> None:
        if not self.debug_enabled:
            return
        cid = update.client_order_id
        eid = update.exchange_order_id
        self.log.debug(
            "order_update connector=%s cid=%s eid=%s state=%s info_keys=%s",
            self.name,
            cid,
            eid,
            update.state.value,
            list(update.info.keys()) if update.info else None,
        )

    def _handle_order_callbacks(self, update: OrderUpdate) -> None:
        payload = update.info or {}
        if update.state in {OrderState.PARTIALLY_FILLED, OrderState.FILLED}:
            cb = self._callbacks.get("order_filled")
            if cb:
                try:
                    cb(payload)
                except Exception:
                    self.log.exception("order_filled callback error")
        elif update.state == OrderState.CANCELLED:
            cb = self._callbacks.get("order_cancelled")
            if cb:
                try:
                    cb(payload)
                except Exception:
                    self.log.exception("order_cancelled callback error")

    # ------------------------------------------------------------------
    # Public helpers for derived connectors
    # ------------------------------------------------------------------
    def create_tracking_limit_order(
        self,
        client_order_id: Optional[int],
        *,
        symbol: Optional[str] = None,
        is_ask: Optional[bool] = None,
        price_i: Optional[int] = None,
        size_i: Optional[int] = None,
    ) -> TrackingLimitOrder:
        tracker = self._track_order(client_order_id, symbol=symbol, is_ask=is_ask)
        return TrackingLimitOrder(tracker, price_i=price_i, size_i=size_i)

    def create_tracking_market_order(
        self,
        client_order_id: Optional[int],
        *,
        symbol: Optional[str] = None,
        is_ask: Optional[bool] = None,
    ) -> TrackingMarketOrder:
        tracker = self._track_order(client_order_id, symbol=symbol, is_ask=is_ask)
        return TrackingMarketOrder(tracker)

    def update_orders_from_events(
        self,
        events: Iterable[Dict[str, Any]],
        *,
        client_key: str = "clientId",
        exchange_key: str = "id",
        status_key: str = "status",
        symbol_key: str = "symbol",
    ) -> None:
        for event in events:
            cid = event.get(client_key)
            try:
                client_id = int(cid) if cid is not None else None
            except (TypeError, ValueError):
                client_id = None
            exchange_id = event.get(exchange_key)
            status = event.get(status_key)
            symbol = event.get(symbol_key)
            self._update_order_state(
                client_order_id=client_id,
                exchange_order_id=exchange_id,
                status=status,
                symbol=symbol,
                info=event,
            )

    def emit_trade(self, payload: Dict[str, Any]) -> None:
        cb = self._callbacks.get("trade")
        if cb:
            try:
                cb(payload)
            except Exception:
                self.log.exception("trade callback error")
        self._broadcast(ConnectorEvent(ConnectorEventType.TRADE, payload))

    def emit_position(self, payload: Dict[str, Any]) -> None:
        cb = self._callbacks.get("position")
        if cb:
            try:
                cb(payload)
            except Exception:
                self.log.exception("position callback error")
        self._broadcast(ConnectorEvent(ConnectorEventType.POSITION, payload))


__all__ = [
    "BaseConnector",
    "ConnectorEvent",
    "ConnectorEventType",
]
