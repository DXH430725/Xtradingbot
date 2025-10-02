from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .market_data_service import MarketDataService
from .position_service import PositionService


class RiskViolationError(Exception):
    """Raised when requested action would violate a risk constraint."""


@dataclass(slots=True)
class RiskLimits:
    max_position: Optional[Decimal] = None
    max_notional: Optional[Decimal] = None


class RiskService:
    def __init__(
        self,
        *,
        market_data: MarketDataService,
        position_service: PositionService,
        limits: Optional[RiskLimits] = None,
    ) -> None:
        self._market_data = market_data
        self._position_service = position_service
        self._limits = limits or RiskLimits()

    async def validate_order(
        self,
        *,
        symbol: str,
        size_i: int,
        is_ask: bool,
        price_i: Optional[int] = None,
    ) -> None:
        await self._market_data.ensure_min_size(symbol, size_i)
        if self._limits.max_position is None and self._limits.max_notional is None:
            return
        price_decimals, size_decimals = await self._market_data.get_price_size_decimals(symbol)
        size = Decimal(size_i) / (Decimal(10) ** size_decimals)
        if self._limits.max_position is not None:
            existing = await self._position_service.get_position(symbol)
            net_base = existing.base_qty if existing else Decimal(0)
            future_base = net_base - size if is_ask else net_base + size
            if abs(future_base) > self._limits.max_position:
                raise RiskViolationError(
                    f"net base {future_base} exceeds limit {self._limits.max_position} for {symbol}"
                )
        if self._limits.max_notional is not None:
            if price_i is None:
                bid_i, ask_i, _scale = await self._market_data.get_top_of_book(symbol)
                reference = ask_i if not is_ask else bid_i
                if reference is None:
                    raise RiskViolationError("unable to determine reference price for notional risk check")
                price_i = reference
            price = Decimal(price_i) / (Decimal(10) ** price_decimals)
            notional = price * size
            if notional > self._limits.max_notional:
                raise RiskViolationError(
                    f"order notional {notional} exceeds limit {self._limits.max_notional}"
                )


__all__ = ["RiskService", "RiskLimits", "RiskViolationError"]
