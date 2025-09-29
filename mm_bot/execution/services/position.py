"""Position service - position aggregation, valuation, and rebalancing."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from ..data import get_collateral, get_position, plan_order_size
from ..emergency import emergency_unwind
from ..positions import confirm_position, rebalance_position
from .symbol import SymbolService


class PositionService:
    """Service for position management and cross-venue aggregation.

    Handles position queries, rebalancing, emergency unwinding, and
    cross-venue position aggregation for net exposure calculation.
    """

    def __init__(self, *, logger: Optional[logging.Logger] = None) -> None:
        self.log = logger or logging.getLogger("mm_bot.execution.services.position")

        # Dependencies
        self._symbol_service = SymbolService(logger=self.log)

        # Connectors
        self._connectors: Dict[str, Any] = {}

    def register_connector(self, venue: str, connector: Any) -> None:
        """Register connector for position queries."""
        key = venue.lower()
        self._connectors[key] = connector
        self._symbol_service.register_connector(key, connector)

    async def get_position(self, venue: str, canonical_symbol: str) -> float:
        """Get position for specific venue and symbol."""
        connector = self._connectors.get(venue.lower())
        if not connector:
            raise ValueError(f"No connector registered for venue: {venue}")

        venue_symbol = self._symbol_service.map_symbol(canonical_symbol, venue)
        return await get_position(connector, venue_symbol)

    async def get_collateral(self, venue: str) -> float:
        """Get available collateral for venue."""
        connector = self._connectors.get(venue.lower())
        if not connector:
            raise ValueError(f"No connector registered for venue: {venue}")

        return await get_collateral(connector)

    async def get_net_position(self, canonical_symbol: str, venues: Optional[List[str]] = None) -> Dict[str, float]:
        """Get net position across multiple venues.

        Returns:
            Dict mapping venue to position, plus 'net' for total exposure
        """
        target_venues = venues or list(self._connectors.keys())
        positions = {}
        net_position = 0.0

        for venue in target_venues:
            try:
                position = await self.get_position(venue, canonical_symbol)
                positions[venue] = position
                net_position += position
            except Exception as e:
                self.log.error("Failed to get position for %s:%s - %s", venue, canonical_symbol, e)
                positions[venue] = 0.0

        positions['net'] = net_position
        return positions

    async def get_total_collateral(self, venues: Optional[List[str]] = None) -> Dict[str, float]:
        """Get collateral across multiple venues.

        Returns:
            Dict mapping venue to collateral, plus 'total' for sum
        """
        target_venues = venues or list(self._connectors.keys())
        collaterals = {}
        total_collateral = 0.0

        for venue in target_venues:
            try:
                collateral = await self.get_collateral(venue)
                collaterals[venue] = collateral
                total_collateral += collateral
            except Exception as e:
                self.log.error("Failed to get collateral for %s - %s", venue, e)
                collaterals[venue] = 0.0

        collaterals['total'] = total_collateral
        return collaterals

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
        """Confirm position reaches target within tolerance."""
        connector = self._connectors.get(venue.lower())
        if not connector:
            raise ValueError(f"No connector registered for venue: {venue}")

        venue_symbol = self._symbol_service.map_symbol(canonical_symbol, venue)
        return await confirm_position(
            connector,
            symbol=venue_symbol,
            target=target,
            tolerance=tolerance,
            timeout=timeout,
            poll_interval=poll_interval,
            logger=self.log,
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
        """Rebalance position to target."""
        connector = self._connectors.get(venue.lower())
        if not connector:
            raise ValueError(f"No connector registered for venue: {venue}")

        venue_symbol = self._symbol_service.map_symbol(canonical_symbol, venue)

        # Get required metadata
        size_scale = await self._symbol_service.get_size_scale(venue, canonical_symbol)
        min_size = await self._symbol_service.get_min_size_i(venue, canonical_symbol)

        # Import dependencies from router/execution layer
        from ..router import ExecutionRouter
        router = ExecutionRouter(logger=self.log)
        router.register_connector(venue, connector)

        # Get API key and lock
        api_key_index = getattr(router._order_service, '_api_keys', {}).get(venue.lower())
        lock = await router._order_service._get_lock(venue)

        return await rebalance_position(
            connector,
            symbol=venue_symbol,
            target=target,
            size_scale=size_scale,
            min_size_i=min_size,
            tolerance=tolerance,
            attempts=attempts,
            retry_delay=retry_delay,
            coi_manager=router._order_service.coi_manager,
            nonce_manager=router._order_service.nonce_manager,
            api_key_index=api_key_index,
            lock=lock,
            logger=self.log,
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
        """Plan order size based on available collateral and leverage."""
        connector = self._connectors.get(venue.lower())
        if not connector:
            raise ValueError(f"No connector registered for venue: {venue}")

        venue_symbol = self._symbol_service.map_symbol(canonical_symbol, venue)
        size_scale = await self._symbol_service.get_size_scale(venue, canonical_symbol)
        min_size = await self._symbol_service.get_min_size_i(venue, canonical_symbol)

        return await plan_order_size(
            connector,
            symbol=venue_symbol,
            leverage=leverage,
            min_collateral=min_collateral,
            size_scale=size_scale,
            min_size_i=min_size,
            collateral_buffer=collateral_buffer,
            logger=self.log,
        )

    async def unwind_all(
        self,
        *,
        canonical_symbol: str,
        tolerance: float = 1e-6,
        venues: Optional[List[str]] = None,
    ) -> Dict[str, bool]:
        """Emergency unwind all positions for symbol across venues."""
        target_venues = [v.lower() for v in (venues or list(self._connectors.keys()))]

        # Prepare connectors and metadata
        filtered_connectors: Dict[str, Any] = {}
        symbols: Dict[str, str] = {}
        size_scales: Dict[str, int] = {}
        locks: Dict[str, asyncio.Lock] = {}

        for venue in target_venues:
            connector = self._connectors.get(venue)
            if connector is None:
                continue

            mapped_symbol = self._symbol_service.map_symbol(canonical_symbol, venue)
            if mapped_symbol is None:
                continue

            filtered_connectors[venue] = connector
            symbols[venue] = mapped_symbol
            size_scales[venue] = await self._symbol_service.get_size_scale(venue, canonical_symbol)

            # Create lock for this operation
            locks[venue] = asyncio.Lock()

        if not filtered_connectors:
            return {}

        # Import dependencies
        from ..router import ExecutionRouter
        router = ExecutionRouter(logger=self.log)
        for venue, connector in filtered_connectors.items():
            router.register_connector(venue, connector)

        # Get API key indices
        api_key_indices = {
            k: getattr(router._order_service, '_api_keys', {}).get(k)
            for k in filtered_connectors
        }

        return await emergency_unwind(
            filtered_connectors,
            symbols=symbols,
            size_scales=size_scales,
            tolerance=tolerance,
            coi_manager=router._order_service.coi_manager,
            nonce_manager=router._order_service.nonce_manager,
            api_key_indices=api_key_indices,
            locks=locks,
            logger=self.log,
            notify=self._notify_emergency,
        )

    async def _notify_emergency(self, results: Dict[str, bool]) -> None:
        """Notify about emergency unwind results."""
        msg = "[Position] Emergency unwind results: " + ", ".join(
            f"{venue}:{'ok' if ok else 'fail'}" for venue, ok in results.items()
        )
        self.log.warning(msg)


__all__ = ["PositionService"]