from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from connector.interface import IConnector


@dataclass
class SymbolMeta:
    price_decimals: int
    size_decimals: int
    min_size_i: int
    top_bid: Optional[int] = None
    top_ask: Optional[int] = None


class StubConnector(IConnector):
    def __init__(self, venue: str = "stub", meta: Optional[Dict[str, SymbolMeta]] = None) -> None:
        self.venue = venue
        self._meta = meta or {}
        self.submitted: List[Dict[str, Any]] = []
        self.cancelled: List[Tuple[str, int]] = []

    async def start(self) -> None:  # pragma: no cover
        return None

    async def stop(self) -> None:  # pragma: no cover
        return None

    async def get_price_size_decimals(self, symbol: str) -> Tuple[int, int]:
        meta = self._meta[symbol]
        return meta.price_decimals, meta.size_decimals

    async def get_min_size_i(self, symbol: str) -> int:
        return self._meta[symbol].min_size_i

    async def get_top_of_book(self, symbol: str) -> Tuple[Optional[int], Optional[int], int]:
        meta = self._meta[symbol]
        scale = 10 ** meta.price_decimals
        return meta.top_bid, meta.top_ask, scale

    async def submit_limit_order(
        self,
        *,
        symbol: str,
        client_order_index: int,
        base_amount: int,
        price: int,
        is_ask: bool,
        post_only: bool = False,
        reduce_only: int = 0,
    ) -> str:
        self.submitted.append(
            {
                "symbol": symbol,
                "client_order_index": client_order_index,
                "base_amount": base_amount,
                "price": price,
                "is_ask": is_ask,
            }
        )
        return f"order-{client_order_index}"

    async def submit_market_order(
        self,
        *,
        symbol: str,
        client_order_index: int,
        size_i: int,
        is_ask: bool,
        reduce_only: int = 0,
    ) -> str:
        self.submitted.append(
            {
                "symbol": symbol,
                "client_order_index": client_order_index,
                "base_amount": size_i,
                "is_ask": is_ask,
                "kind": "market",
            }
        )
        return f"market-{client_order_index}"

    async def cancel_by_client_id(self, symbol: str, client_order_index: int) -> None:
        self.cancelled.append((symbol, client_order_index))

    async def get_order(self, symbol: str, client_order_index: int) -> Dict[str, Any]:  # pragma: no cover
        return {"state": "open", "client_order_index": client_order_index}

    async def get_positions(self) -> List[Dict[str, Any]]:  # pragma: no cover
        return []

    async def get_margin(self) -> Dict[str, Any]:  # pragma: no cover
        return {}


class FakeOrder:
    def __init__(self, *, final_event: asyncio.Future, client_order_index: int, default_state: Any) -> None:
        self._future = final_event
        self.client_order_index = client_order_index
        self.state = default_state

    async def wait_final(self, timeout: Optional[float] = None):
        return await asyncio.wait_for(asyncio.shield(self._future), timeout=timeout)

    def snapshot(self):
        return self._future.result() if self._future.done() else None


__all__ = ["StubConnector", "SymbolMeta", "FakeOrder"]
