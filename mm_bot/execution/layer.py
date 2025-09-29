from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional, List

from mm_bot.execution.orders import TrackingLimitOrder, TrackingMarketOrder

from .adapters import get_api_key_index
from .data import get_collateral, get_position, plan_order_size
from .emergency import emergency_unwind
from .ids import COIManager, NonceManager
from .order_actions import place_tracking_limit_order, place_tracking_market_order
from .positions import confirm_position, rebalance_position
from .symbols import SymbolMapper
from .telemetry import TelemetryClient, TelemetryConfig
from .notifier import TelegramNotifier


class ExecutionLayer:
    """High-level execution facade coordinating connectors, IDs, and helpers."""

    def __init__(self, *, logger: Optional[logging.Logger] = None, symbols: Optional[SymbolMapper] = None) -> None:
        self.log = logger or logging.getLogger("mm_bot.execution.layer")
        self.symbols = symbols or SymbolMapper()
        self.connectors: Dict[str, Any] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._size_scales: Dict[tuple[str, str], int] = {}
        self._min_size_i: Dict[tuple[str, str], int] = {}
        self._api_keys: Dict[str, Optional[int]] = {}
        self.coi_manager = COIManager(logger=self.log)
        self.nonce_manager = NonceManager(logger=self.log)
        self.telemetry: Optional[TelemetryClient] = None
        self.notifier: Optional[TelegramNotifier] = None

    def register_connector(
        self,
        venue: str,
        connector: Any,
        *,
        coi_limit: Optional[int] = None,
        api_key_index: Optional[int] = None,
    ) -> None:
        key = venue.lower()
        self.connectors[key] = connector
        self._locks.pop(key, None)  # lazily recreated per loop
        if coi_limit:
            self.coi_manager.register_limit(key, coi_limit)
        if api_key_index is not None:
            self._api_keys[key] = api_key_index
        else:
            self._api_keys[key] = get_api_key_index(connector)

    def register_symbol(self, canonical: str, **venues: str) -> None:
        self.symbols.register(canonical, **venues)

    async def ensure_scale(self, venue: str, canonical_symbol: str) -> int:
        key = (venue.lower(), canonical_symbol.upper())
        if key in self._size_scales:
            return self._size_scales[key]
        connector = self.connectors[venue.lower()]
        venue_symbol = self.symbols.to_venue(canonical_symbol, venue, default=canonical_symbol)
        price_dec, size_dec = await connector.get_price_size_decimals(venue_symbol)
        scale = 10 ** int(size_dec)
        self._size_scales[key] = scale
        self._min_size_i[key] = await connector.get_min_order_size_i(venue_symbol) if hasattr(connector, "get_min_order_size_i") else 1
        return scale

    async def size_scale(self, venue: str, canonical_symbol: str) -> int:
        return await self.ensure_scale(venue, canonical_symbol)

    async def min_size_i(self, venue: str, canonical_symbol: str) -> int:
        await self.ensure_scale(venue, canonical_symbol)
        return self._min_size_i.get((venue.lower(), canonical_symbol.upper()), 1)

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
    ) -> Optional[TrackingMarketOrder]:
        connector = self.connectors[venue.lower()]
        venue_symbol = self.symbols.to_venue(canonical_symbol, venue, default=canonical_symbol)
        lock = await self._lock_for(venue)
        api_key_index = self._api_keys.get(venue.lower())
        return await place_tracking_market_order(
            connector,
            symbol=venue_symbol,
            size_i=size_i,
            is_ask=is_ask,
            reduce_only=reduce_only,
            max_slippage=max_slippage,
            attempts=attempts,
            retry_delay=retry_delay,
            wait_timeout=wait_timeout,
            coi_manager=self.coi_manager,
            nonce_manager=self.nonce_manager,
            api_key_index=api_key_index,
            lock=lock,
            label=label or f"{venue}:{venue_symbol}",
            logger=self.log,
        )

    async def limit_order(
        self,
        venue: str,
        canonical_symbol: str,
        *,
        base_amount_i: int,
        is_ask: bool,
        **kwargs: Any,
    ) -> TrackingLimitOrder:
        connector = self.connectors[venue.lower()]
        venue_symbol = self.symbols.to_venue(canonical_symbol, venue, default=canonical_symbol)
        lock = await self._lock_for(venue)
        return await place_tracking_limit_order(
            connector,
            symbol=venue_symbol,
            base_amount_i=base_amount_i,
            is_ask=is_ask,
            coi_manager=self.coi_manager,
            lock=lock,
            logger=self.log,
            **kwargs,
        )

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
        connector = self.connectors[venue.lower()]
        venue_symbol = self.symbols.to_venue(canonical_symbol, venue, default=canonical_symbol)
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
        connector = self.connectors[venue.lower()]
        venue_symbol = self.symbols.to_venue(canonical_symbol, venue, default=canonical_symbol)
        size_scale = await self.ensure_scale(venue, canonical_symbol)
        min_size = self._min_size_i.get((venue.lower(), canonical_symbol.upper()), 1)
        api_key_index = self._api_keys.get(venue.lower())
        lock = await self._lock_for(venue)
        return await rebalance_position(
            connector,
            symbol=venue_symbol,
            target=target,
            size_scale=size_scale,
            min_size_i=min_size,
            tolerance=tolerance,
            attempts=attempts,
            retry_delay=retry_delay,
            coi_manager=self.coi_manager,
            nonce_manager=self.nonce_manager,
            api_key_index=api_key_index,
            lock=lock,
            logger=self.log,
        )

    async def position(self, venue: str, canonical_symbol: str) -> float:
        connector = self.connectors[venue.lower()]
        venue_symbol = self.symbols.to_venue(canonical_symbol, venue, default=canonical_symbol)
        return await get_position(connector, venue_symbol)

    async def collateral(self, venue: str) -> float:
        connector = self.connectors[venue.lower()]
        return await get_collateral(connector)

    async def plan_order_size(
        self,
        venue: str,
        canonical_symbol: str,
        *,
        leverage: float,
        min_collateral: float,
        collateral_buffer: float = 0.96,
    ) -> Optional[Dict[str, Any]]:
        connector = self.connectors[venue.lower()]
        venue_symbol = self.symbols.to_venue(canonical_symbol, venue, default=canonical_symbol)
        size_scale = await self.ensure_scale(venue, canonical_symbol)
        return await plan_order_size(
            connector,
            symbol=venue_symbol,
            leverage=leverage,
            min_collateral=min_collateral,
            size_scale=size_scale,
            min_size_i=self._min_size_i.get((venue.lower(), canonical_symbol.upper()), 1),
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
        targets = [v.lower() for v in (venues or self.connectors.keys())]
        size_scales: Dict[str, int] = {}
        symbols: Dict[str, str] = {}
        locks: Dict[str, asyncio.Lock] = {}
        filtered_connectors: Dict[str, Any] = {}
        for venue in targets:
            connector = self.connectors.get(venue)
            if connector is None:
                continue
            mapped_symbol = self.symbols.to_venue(canonical_symbol, venue)
            if mapped_symbol is None:
                continue
            filtered_connectors[venue] = connector
            size_scales[venue] = await self.ensure_scale(venue, canonical_symbol)
            symbols[venue] = mapped_symbol
            locks[venue] = await self._lock_for(venue)
        if not filtered_connectors:
            return {}
        return await emergency_unwind(
            filtered_connectors,
            symbols=symbols,
            size_scales=size_scales,
            tolerance=tolerance,
            coi_manager=self.coi_manager,
            nonce_manager=self.nonce_manager,
            api_key_indices={k: self._api_keys.get(k) for k in filtered_connectors},
            locks=locks,
            logger=self.log,
            notify=self._notify_emergency,
        )

    def configure_telemetry(self, config: TelemetryConfig) -> None:
        self.telemetry = TelemetryClient(config, logger=self.log)

    def configure_telegram(self, notifier: TelegramNotifier) -> None:
        self.notifier = notifier

    async def _notify_emergency(self, results: Dict[str, bool]) -> None:
        if not self.notifier:
            return
        msg = "[Execution] Emergency unwind results: " + ", ".join(f"{venue}:{'ok' if ok else 'fail'}" for venue, ok in results.items())
        await self.notifier.send(msg)

    async def _lock_for(self, venue: str) -> asyncio.Lock:
        key = venue.lower()
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock


__all__ = ["ExecutionLayer"]
