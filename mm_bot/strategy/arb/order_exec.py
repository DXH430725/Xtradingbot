"""Order execution helpers for arbitrage strategies."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

__all__ = [
    "OrderCompletion",
    "OrderTracker",
    "parse_backpack_event",
    "parse_lighter_event",
    "TrackingLimitExecutor",
]


@dataclass
class OrderCompletion:
    client_order_index: int
    status: str
    order_id: Optional[int] = None
    filled_size_i: Optional[int] = None
    remaining_size_i: Optional[int] = None
    reason: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in {"filled", "cancelled", "failed", "timeout"}


@dataclass
class _OrderContext:
    symbol: str
    target_size_i: int
    future: "asyncio.Future[OrderCompletion]"
    metadata: Dict[str, Any] = field(default_factory=dict)


class OrderTracker:
    """Track per-venue order lifecycle via WS event callbacks."""

    def __init__(
        self,
        venue: str,
        parser: Callable[[Dict[str, Any]], Optional[OrderCompletion]],
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.venue = venue
        self._parser = parser
        self.log = logger or logging.getLogger(f"mm_bot.strategy.order_tracker.{venue}")
        self._orders: Dict[int, _OrderContext] = {}

    def register(
        self,
        client_order_index: int,
        *,
        symbol: str,
        size_i: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "asyncio.Future[OrderCompletion]":
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[OrderCompletion]" = loop.create_future()
        self._orders[client_order_index] = _OrderContext(
            symbol=symbol,
            target_size_i=int(size_i),
            future=fut,
            metadata=dict(metadata or {}),
        )
        return fut

    def handle_event(self, payload: Dict[str, Any]) -> None:
        try:
            completion = self._parser(payload)
        except Exception:
            self.log.debug("failed to parse event", exc_info=True)
            return
        if completion is None or not completion.client_order_index:
            return
        ctx = self._orders.get(int(completion.client_order_index))
        if ctx is None:
            return
        if completion.status not in {"filled", "cancelled", "failed"}:
            # skip non-terminal statuses to avoid completing future too early
            return
        if completion.filled_size_i is None and completion.status == "filled":
            completion.filled_size_i = ctx.target_size_i
        if completion.remaining_size_i is None and completion.filled_size_i is not None:
            completion.remaining_size_i = max(ctx.target_size_i - int(completion.filled_size_i), 0)
        fut = ctx.future
        if not fut.done():
            fut.set_result(completion)
        self._orders.pop(int(completion.client_order_index), None)

    def fail(self, client_order_index: int, reason: str) -> None:
        ctx = self._orders.pop(int(client_order_index), None)
        if ctx is None:
            return
        fut = ctx.future
        if not fut.done():
            fut.set_result(
                OrderCompletion(
                    client_order_index=int(client_order_index),
                    status="failed",
                    reason=reason,
                    raw={},
                )
            )

    def timeout(self, client_order_index: int) -> None:
        ctx = self._orders.pop(int(client_order_index), None)
        if ctx is None:
            return
        fut = ctx.future
        if not fut.done():
            fut.set_result(
                OrderCompletion(
                    client_order_index=int(client_order_index),
                    status="timeout",
                    reason="limit_timeout",
                    raw={},
                )
            )

    def clear(self) -> None:
        for coi, ctx in list(self._orders.items()):
            fut = ctx.future
            if not fut.done():
                fut.set_result(
                    OrderCompletion(
                        client_order_index=int(coi),
                        status="failed",
                        reason="tracker_cleared",
                        raw={},
                    )
                )
        self._orders.clear()


def parse_backpack_event(payload: Dict[str, Any]) -> Optional[OrderCompletion]:
    client_keys = ("clientId", "clientID", "client_id")
    client_order_index = None
    for key in client_keys:
        if key in payload:
            try:
                client_order_index = int(payload[key])
                break
            except Exception:
                continue
    if not client_order_index:
        return None
    status_raw = str(payload.get("status") or payload.get("orderStatus") or "").lower()
    if "filled" in status_raw:
        status = "filled"
    elif "cancel" in status_raw or "closed" in status_raw:
        status = "cancelled"
    elif any(word in status_raw for word in ("reject", "fail", "error")):
        status = "failed"
    else:
        return OrderCompletion(client_order_index=client_order_index, status=status_raw or "pending", raw=payload)
    order_id = None
    for k in ("id", "orderId"):
        if payload.get(k) is not None:
            try:
                order_id = int(payload[k])
            except Exception:
                try:
                    order_id = int(str(payload[k]))
                except Exception:
                    order_id = None
            break
    reason = payload.get("reason") or payload.get("message") or payload.get("errorMessage")
    completion = OrderCompletion(
        client_order_index=client_order_index,
        status=status,
        order_id=order_id,
        reason=reason,
        raw=payload,
    )
    return completion


def parse_lighter_event(payload: Dict[str, Any]) -> Optional[OrderCompletion]:
    client_keys = (
        "client_order_index",
        "clientOrderIndex",
        "client-index",
    )
    client_order_index = None
    for key in client_keys:
        if key in payload:
            try:
                client_order_index = int(payload[key])
                break
            except Exception:
                continue
    if not client_order_index:
        return None
    status_raw = str(payload.get("status") or payload.get("order_status") or "").lower()
    if status_raw.startswith("filled"):
        status = "filled"
    elif status_raw.startswith("cancel") or status_raw.startswith("expired"):
        status = "cancelled"
    elif status_raw.startswith("reject") or status_raw.startswith("fail"):
        status = "failed"
    else:
        return OrderCompletion(client_order_index=client_order_index, status=status_raw or "pending", raw=payload)
    order_id = None
    for key in ("order_index", "orderIndex"):
        if payload.get(key) is not None:
            try:
                order_id = int(payload[key])
            except Exception:
                continue
            break
    reason = payload.get("cancel_reason") or payload.get("failure_reason") or payload.get("reason")
    completion = OrderCompletion(
        client_order_index=client_order_index,
        status=status,
        order_id=order_id,
        reason=reason,
        raw=payload,
    )
    return completion


@dataclass
class LegOrder:
    name: str
    connector: Any
    symbol: str
    size_i: int
    is_ask: bool
    reduce_only: int = 0


@dataclass
class LegExecutionResult:
    order: LegOrder
    success: bool
    filled_size_i: int
    attempts: int
    status: str
    reason: Optional[str] = None
    history: list[OrderCompletion] = field(default_factory=list)


class TrackingLimitExecutor:
    """Execute tracking limit orders with automatic re-post and optional market fallback."""

    def __init__(
        self,
        *,
        venue: str,
        connector: Any,
        order_tracker: OrderTracker,
        next_client_order_index: Callable[[], int],
        top_of_book_fetcher: Callable[[str], "asyncio.Future[Tuple[Optional[int], Optional[int], int]]"],
        cancel_by_coi: Callable[[int, str], "asyncio.Future[Any]"],
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.venue = venue
        self.connector = connector
        self.order_tracker = order_tracker
        self._next_client_order_index = next_client_order_index
        self._get_top_of_book = top_of_book_fetcher
        self._cancel_by_coi = cancel_by_coi
        self.log = logger or logging.getLogger(f"mm_bot.strategy.tracking_limit.{venue}")

    async def run(
        self,
        leg: LegOrder,
        *,
        wait_seconds: float = 10.0,
        max_retries: int = 3,
        allow_market_fallback: bool = True,
    ) -> LegExecutionResult:
        remaining = int(leg.size_i)
        attempts = 0
        history: list[OrderCompletion] = []
        last_reason: Optional[str] = None

        while remaining > 0 and attempts < max_retries:
            attempts += 1
            bid_i, ask_i, _scale = await self._get_top_of_book(leg.symbol)
            price_i = bid_i if leg.is_ask else ask_i
            if price_i is None:
                last_reason = "no_top_of_book"
                break
            coi = self._next_client_order_index()
            waiter = self.order_tracker.register(coi, symbol=leg.symbol, size_i=remaining)
            _, _, err = await self.connector.place_limit(
                leg.symbol,
                coi,
                remaining,
                price_i,
                is_ask=leg.is_ask,
                post_only=False,
                reduce_only=int(leg.reduce_only),
            )
            if err:
                self.order_tracker.fail(coi, str(err))
                last_reason = str(err)
                await asyncio.sleep(0.05)
                continue
            try:
                completion = await asyncio.wait_for(waiter, timeout=wait_seconds)
            except asyncio.TimeoutError:
                self.order_tracker.timeout(coi)
                try:
                    await asyncio.wait_for(self._cancel_by_coi(coi, leg.symbol), timeout=2.0)
                except Exception:
                    pass
                last_reason = "limit_timeout"
                continue
            history.append(completion)
            if completion.status == "filled":
                remaining = max(remaining - int(completion.filled_size_i or leg.size_i), 0)
                if remaining <= 0:
                    return LegExecutionResult(
                        order=leg,
                        success=True,
                        filled_size_i=leg.size_i,
                        attempts=attempts,
                        status="filled",
                        reason=None,
                        history=history,
                    )
                continue
            if completion.status == "cancelled":
                reason = (completion.reason or "cancelled").lower()
                last_reason = completion.reason or "cancelled"
                if "slippage" in reason or "price" in reason:
                    await asyncio.sleep(0.05)
                    continue
                continue
            if completion.status == "failed":
                last_reason = completion.reason or "failed"
                await asyncio.sleep(0.05)
                continue

        if remaining > 0 and allow_market_fallback:
            coi_mkt = self._next_client_order_index()
            _, _, err = await self.connector.place_market(
                leg.symbol,
                coi_mkt,
                remaining,
                is_ask=leg.is_ask,
                reduce_only=int(leg.reduce_only),
            )
            if err:
                last_reason = str(err)
                return LegExecutionResult(
                    order=leg,
                    success=False,
                    filled_size_i=leg.size_i - remaining,
                    attempts=attempts,
                    status="market_failed",
                    reason=last_reason,
                    history=history,
                )
            return LegExecutionResult(
                order=leg,
                success=True,
                filled_size_i=leg.size_i,
                attempts=attempts + 1,
                status="market_fallback",
                reason=None,
                history=history,
            )

        return LegExecutionResult(
            order=leg,
            success=False,
            filled_size_i=leg.size_i - remaining,
            attempts=attempts,
            status="limit_failed",
            reason=last_reason,
            history=history,
        )
