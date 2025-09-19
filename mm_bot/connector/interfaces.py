from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple


class IConnector(Protocol):
    # Lifecycle / WS state
    def start(self, core: Any | None = None) -> None: ...
    def stop(self, core: Any | None = None) -> None: ...
    async def close(self) -> None: ...

    def set_event_handlers(
        self,
        *,
        on_order_filled: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_order_cancelled: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_trade: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_position_update: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None: ...

    async def start_ws_state(self) -> None: ...
    async def stop_ws_state(self) -> None: ...

    # Market / precision
    async def list_symbols(self) -> List[str]: ...
    async def get_market_id(self, symbol: str) -> int: ...
    async def get_price_size_decimals(self, symbol: str) -> Tuple[int, int]: ...
    async def get_min_order_size_i(self, symbol: str) -> int: ...

    # Order book
    async def get_top_of_book(self, symbol: str) -> Tuple[Optional[int], Optional[int], int]: ...

    # Account / orders / positions
    async def get_account_overview(self) -> Dict[str, Any]: ...
    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]: ...
    async def get_positions(self) -> List[Dict[str, Any]]: ...

    # Trading
    async def place_limit(
        self,
        symbol: str,
        client_order_index: int,
        base_amount: int,
        price: int,
        is_ask: bool,
        post_only: bool = False,
        reduce_only: int = 0,
    ): ...

    async def place_market(
        self,
        symbol: str,
        client_order_index: int,
        base_amount: int,
        is_ask: bool,
        reduce_only: int = 0,
    ): ...

    async def cancel_order(self, order_index: int, market_index: Optional[int] = None) -> Any: ...
    async def cancel_all(self) -> Any: ...

    # Misc
    async def best_effort_latency_ms(self) -> float: ...

