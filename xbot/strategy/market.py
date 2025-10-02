from __future__ import annotations

from decimal import Decimal

from core.clock import WallClock
from execution.router import ExecutionRouter
from .base import Strategy, StrategyConfig


class MarketOrderStrategy(Strategy):
    """Places a single market order on start."""

    async def start(self) -> None:
        await super().start()
        size_i = await self.router.market_data.to_size_i(self.config.symbol, Decimal(str(self.config.qty)))
        await self.router.submit_market(
            symbol=self.config.symbol,
            is_ask=self.config.side == "sell",
            size_i=size_i,
            reduce_only=self.config.reduce_only,
        )


__all__ = ["MarketOrderStrategy"]
