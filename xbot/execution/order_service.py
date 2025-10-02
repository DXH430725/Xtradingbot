from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional

from connector.interface import IConnector

from .market_data_service import MarketDataService
from .models import Order, OrderEvent, OrderState
from .risk_service import RiskService
from .tracking_limit import TrackingLimitEngine, TrackingLimitOrder
from ..utils.idgen import ClientOrderIdGenerator


@dataclass(slots=True)
class OrderUpdatePayload:
    client_order_index: int
    state: OrderState
    exchange_order_id: Optional[str] = None
    info: Dict[str, object] = field(default_factory=dict)


class UnknownOrderError(KeyError):
    pass


class OrderService:
    """Centralised order flow coordination for a single venue."""

    def __init__(
        self,
        *,
        connector: IConnector,
        market_data: MarketDataService,
        risk_service: RiskService,
        tracking_engine: TrackingLimitEngine,
        log_root: Path | None = None,
    ) -> None:
        self._connector = connector
        self._market_data = market_data
        self._risk = risk_service
        self._tracking = tracking_engine
        self._log_root = log_root or Path("logs/orders")
        self._generator = ClientOrderIdGenerator()
        self._orders: Dict[int, Order] = {}
        self._lock = asyncio.Lock()

    async def _register(self, order: Order) -> None:
        async with self._lock:
            self._orders[order.client_order_index] = order

    async def _get(self, client_order_index: int) -> Order:
        async with self._lock:
            if client_order_index not in self._orders:
                raise UnknownOrderError(client_order_index)
            return self._orders[client_order_index]

    async def submit_limit(
        self,
        *,
        symbol: str,
        is_ask: bool,
        size_i: Optional[int] = None,
        size: Optional[Decimal | float | str] = None,
        price_i: Optional[int] = None,
        price: Optional[Decimal | float | str] = None,
        client_order_index: Optional[int] = None,
        post_only: bool = False,
        reduce_only: int = 0,
        trace_id: Optional[str] = None,
    ) -> Order:
        if size_i is None and size is None:
            raise ValueError("size_i or size must be provided")
        if price_i is None and price is None:
            raise ValueError("price_i or price must be provided")
        if size_i is None:
            size_i = await self._market_data.to_size_i(symbol, size)
        if price_i is None:
            price_i = await self._market_data.to_price_i(symbol, price)
        await self._risk.validate_order(symbol=symbol, size_i=size_i, is_ask=is_ask, price_i=price_i)
        coi = client_order_index or self._generator.next()
        venue_symbol = self._market_data.resolve_symbol(symbol)
        order = Order(
            venue=self._connector.venue,
            symbol=symbol,
            client_order_index=coi,
            is_ask=is_ask,
            log_dir=self._log_root,
            trace_id=trace_id,
        )
        await self._register(order)
        await order.apply_update(
            OrderEvent(
                state=OrderState.SUBMITTING,
                info={
                    "size_i": size_i,
                    "price_i": price_i,
                    "is_ask": is_ask,
                    "symbol": symbol,
                },
            )
        )
        try:
            exchange_order_id = await self._connector.submit_limit_order(
                symbol=venue_symbol,
                client_order_index=coi,
                base_amount=size_i,
                price=price_i,
                is_ask=is_ask,
                post_only=post_only,
                reduce_only=reduce_only,
            )
        except Exception as exc:
            await order.apply_update(
                OrderEvent(
                    state=OrderState.FAILED,
                    info={"error": str(exc)}
                )
            )
            raise
        await order.apply_update(
            OrderEvent(
                state=OrderState.OPEN,
                info={
                    "exchange_order_id": exchange_order_id,
                    "size_i": size_i,
                    "price_i": price_i,
                },
            ),
            exchange_order_id=exchange_order_id,
        )
        return order

    async def submit_market(
        self,
        *,
        symbol: str,
        is_ask: bool,
        size_i: Optional[int] = None,
        size: Optional[Decimal | float | str] = None,
        client_order_index: Optional[int] = None,
        reduce_only: int = 0,
        trace_id: Optional[str] = None,
    ) -> Order:
        if size_i is None and size is None:
            raise ValueError("size_i or size must be provided")
        if size_i is None:
            size_i = await self._market_data.to_size_i(symbol, size)
        await self._risk.validate_order(symbol=symbol, size_i=size_i, is_ask=is_ask)
        coi = client_order_index or self._generator.next()
        venue_symbol = self._market_data.resolve_symbol(symbol)
        order = Order(
            venue=self._connector.venue,
            symbol=symbol,
            client_order_index=coi,
            is_ask=is_ask,
            log_dir=self._log_root,
            trace_id=trace_id,
        )
        await self._register(order)
        await order.apply_update(
            OrderEvent(
                state=OrderState.SUBMITTING,
                info={"size_i": size_i, "is_ask": is_ask, "symbol": symbol},
            )
        )
        try:
            exchange_order_id = await self._connector.submit_market_order(
                symbol=venue_symbol,
                client_order_index=coi,
                size_i=size_i,
                is_ask=is_ask,
                reduce_only=reduce_only,
            )
        except Exception as exc:
            await order.apply_update(
                OrderEvent(
                    state=OrderState.FAILED,
                    info={"error": str(exc)},
                )
            )
            raise
        await order.apply_update(
            OrderEvent(
                state=OrderState.OPEN,
                info={
                    "exchange_order_id": exchange_order_id,
                    "size_i": size_i,
                },
            ),
            exchange_order_id=exchange_order_id,
        )
        return order

    async def cancel(self, symbol: str, client_order_index: int) -> None:
        order = await self._get(client_order_index)
        venue_symbol = self._market_data.resolve_symbol(symbol)
        await self._connector.cancel_by_client_id(venue_symbol, client_order_index)
        await order.apply_update(
            OrderEvent(
                state=OrderState.CANCELLED,
                info={"symbol": symbol, "client_order_index": client_order_index},
            )
        )

    async def place_tracking_limit(
        self,
        *,
        symbol: str,
        base_amount_i: int,
        is_ask: bool,
        **kwargs: object,
    ) -> TrackingLimitOrder:
        await self._risk.validate_order(symbol=symbol, size_i=base_amount_i, is_ask=is_ask)
        return await self._tracking.place(
            order_service=self,
            connector=self._connector,
            symbol=symbol,
            base_amount_i=base_amount_i,
            is_ask=is_ask,
            **kwargs,
        )

    async def ingest_update(self, payload: OrderUpdatePayload) -> Order:
        order = await self._get(payload.client_order_index)
        await order.apply_update(
            OrderEvent(
                state=payload.state,
                info=payload.info,
            ),
            exchange_order_id=payload.exchange_order_id,
        )
        return order

    async def fetch_order(self, symbol: str, client_order_index: int) -> Order:
        venue_symbol = self._market_data.resolve_symbol(symbol)
        data = await self._connector.get_order(venue_symbol, client_order_index)
        state_str = (data.get("state") or data.get("status") or "").lower()
        if not state_str:
            raise ValueError("connector get_order response missing state/status")
        state = OrderState(state_str)
        payload = OrderUpdatePayload(
            client_order_index=client_order_index,
            state=state,
            exchange_order_id=data.get("order_id") or data.get("exchange_order_id"),
            info=data,
        )
        return await self.ingest_update(payload)


__all__ = ["OrderService", "OrderUpdatePayload", "UnknownOrderError"]
