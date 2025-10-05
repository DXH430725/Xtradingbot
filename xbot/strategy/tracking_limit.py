from __future__ import annotations

from decimal import Decimal
from typing import Optional

from xbot.core.clock import WallClock
from xbot.execution.router import ExecutionRouter
from .base import Strategy, StrategyConfig


class TrackingLimitStrategy(Strategy):
    """Simple strategy that opens via tracking-limit then closes with a market order."""

    def __init__(
        self,
        *,
        router: ExecutionRouter,
        clock: WallClock,
        config: StrategyConfig,
        wait_after_fill: float = 5.0,
    ) -> None:
        super().__init__(router=router, clock=clock, config=config)
        self._wait_after_fill = wait_after_fill

    async def start(self) -> None:
        await super().start()
        symbol = self.config.symbol
        qty = Decimal(str(self.config.qty))
        is_ask_open = self.config.side == "sell"
        size_i = await self.router.market_data.to_size_i(symbol, qty)
        tracking = await self.router.tracking_limit(
            symbol=symbol,
            base_amount_i=size_i,
            is_ask=is_ask_open,
            price_offset_ticks=self.config.price_offset_ticks,
            interval_secs=self.config.interval_secs,
            timeout_secs=self.config.timeout_secs,
        )
        await tracking.wait_final()
        await self.clock.sleep(self._wait_after_fill)
        await self.router.submit_market(
            symbol=symbol,
            is_ask=not is_ask_open,
            size_i=size_i,
            reduce_only=1,
        )


__all__ = ["TrackingLimitStrategy"]
