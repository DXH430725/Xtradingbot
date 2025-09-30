"""Combined market data service: symbol mapping + position aggregation.

Merges symbol_service.py and position_service.py to reduce file count.
Target: < 500 lines.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, List, Tuple


class MarketDataService:
    """Combined service for symbol mapping and position management."""

    def __init__(self, *, logger: Optional[logging.Logger] = None) -> None:
        self.log = logger or logging.getLogger("xbot.execution.market_data_service")

        # Connectors registry
        self._connectors: Dict[str, Any] = {}

        # Symbol mappings: canonical -> {venue -> venue_symbol}
        self._canonical_to_venue: Dict[str, Dict[str, str]] = {}

        # Cached metadata: (venue, symbol) -> metadata
        self._metadata_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}

        # Position cache: venue -> symbol -> position_data
        self._position_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}

        # Collateral cache: venue -> collateral_data
        self._collateral_cache: Dict[str, Dict[str, Any]] = {}

    def register_connector(
        self,
        venue: str,
        connector: Any,
        *,
        coi_limit: Optional[int] = None,
        api_key_index: Optional[int] = None,
    ) -> None:
        """Register connector for this venue."""
        key = venue.lower()
        self._connectors[key] = connector
        self.log.debug(f"Market data service registered connector: {key}")

    # === Symbol Service Methods ===

    def register_symbol(self, canonical: str, **venues: str) -> None:
        """Register symbol mappings for multiple venues."""
        canonical_key = canonical.upper()
        venue_map = self._canonical_to_venue.setdefault(canonical_key, {})
        for venue, venue_symbol in venues.items():
            venue_map[venue.lower()] = venue_symbol
        self.log.debug(f"Registered symbol mappings for {canonical}: {venues}")

    def map_symbol(self, canonical: str, venue: str) -> str:
        """Map canonical symbol to venue-specific format."""
        canonical_key = canonical.upper()
        venue_key = venue.lower()
        venue_map = self._canonical_to_venue.get(canonical_key, {})
        return venue_map.get(venue_key, canonical)

    async def get_size_scale(self, venue: str, canonical_symbol: str) -> int:
        """Get size scale (decimal places) for symbol at venue."""
        await self._ensure_metadata(venue, canonical_symbol)
        metadata = self._metadata_cache.get((venue.lower(), canonical_symbol.upper()), {})
        return metadata.get('size_scale', 6)

    async def get_min_size_i(self, venue: str, canonical_symbol: str) -> int:
        """Get minimum order size in integer format."""
        await self._ensure_metadata(venue, canonical_symbol)
        metadata = self._metadata_cache.get((venue.lower(), canonical_symbol.upper()), {})
        return metadata.get('min_size_i', 1)

    async def get_price_size_decimals(self, venue: str, canonical_symbol: str) -> Tuple[int, int]:
        """Get price and size decimal places."""
        await self._ensure_metadata(venue, canonical_symbol)
        metadata = self._metadata_cache.get((venue.lower(), canonical_symbol.upper()), {})
        return (metadata.get('price_scale', 2), metadata.get('size_scale', 6))

    async def _ensure_metadata(self, venue: str, canonical_symbol: str) -> None:
        """Ensure metadata is loaded for symbol."""
        cache_key = (venue.lower(), canonical_symbol.upper())
        if cache_key in self._metadata_cache:
            return

        connector = self._connectors.get(venue.lower())
        if not connector:
            self.log.warning(f"No connector for venue {venue}")
            return

        venue_symbol = self.map_symbol(canonical_symbol, venue)
        try:
            if hasattr(connector, 'get_price_size_decimals'):
                price_scale, size_scale = await connector.get_price_size_decimals(venue_symbol)
            else:
                price_scale, size_scale = 2, 6

            if hasattr(connector, 'get_min_size_i'):
                min_size_i = await connector.get_min_size_i(venue_symbol)
            else:
                min_size_i = 1

            self._metadata_cache[cache_key] = {
                'price_scale': price_scale, 'size_scale': size_scale,
                'min_size_i': min_size_i, 'venue_symbol': venue_symbol
            }
        except Exception as e:
            self.log.error(f"Failed to load metadata for {venue}:{canonical_symbol}: {e}")
            self._metadata_cache[cache_key] = {
                'price_scale': 2, 'size_scale': 6, 'min_size_i': 1, 'venue_symbol': venue_symbol
            }

    # === Position Service Methods ===

    async def get_positions(self, venue: Optional[str] = None) -> Any:
        """Get positions for venue or aggregated across all venues."""
        if venue:
            return await self._get_venue_positions(venue)
        else:
            return await self._get_aggregated_positions()

    async def _get_venue_positions(self, venue: str) -> List[Dict[str, Any]]:
        """Get positions for specific venue."""
        venue_key = venue.lower()
        connector = self._connectors.get(venue_key)
        if not connector:
            self.log.warning(f"No connector for venue {venue}")
            return []

        try:
            positions = await connector.get_positions() if hasattr(connector, 'get_positions') else []
            venue_cache = self._position_cache.setdefault(venue_key, {})
            for pos in positions:
                symbol = pos.get('symbol', '')
                if symbol:
                    venue_cache[symbol.upper()] = pos
            return positions
        except Exception as e:
            self.log.error(f"Failed to get positions for {venue}: {e}")
            return []

    async def _get_aggregated_positions(self) -> Dict[str, Any]:
        """Get aggregated positions across all venues."""
        aggregated = {}
        for venue_key in self._connectors.keys():
            try:
                positions = await self._get_venue_positions(venue_key)
                for pos in positions:
                    symbol = pos.get('symbol', '').upper()
                    if not symbol:
                        continue
                    size = float(pos.get('size', 0))
                    if symbol not in aggregated:
                        aggregated[symbol] = {'symbol': symbol, 'total_size': 0.0, 'venues': {}}
                    aggregated[symbol]['total_size'] += size
                    aggregated[symbol]['venues'][venue_key] = {
                        'size': size, 'value': pos.get('value', 0),
                        'unrealized_pnl': pos.get('unrealized_pnl', 0)
                    }
            except Exception as e:
                self.log.error(f"Failed to aggregate positions for {venue_key}: {e}")
        return aggregated

    async def get_collateral(self, venue: str) -> float:
        """Get available collateral for venue."""
        venue_key = venue.lower()
        connector = self._connectors.get(venue_key)
        if not connector:
            return 0.0

        try:
            if hasattr(connector, 'get_account_overview'):
                account = await connector.get_account_overview()
                return float(account.get('available_balance', 0.0))
            return 0.0
        except Exception as e:
            self.log.error(f"Failed to get collateral for {venue}: {e}")
            return 0.0

    async def get_net_exposure(self, symbol: str) -> Dict[str, Any]:
        """Get net exposure for symbol across all venues."""
        symbol_key = symbol.upper()
        total_exposure = 0.0
        venue_exposures = {}

        for venue_key in self._connectors.keys():
            try:
                positions = await self._get_venue_positions(venue_key)
                venue_exposure = sum(float(pos.get('size', 0))
                                   for pos in positions
                                   if pos.get('symbol', '').upper() == symbol_key)
                venue_exposures[venue_key] = venue_exposure
                total_exposure += venue_exposure
            except Exception as e:
                self.log.error(f"Failed to calculate exposure for {venue_key}:{symbol}: {e}")
                venue_exposures[venue_key] = 0.0

        return {
            'symbol': symbol,
            'net_exposure': total_exposure,
            'venue_exposures': venue_exposures
        }

    def clear_cache(self, venue: Optional[str] = None) -> None:
        """Clear all caches."""
        if venue:
            venue_key = venue.lower()
            keys_to_remove = [k for k in self._metadata_cache.keys() if k[0] == venue_key]
            for key in keys_to_remove:
                del self._metadata_cache[key]
            self._position_cache.pop(venue_key, None)
            self._collateral_cache.pop(venue_key, None)
        else:
            self._metadata_cache.clear()
            self._position_cache.clear()
            self._collateral_cache.clear()
        self.log.debug(f"Cleared caches for venue: {venue or 'all'}")

    def get_venue_symbol(self, canonical: str, venue: str) -> str:
        """Get cached venue symbol if available."""
        cache_key = (venue.lower(), canonical.upper())
        metadata = self._metadata_cache.get(cache_key, {})
        return metadata.get('venue_symbol', self.map_symbol(canonical, venue))