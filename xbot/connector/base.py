from __future__ import annotations

import abc
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .interface import IConnector


class BaseConnector(IConnector, abc.ABC):
    """Shared HTTP utilities for venue connectors."""

    base_url: str

    def __init__(self, venue: str, *, timeout: float = 10.0) -> None:
        self.venue = venue
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self._timeout)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("connector not started")
        return self._client

    async def get_price_size_decimals(self, symbol: str) -> Tuple[int, int]:  # pragma: no cover - venue specific
        raise NotImplementedError

    async def get_min_size_i(self, symbol: str) -> int:  # pragma: no cover - venue specific
        raise NotImplementedError

    async def get_top_of_book(self, symbol: str) -> Tuple[Optional[int], Optional[int], int]:  # pragma: no cover
        raise NotImplementedError

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
    ) -> str:  # pragma: no cover - venue specific
        raise NotImplementedError

    async def submit_market_order(
        self,
        *,
        symbol: str,
        client_order_index: int,
        size_i: int,
        is_ask: bool,
        reduce_only: int = 0,
    ) -> str:  # pragma: no cover - venue specific
        raise NotImplementedError

    async def cancel_by_client_id(self, symbol: str, client_order_index: int) -> Dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def cancel_by_order_id(self, symbol: str, order_id: str) -> Dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def get_order(self, symbol: str, client_order_index: int) -> Dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def get_positions(self) -> List[Dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError

    async def get_margin(self) -> Dict[str, Any]:  # pragma: no cover
        return {}


__all__ = ["BaseConnector"]
