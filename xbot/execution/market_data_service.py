from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, getcontext
from typing import Dict, Mapping, Optional, Tuple

from xbot.connector.interface import IConnector

getcontext().prec = 28


@dataclass(slots=True)
class SymbolSpec:
    canonical: str
    venue_symbol: str


class UnknownSymbolError(KeyError):
    """Raised when a canonical symbol is not configured for the active venue."""


class MarketDataService:
    """Resolves canonical symbols, precision and conversion helpers."""

    def __init__(
        self,
        *,
        connector: IConnector,
        symbol_map: Mapping[str, str],
    ) -> None:
        self._connector = connector
        self._symbol_map: Dict[str, SymbolSpec] = {
            canonical.upper(): SymbolSpec(canonical=canonical.upper(), venue_symbol=venue)
            for canonical, venue in symbol_map.items()
        }
        self._decimal_cache: Dict[str, Tuple[int, int]] = {}
        self._min_size_cache: Dict[str, int] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _canonical_key(self, symbol: str) -> str:
        key = symbol.upper()
        if key not in self._symbol_map:
            raise UnknownSymbolError(symbol)
        return key

    def resolve_symbol(self, symbol: str) -> str:
        key = self._canonical_key(symbol)
        return self._symbol_map[key].venue_symbol

    async def get_price_size_decimals(self, symbol: str) -> Tuple[int, int]:
        key = self._canonical_key(symbol)
        if key in self._decimal_cache:
            return self._decimal_cache[key]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._decimal_cache:
                return self._decimal_cache[key]
            venue_symbol = self._symbol_map[key].venue_symbol
            decimals = await self._connector.get_price_size_decimals(venue_symbol)
            self._decimal_cache[key] = decimals
            return decimals

    async def get_min_size_i(self, symbol: str) -> int:
        key = self._canonical_key(symbol)
        if key in self._min_size_cache:
            return self._min_size_cache[key]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._min_size_cache:
                return self._min_size_cache[key]
            venue_symbol = self._symbol_map[key].venue_symbol
            minimum = await self._connector.get_min_size_i(venue_symbol)
            self._min_size_cache[key] = minimum
            return minimum

    async def to_price_i(self, symbol: str, price: Decimal | float | str) -> int:
        price_decimals, _ = await self.get_price_size_decimals(symbol)
        scale = Decimal(10) ** price_decimals
        value = Decimal(str(price)) * scale
        return int(value.to_integral_value(rounding=ROUND_DOWN))

    async def to_size_i(self, symbol: str, size: Decimal | float | str) -> int:
        _, size_decimals = await self.get_price_size_decimals(symbol)
        scale = Decimal(10) ** size_decimals
        value = Decimal(str(size)) * scale
        return int(value.to_integral_value(rounding=ROUND_DOWN))

    async def ensure_min_size(self, symbol: str, size_i: int) -> None:
        minimum = await self.get_min_size_i(symbol)
        if size_i < minimum:
            raise ValueError(f"size {size_i} below minimum {minimum} for {symbol}")

    async def get_top_of_book(self, symbol: str) -> Tuple[Optional[int], Optional[int], int]:
        venue_symbol = self.resolve_symbol(symbol)
        bid_i, ask_i, scale = await self._connector.get_top_of_book(venue_symbol)
        return bid_i, ask_i, scale


__all__ = ["MarketDataService", "SymbolSpec", "UnknownSymbolError"]
