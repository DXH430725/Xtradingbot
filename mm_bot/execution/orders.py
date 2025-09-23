"""Async order tracking primitives shared across connectors and strategies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class OrderState(str, Enum):
    NEW = "new"
    SUBMITTING = "submitting"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


FINAL_STATES = {OrderState.FILLED, OrderState.CANCELLED, OrderState.FAILED}


@dataclass
class OrderUpdate:
    state: OrderState
    client_order_id: Optional[int] = None
    exchange_order_id: Optional[str] = None
    symbol: Optional[str] = None
    is_ask: Optional[bool] = None
    filled_base: Optional[float] = None
    remaining_base: Optional[float] = None
    info: Dict[str, Any] = field(default_factory=dict)


class OrderTracker:
    """Tracks state transitions and exposes awaitables for completion."""

    def __init__(self, connector: str, client_order_id: Optional[int]) -> None:
        self.connector = connector
        self.client_order_id = client_order_id
        self.exchange_order_id: Optional[str] = None
        self.state: OrderState = OrderState.NEW
        self.symbol: Optional[str] = None
        self.is_ask: Optional[bool] = None
        self.history: List[OrderUpdate] = []
        self._loop = asyncio.get_event_loop()
        self._final_future: asyncio.Future[OrderUpdate] = self._loop.create_future()
        self._update_waiters: List[asyncio.Future[OrderUpdate]] = []

    def apply(self, update: OrderUpdate) -> OrderUpdate:
        if update.exchange_order_id:
            self.exchange_order_id = str(update.exchange_order_id)
        if update.symbol:
            self.symbol = update.symbol
        if update.is_ask is not None:
            self.is_ask = update.is_ask
        self.state = update.state
        self.history.append(update)
        # wake pending waiters
        for waiter in list(self._update_waiters):
            if not waiter.done():
                waiter.set_result(update)
        self._update_waiters.clear()
        # resolve final future
        if update.state in FINAL_STATES and not self._final_future.done():
            self._final_future.set_result(update)
        return update

    async def wait_final(self, timeout: Optional[float] = None) -> OrderUpdate:
        future = asyncio.shield(self._final_future)
        return await asyncio.wait_for(future, timeout=timeout) if timeout else await future

    async def next_update(self, timeout: Optional[float] = None) -> OrderUpdate:
        fut: asyncio.Future[OrderUpdate] = self._loop.create_future()
        self._update_waiters.append(fut)
        future = asyncio.shield(fut)
        return await asyncio.wait_for(future, timeout=timeout) if timeout else await future

    def snapshot(self) -> OrderUpdate:
        return self.history[-1] if self.history else OrderUpdate(state=self.state, client_order_id=self.client_order_id)


class TrackingOrder:
    """Thin wrapper exposing async helpers on top of OrderTracker."""

    def __init__(self, tracker: OrderTracker) -> None:
        self._tracker = tracker

    @property
    def client_order_id(self) -> Optional[int]:
        return self._tracker.client_order_id

    @property
    def exchange_order_id(self) -> Optional[str]:
        return self._tracker.exchange_order_id

    @property
    def state(self) -> OrderState:
        return self._tracker.state

    @property
    def symbol(self) -> Optional[str]:
        return self._tracker.symbol

    async def wait_final(self, timeout: Optional[float] = None) -> OrderUpdate:
        return await self._tracker.wait_final(timeout=timeout)

    async def next_update(self, timeout: Optional[float] = None) -> OrderUpdate:
        return await self._tracker.next_update(timeout=timeout)

    def snapshot(self) -> OrderUpdate:
        return self._tracker.snapshot()


class TrackingLimitOrder(TrackingOrder):
    def __init__(self, tracker: OrderTracker, price_i: Optional[int] = None, size_i: Optional[int] = None) -> None:
        super().__init__(tracker)
        self.price_i = price_i
        self.size_i = size_i


class TrackingMarketOrder(TrackingOrder):
    pass


__all__ = [
    "OrderState",
    "OrderUpdate",
    "OrderTracker",
    "TrackingOrder",
    "TrackingLimitOrder",
    "TrackingMarketOrder",
    "FINAL_STATES",
]
