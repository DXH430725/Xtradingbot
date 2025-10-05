from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Tuple


@dataclass
class PositionInfo:
    symbol: str
    position: float
    ts: float


class MarketCache:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.orderbooks: Dict[str, Tuple[float | None, float | None, float]] = {}
        self.trades: Dict[str, Deque[dict]] = defaultdict(lambda: deque(maxlen=100))
        self.positions: Dict[str, PositionInfo] = {}

    async def set_top(self, symbol: str, bid: float | None, ask: float | None) -> None:
        async with self._lock:
            self.orderbooks[symbol] = (bid, ask, time.time())

    async def add_trade(self, symbol: str, trade: dict) -> None:
        async with self._lock:
            self.trades[symbol].append(trade)

    async def set_position(self, symbol: str, pos: float) -> None:
        async with self._lock:
            self.positions[symbol] = PositionInfo(symbol=symbol, position=pos, ts=time.time())

