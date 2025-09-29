"""Symbol service - unified symbol mapping, precision, and minimum size handling."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..symbols import SymbolMapper


class SymbolService:
    """Service for symbol mapping, precision, and minimum size management.

    Single source of truth for symbol-related metadata across venues.
    """

    def __init__(self, *, logger: Optional[logging.Logger] = None) -> None:
        self.log = logger or logging.getLogger("mm_bot.execution.services.symbol")
        self.symbols = SymbolMapper()

        # Connectors for querying venue-specific metadata
        self._connectors: Dict[str, Any] = {}

        # Cache for expensive operations (venue, canonical) -> value
        self._size_scales: Dict[tuple[str, str], int] = {}
        self._min_size_i: Dict[tuple[str, str], int] = {}

    def register_connector(self, venue: str, connector: Any) -> None:
        """Register connector for venue-specific queries."""
        self._connectors[venue.lower()] = connector

    def register_symbol(self, canonical: str, **venues: str) -> None:
        """Register symbol mapping across venues."""
        self.symbols.register(canonical, **venues)

    def map_symbol(self, canonical: str, venue: str) -> str:
        """Map canonical symbol to venue-specific format."""
        return self.symbols.to_venue(canonical, venue, default=canonical)

    async def get_size_scale(self, venue: str, canonical_symbol: str) -> int:
        """Get size scale (10^decimals) for venue/symbol pair."""
        await self._ensure_metadata(venue, canonical_symbol)
        return self._size_scales.get((venue.lower(), canonical_symbol.upper()), 1)

    async def get_min_size_i(self, venue: str, canonical_symbol: str) -> int:
        """Get minimum order size (integer scaled) for venue/symbol pair."""
        await self._ensure_metadata(venue, canonical_symbol)
        return self._min_size_i.get((venue.lower(), canonical_symbol.upper()), 1)

    async def get_price_size_decimals(self, venue: str, canonical_symbol: str) -> tuple[int, int]:
        """Get price and size decimal places for venue/symbol pair."""
        connector = self._connectors.get(venue.lower())
        if not connector:
            raise ValueError(f"No connector registered for venue: {venue}")

        venue_symbol = self.map_symbol(canonical_symbol, venue)
        return await connector.get_price_size_decimals(venue_symbol)

    async def _ensure_metadata(self, venue: str, canonical_symbol: str) -> None:
        """Ensure size scale and min size are cached for venue/symbol."""
        key = (venue.lower(), canonical_symbol.upper())

        if key in self._size_scales:
            return  # Already cached

        connector = self._connectors.get(venue.lower())
        if not connector:
            raise ValueError(f"No connector registered for venue: {venue}")

        venue_symbol = self.map_symbol(canonical_symbol, venue)

        try:
            # Get price and size decimals
            price_dec, size_dec = await connector.get_price_size_decimals(venue_symbol)
            scale = 10 ** int(size_dec)
            self._size_scales[key] = scale

            # Get minimum order size
            if hasattr(connector, "get_min_order_size_i"):
                min_size = await connector.get_min_order_size_i(venue_symbol)
            else:
                min_size = 1

            self._min_size_i[key] = min_size

            self.log.debug(
                "Cached metadata for %s:%s - scale=%d, min_size_i=%d",
                venue, canonical_symbol, scale, min_size
            )

        except Exception as e:
            self.log.error(
                "Failed to fetch metadata for %s:%s - %s",
                venue, canonical_symbol, e
            )
            # Use safe defaults
            self._size_scales[key] = 1
            self._min_size_i[key] = 1


__all__ = ["SymbolService"]