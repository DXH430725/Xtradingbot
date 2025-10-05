from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, TYPE_CHECKING

from xbot.connector.interface import IConnector

from .market_data_service import MarketDataService
from .models import Order, OrderState

if TYPE_CHECKING:
    from .models import OrderEvent
    from .order_service import OrderService


class TrackingLimitTimeoutError(TimeoutError):
    pass


@dataclass(slots=True)
class TrackingAttempt:
    attempt: int
    client_order_index: int
    price_i: int
    state: OrderState
    info: Dict[str, object]


class TrackingLimitOrder:
    def __init__(
        self,
        final_order: Order,
        attempts: List[TrackingAttempt],
        filled_base_i: int,
    ) -> None:
        self._final_order = final_order
        self.attempts = attempts
        self.filled_base_i = filled_base_i

    @property
    def order(self) -> Order:
        return self._final_order

    @property
    def attempts_count(self) -> int:
        return len(self.attempts)

    async def wait_final(self, timeout: Optional[float] = None) -> "OrderEvent":
        return await self._final_order.wait_final(timeout=timeout)


class TrackingLimitEngine:
    """Single implementation of the tracking-limit orchestration loop."""

    def __init__(
        self,
        *,
        market_data: MarketDataService,
        default_interval_secs: float = 10.0,
        default_timeout_secs: float = 120.0,
        cancel_wait_secs: float = 2.0,
    ) -> None:
        self._market_data = market_data
        self._default_interval = default_interval_secs
        self._default_timeout = default_timeout_secs
        self._cancel_wait_secs = cancel_wait_secs

    async def place(
        self,
        *,
        order_service: "OrderService",
        connector: IConnector,
        symbol: str,
        base_amount_i: int,
        is_ask: bool,
        interval_secs: Optional[float] = None,
        timeout_secs: Optional[float] = None,
        price_offset_ticks: int = 0,
        max_attempts: Optional[int] = None,
        post_only: bool = False,
        reduce_only: int = 0,
        trace_id: Optional[str] = None,
    ) -> TrackingLimitOrder:
        interval = interval_secs or self._default_interval
        timeout = timeout_secs or self._default_timeout
        deadline = time.monotonic() + timeout
        attempt = 0
        cumulative_filled = 0
        remaining = base_amount_i
        records: List[TrackingAttempt] = []

        while True:
            attempt += 1
            if max_attempts and attempt > max_attempts:
                raise TrackingLimitTimeoutError("max attempts reached before fill")
            now = time.monotonic()
            if now >= deadline:
                raise TrackingLimitTimeoutError("tracking limit timeout reached")
            bid_i, ask_i, _scale = await self._market_data.get_top_of_book(symbol)
            reference = ask_i if is_ask else bid_i
            if reference is None:
                raise RuntimeError("top of book unavailable for tracking limit")
            price_i = reference + price_offset_ticks if is_ask else reference - price_offset_ticks
            if price_i <= 0:
                raise ValueError("price offset results in non-positive price")
            order = await order_service.submit_limit(
                symbol=symbol,
                is_ask=is_ask,
                size_i=remaining,
                price_i=price_i,
                post_only=post_only,
                reduce_only=reduce_only,
                trace_id=trace_id,
            )
            wait_budget = max(0.0, min(interval, deadline - time.monotonic()))
            try:
                update = await asyncio.wait_for(order.wait_final(), timeout=wait_budget)
            except asyncio.TimeoutError:
                await order_service.cancel(symbol, order.client_order_index)
                try:
                    update = await asyncio.wait_for(order.wait_final(), timeout=self._cancel_wait_secs)
                except asyncio.TimeoutError:
                    update = order.snapshot()
                    update.info = {**update.info, "cancel_wait_timeout": True}
                records.append(
                    TrackingAttempt(
                        attempt=attempt,
                        client_order_index=order.client_order_index,
                        price_i=price_i,
                        state=update.state,
                        info={**update.info, "timeout": True},
                    )
                )
                filled = self._extract_filled(update.info)
                cumulative_filled += filled
                remaining = base_amount_i - cumulative_filled
                if remaining <= max(1, int(base_amount_i * 0.0001)):
                    return TrackingLimitOrder(order, records, cumulative_filled)
                continue
            records.append(
                TrackingAttempt(
                    attempt=attempt,
                    client_order_index=order.client_order_index,
                    price_i=price_i,
                    state=update.state,
                    info=update.info,
                )
            )
            if update.state == OrderState.FILLED:
                cumulative_filled += remaining
                return TrackingLimitOrder(order, records, cumulative_filled)
            if update.state == OrderState.FAILED:
                raise RuntimeError(f"tracking limit attempt failed: {update.info}")
            filled = self._extract_filled(update.info)
            cumulative_filled += filled
            remaining = base_amount_i - cumulative_filled
            if remaining <= max(1, int(base_amount_i * 0.0001)):
                return TrackingLimitOrder(order, records, cumulative_filled)
            if remaining <= 0:
                return TrackingLimitOrder(order, records, cumulative_filled)

    @staticmethod
    def _extract_filled(info: Dict[str, object]) -> int:
        candidates = (
            info.get("filled_base_i"),
            info.get("filled_size_i"),
            info.get("filled"),
        )
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                return int(float(candidate))
            except (ValueError, TypeError):
                continue
        return 0


__all__ = [
    "TrackingLimitEngine",
    "TrackingLimitOrder",
    "TrackingLimitTimeoutError",
    "TrackingAttempt",
]
