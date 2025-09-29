"""Thin execution router dispatching to specialized services.

This router eliminates all state caching and persistence from ExecutionLayer,
delegating to focused services that manage their own state.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, List

from .services.symbol import SymbolService
from .services.order import OrderService
from .services.position import PositionService
from .services.risk import RiskService


class ExecutionRouter:
    """Thin router dispatching execution requests to specialized services.

    No state caching, no persistent dictionaries - just routing and aggregation.
    """

    def __init__(self, *, logger: Optional[logging.Logger] = None) -> None:
        self.log = logger or logging.getLogger("mm_bot.execution.router")

        # Initialize services
        self._symbol_service = SymbolService(logger=self.log)
        self._order_service = OrderService(logger=self.log)
        self._position_service = PositionService(logger=self.log)
        self._risk_service = RiskService(logger=self.log)

        # Connector registry (no state caching)
        self._connectors: Dict[str, Any] = {}

    def register_connector(
        self,
        venue: str,
        connector: Any,
        *,
        coi_limit: Optional[int] = None,
        api_key_index: Optional[int] = None,
    ) -> None:
        """Register connector with all services."""
        key = venue.lower()
        self._connectors[key] = connector

        # Register with services
        self._symbol_service.register_connector(key, connector)
        self._order_service.register_connector(key, connector, coi_limit=coi_limit, api_key_index=api_key_index)
        self._position_service.register_connector(key, connector)
        self._risk_service.register_connector(key, connector)

    def register_symbol(self, canonical: str, **venues: str) -> None:
        """Register symbol mapping."""
        return self._symbol_service.register_symbol(canonical, **venues)

    # Symbol service methods
    async def size_scale(self, venue: str, canonical_symbol: str) -> int:
        return await self._symbol_service.get_size_scale(venue, canonical_symbol)

    async def min_size_i(self, venue: str, canonical_symbol: str) -> int:
        return await self._symbol_service.get_min_size_i(venue, canonical_symbol)

    # Order service methods
    async def market_order(
        self,
        venue: str,
        canonical_symbol: str,
        *,
        size_i: int,
        is_ask: bool,
        reduce_only: int = 0,
        max_slippage: Optional[float] = None,
        attempts: int = 1,
        retry_delay: float = 0.0,
        wait_timeout: float = 30.0,
        label: Optional[str] = None,
    ) -> Any:
        # Pre-order risk check
        await self._risk_service.check_pre_order_risk(venue, canonical_symbol, size_i, is_ask)

        return await self._order_service.market_order(
            venue=venue,
            canonical_symbol=canonical_symbol,
            size_i=size_i,
            is_ask=is_ask,
            reduce_only=reduce_only,
            max_slippage=max_slippage,
            attempts=attempts,
            retry_delay=retry_delay,
            wait_timeout=wait_timeout,
            label=label,
        )

    async def limit_order(
        self,
        venue: str,
        canonical_symbol: str,
        *,
        base_amount_i: int,
        is_ask: bool,
        **kwargs: Any,
    ) -> Any:
        # Pre-order risk check
        await self._risk_service.check_pre_order_risk(venue, canonical_symbol, base_amount_i, is_ask)

        return await self._order_service.limit_order(
            venue=venue,
            canonical_symbol=canonical_symbol,
            base_amount_i=base_amount_i,
            is_ask=is_ask,
            **kwargs,
        )

    # Position service methods
    async def position(self, venue: str, canonical_symbol: str) -> float:
        return await self._position_service.get_position(venue, canonical_symbol)

    async def collateral(self, venue: str) -> float:
        return await self._position_service.get_collateral(venue)

    async def confirm_position(
        self,
        venue: str,
        canonical_symbol: str,
        *,
        target: float,
        tolerance: float,
        timeout: float = 5.0,
        poll_interval: float = 0.2,
    ) -> Optional[float]:
        return await self._position_service.confirm_position(
            venue=venue,
            canonical_symbol=canonical_symbol,
            target=target,
            tolerance=tolerance,
            timeout=timeout,
            poll_interval=poll_interval,
        )

    async def rebalance(
        self,
        venue: str,
        canonical_symbol: str,
        *,
        target: float,
        tolerance: float,
        attempts: int = 3,
        retry_delay: float = 0.5,
    ) -> bool:
        return await self._position_service.rebalance(
            venue=venue,
            canonical_symbol=canonical_symbol,
            target=target,
            tolerance=tolerance,
            attempts=attempts,
            retry_delay=retry_delay,
        )

    async def plan_order_size(
        self,
        venue: str,
        canonical_symbol: str,
        *,
        leverage: float,
        min_collateral: float,
        collateral_buffer: float = 0.96,
    ) -> Optional[Dict[str, Any]]:
        return await self._position_service.plan_order_size(
            venue=venue,
            canonical_symbol=canonical_symbol,
            leverage=leverage,
            min_collateral=min_collateral,
            collateral_buffer=collateral_buffer,
        )

    async def unwind_all(
        self,
        *,
        canonical_symbol: str,
        tolerance: float = 1e-6,
        venues: Optional[List[str]] = None,
    ) -> Dict[str, bool]:
        return await self._position_service.unwind_all(
            canonical_symbol=canonical_symbol,
            tolerance=tolerance,
            venues=venues,
        )


# Backwards compatibility alias
ExecutionLayer = ExecutionRouter

__all__ = ["ExecutionRouter", "ExecutionLayer"]