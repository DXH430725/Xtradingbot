from __future__ import annotations

from typing import Dict, Optional


class SymbolMapper:
    """Maintain bidirectional mapping between canonical symbols and venue-specific tickers."""

    def __init__(self, initial: Optional[Dict[str, Dict[str, str]]] = None) -> None:
        self._canonical_to_venue: Dict[str, Dict[str, str]] = {}
        self._venue_to_canonical: Dict[str, Dict[str, str]] = {}
        if initial:
            for canonical, venues in initial.items():
                self.register(canonical, **venues)

    def register(self, canonical: str, **venues: str) -> None:
        canonical_key = canonical.upper()
        venue_map = self._canonical_to_venue.setdefault(canonical_key, {})
        for venue, symbol in venues.items():
            venue_key = venue.lower()
            symbol_key = self._normalize(symbol)
            venue_map[venue_key] = symbol
            canon_map = self._venue_to_canonical.setdefault(venue_key, {})
            canon_map[symbol_key] = canonical_key

    def update_for_venue(self, venue: str, mapping: Dict[str, str]) -> None:
        for canonical, symbol in mapping.items():
            self.register(canonical, **{venue: symbol})

    def to_venue(self, canonical: str, venue: str, *, default: Optional[str] = None) -> Optional[str]:
        venue_map = self._canonical_to_venue.get(canonical.upper()) or {}
        return venue_map.get(venue.lower(), default)

    def to_canonical(self, venue: str, symbol: str, *, default: Optional[str] = None) -> Optional[str]:
        canon_map = self._venue_to_canonical.get(venue.lower()) or {}
        return canon_map.get(self._normalize(symbol), default)

    def has(self, canonical: str, venue: str) -> bool:
        return self.to_venue(canonical, venue) is not None

    def symbols_for(self, venue: str) -> Dict[str, str]:
        return dict((canon, self.to_venue(canon, venue)) for canon in self._canonical_to_venue)

    def canonical_symbols(self) -> Dict[str, Dict[str, str]]:
        return {canonical: dict(venues) for canonical, venues in self._canonical_to_venue.items()}

    def _normalize(self, symbol: str) -> str:
        return "".join(ch for ch in str(symbol or "").upper() if ch.isalnum() or ch in {"_", "-"})


__all__ = ["SymbolMapper"]
