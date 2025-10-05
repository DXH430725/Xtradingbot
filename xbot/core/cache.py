from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Tuple, Optional


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
        self.balances: Dict[str, Tuple[float, float, float]] = {}

    async def set_top(self, symbol: str, bid: float | None, ask: float | None) -> None:
        async with self._lock:
            self.orderbooks[symbol] = (bid, ask, time.time())

    async def add_trade(self, symbol: str, trade: dict) -> None:
        async with self._lock:
            self.trades[symbol].append(trade)

    async def set_position(self, symbol: str, pos: float) -> None:
        async with self._lock:
            self.positions[symbol] = PositionInfo(symbol=symbol, position=pos, ts=time.time())

    async def set_balance(self, asset: str, total: float, available: Optional[float] = None) -> None:
        async with self._lock:
            avail = available if available is not None else total
            self.balances[asset.upper()] = (total, avail, time.time())

    async def set_balances(self, payload: Dict[str, Tuple[float, float] | float]) -> None:
        # payload: {"USDC": (total, available)} or {"USDC": total}
        async with self._lock:
            ts = time.time()
            for k, v in payload.items():
                if isinstance(v, tuple):
                    total, available = float(v[0]), float(v[1])
                else:
                    total, available = float(v), float(v)
                self.balances[k.upper()] = (total, available, ts)

    async def snapshot_positions(self) -> Dict[str, dict]:
        async with self._lock:
            return {k: {"position": v.position, "ts": v.ts} for k, v in self.positions.items()}

    async def snapshot_trades(self, symbol: Optional[str] = None, limit: int = 10) -> Dict[str, list]:
        async with self._lock:
            if symbol is None:
                return {k: list(self.trades[k])[-limit:] for k in self.trades}
            return {symbol: list(self.trades[symbol])[-limit:]}

    async def snapshot_balances(self) -> Dict[str, dict]:
        async with self._lock:
            return {k: {"total": v[0], "available": v[1], "ts": v[2]} for k, v in self.balances.items()}
