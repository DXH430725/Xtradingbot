"""Thin execution router dispatching to specialized services.

The router eliminates all state caching and persistence,
delegating to focused services that manage their own state.
Designed to stay < 200 lines as per architecture constraints.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, List

from .services.market_data_service import MarketDataService
from .services.order_service import OrderService
from .services.risk_service import RiskService


class ExecutionRouter:
    """Thin router dispatching execution requests to specialized services.

    No state caching, no persistent dictionaries - just routing and aggregation.
    """

    def __init__(self, *, logger: Optional[logging.Logger] = None) -> None:
        self.log = logger or logging.getLogger("xbot.execution.router")

        # Initialize services
        self.services = {
            'market': MarketDataService(logger=self.log),
            'order': OrderService(logger=self.log),
            'risk': RiskService(logger=self.log)
        }

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

        # Register with all services
        for service in self.services.values():
            if hasattr(service, 'register_connector'):
                service.register_connector(key, connector,
                                         coi_limit=coi_limit,
                                         api_key_index=api_key_index)

    def register_symbol(self, canonical: str, **venues: str) -> None:
        """Register symbol mapping."""
        self.services['market'].register_symbol(canonical, **venues)

    # Delegated methods - thin routing only
    async def limit_order(
        self,
        venue: str,
        symbol: str,
        *,
        size_i: int,
        price_i: int,
        is_ask: bool,
        tracking: bool = False,
        **kwargs
    ) -> Any:
        """Route limit order request to order service."""
        return await self.services['order'].limit_order(
            venue=venue,
            symbol=symbol,
            size_i=size_i,
            price_i=price_i,
            is_ask=is_ask,
            tracking=tracking,
            **kwargs
        )

    async def market_order(
        self,
        venue: str,
        symbol: str,
        *,
        size_i: int,
        is_ask: bool,
        **kwargs
    ) -> Any:
        """Route market order request to order service."""
        return await self.services['order'].market_order(
            venue=venue,
            symbol=symbol,
            size_i=size_i,
            is_ask=is_ask,
            **kwargs
        )

    async def cancel(self, venue: str, symbol: str, coi: int) -> None:
        """Route cancel request to order service."""
        return await self.services['order'].cancel(venue, symbol, coi)

    async def position(self, venue: Optional[str] = None) -> Any:
        """Route position query to position service."""
        return await self.services['market'].get_positions(venue)

    async def collateral(self, venue: str) -> float:
        """Route collateral query to position service."""
        return await self.services['market'].get_collateral(venue)

    async def size_scale(self, venue: str, canonical_symbol: str) -> int:
        """Route size scale query to symbol service."""
        return await self.services['market'].get_size_scale(venue, canonical_symbol)

    async def min_size_i(self, venue: str, canonical_symbol: str) -> int:
        """Route min size query to symbol service."""
        return await self.services['market'].get_min_size_i(venue, canonical_symbol)

    async def check_risk(self, venue: str, symbol: str, size_i: int, **kwargs) -> bool:
        """Route risk check to risk service."""
        return await self.services['risk'].check_order_risk(venue, symbol, size_i, **kwargs)

    def get_connector(self, venue: str) -> Any:
        """Get connector for venue."""
        return self._connectors.get(venue.lower())