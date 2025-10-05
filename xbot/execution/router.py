from __future__ import annotations

from typing import Optional

from .order_service import OrderService
from .position_service import PositionService
from .risk_service import RiskService
from .tracking_limit import TrackingLimitOrder
from .models import Order
from .market_data_service import MarketDataService
from ..core.cache import MarketCache


class ExecutionRouter:
    """Thin facade exposing venue execution services to strategies."""

    def __init__(
        self,
        *,
        order_service: OrderService,
        position_service: PositionService,
        risk_service: RiskService,
        market_data: MarketDataService,
        cache: MarketCache | None = None,
    ) -> None:
        self._orders = order_service
        self._positions = position_service
        self._risk = risk_service
        self._market_data = market_data
        self._cache = cache

    @property
    def risk(self) -> RiskService:
        return self._risk

    @property
    def positions(self) -> PositionService:
        return self._positions

    @property
    def orders(self) -> OrderService:
        return self._orders

    @property
    def market_data(self) -> MarketDataService:
        return self._market_data

    @property
    def cache(self) -> MarketCache | None:
        return self._cache

    async def submit_limit(self, **kwargs) -> Order:
        return await self._orders.submit_limit(**kwargs)

    async def submit_market(self, **kwargs) -> Order:
        return await self._orders.submit_market(**kwargs)

    async def cancel(self, symbol: str, client_order_index: int) -> None:
        await self._orders.cancel(symbol, client_order_index)

    async def tracking_limit(self, **kwargs) -> TrackingLimitOrder:
        return await self._orders.place_tracking_limit(**kwargs)

    async def fetch_order(self, symbol: str, client_order_index: int) -> object:
        return await self._orders.fetch_order(symbol, client_order_index)

    async def fetch_margin(self) -> dict:
        # Access underlying connector for margin snapshot when available
        return await self._orders._connector.get_margin()  # type: ignore[attr-defined]


__all__ = ["ExecutionRouter"]
