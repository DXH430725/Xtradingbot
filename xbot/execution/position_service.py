from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, Optional


@dataclass(slots=True)
class PositionSnapshot:
    symbol: str
    base_qty: Decimal
    quote_value: Decimal
    notional: Decimal
    raw: Dict[str, object] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


class PositionService:
    """Aggregates position information from exchange feeds."""

    def __init__(self) -> None:
        self._positions: Dict[str, PositionSnapshot] = {}
        self._lock = asyncio.Lock()

    async def ingest(self, snapshot: PositionSnapshot) -> None:
        async with self._lock:
            self._positions[snapshot.symbol] = snapshot

    async def get_position(self, symbol: str) -> Optional[PositionSnapshot]:
        async with self._lock:
            return self._positions.get(symbol)

    async def all_positions(self) -> Iterable[PositionSnapshot]:
        async with self._lock:
            return list(self._positions.values())

    async def reset(self, symbol: Optional[str] = None) -> None:
        async with self._lock:
            if symbol is None:
                self._positions.clear()
            else:
                self._positions.pop(symbol, None)


__all__ = ["PositionService", "PositionSnapshot"]
