from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.clock import WallClock
from execution.router import ExecutionRouter


@dataclass(slots=True)
class StrategyConfig:
    symbol: str
    mode: str = "market"
    qty: float = 0.0
    side: str = "buy"
    reduce_only: int = 0
    price_offset_ticks: int = 0
    interval_secs: float = 10.0
    timeout_secs: float = 120.0


class Strategy:
    def __init__(self, *, router: ExecutionRouter, clock: WallClock, config: StrategyConfig) -> None:
        self._router = router
        self._clock = clock
        self._config = config
        self._running = False

    @property
    def router(self) -> ExecutionRouter:
        return self._router

    @property
    def clock(self) -> WallClock:
        return self._clock

    @property
    def config(self) -> StrategyConfig:
        return self._config

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False


__all__ = ["Strategy", "StrategyConfig"]
