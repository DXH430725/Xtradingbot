"""Trading core for startup, event coordination and lifecycle management.

Target: < 300 lines.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from .clock import SimpleClock
from ..execution import ExecutionRouter


class TradingCore:
    """Core trading system orchestrator.

    Manages strategy lifecycle, clock, execution router, and connectors.
    """

    def __init__(
        self,
        *,
        tick_interval_ms: float = 1000.0,
        debug: bool = False,
        logger: Optional[logging.Logger] = None
    ):
        self.debug_enabled = debug
        self.log = logger or logging.getLogger("xbot.core.trading_core")

        # Core components
        self.clock = SimpleClock(tick_interval_ms, logger=self.log)
        self.router = ExecutionRouter(logger=self.log)

        # Strategy and connectors
        self.strategy: Optional[Any] = None
        self.connectors: Dict[str, Any] = {}

        # State tracking
        self._running = False
        self._started_at: Optional[float] = None
        self._stop_event = asyncio.Event()
        self._stop_event.set()  # Initially stopped

    def add_connector(self, venue: str, connector: Any) -> None:
        """Add connector to the system.

        Args:
            venue: Venue name (e.g., 'backpack', 'lighter')
            connector: Connector instance implementing IConnector
        """
        venue_key = venue.lower()
        self.connectors[venue_key] = connector

        # Register with router
        self.router.register_connector(venue_key, connector)

        self.log.info(f"Added connector: {venue}")

    def set_strategy(self, strategy: Any) -> None:
        """Set the active trading strategy.

        Args:
            strategy: Strategy instance implementing StrategyBase
        """
        self.strategy = strategy
        self.log.info("Strategy set")

    def register_symbol(self, canonical: str, **venues: str) -> None:
        """Register symbol mapping.

        Args:
            canonical: Canonical symbol name (e.g., 'BTC')
            **venues: Venue-specific symbol mappings
        """
        self.router.register_symbol(canonical, **venues)
        self.log.debug(f"Registered symbol mapping: {canonical} -> {venues}")

    async def start(self) -> None:
        """Start the trading core."""
        if self._running:
            self.log.warning("Trading core already running")
            return

        self.log.info("Starting trading core...")

        # Start connectors
        for venue, connector in self.connectors.items():
            try:
                if hasattr(connector, 'start'):
                    await connector.start()
                self.log.info(f"Started connector: {venue}")
            except Exception as e:
                self.log.error(f"Failed to start connector {venue}: {e}")
                raise

        # Start strategy
        if self.strategy:
            try:
                if hasattr(self.strategy, 'start'):
                    self.strategy.start(self)
                self.log.info("Started strategy")
            except Exception as e:
                self.log.error(f"Failed to start strategy: {e}")
                raise

            # Register strategy tick handler
            self.clock.add_tick_handler(self._strategy_tick)

        # Start clock
        self.clock.start()

        # Update state
        self._running = True
        self._started_at = time.time()
        self._stop_event.clear()

        self.log.info("Trading core started successfully")

    async def stop(self, *, cancel_orders: bool = False) -> None:
        """Stop the trading core.

        Args:
            cancel_orders: Whether to cancel all open orders
        """
        if not self._running:
            self.log.warning("Trading core not running")
            self._stop_event.set()
            return

        self.log.info("Stopping trading core...")

        # Stop clock first
        await self.clock.stop()

        # Stop strategy
        if self.strategy:
            try:
                if hasattr(self.strategy, 'stop'):
                    self.strategy.stop()
                self.log.info("Stopped strategy")
            except Exception as e:
                self.log.error(f"Error stopping strategy: {e}")

        # Cancel orders if requested
        if cancel_orders:
            await self._cancel_all_orders()

        # Stop connectors
        for venue, connector in self.connectors.items():
            try:
                if hasattr(connector, 'stop'):
                    await connector.stop()
                self.log.info(f"Stopped connector: {venue}")
            except Exception as e:
                self.log.error(f"Error stopping connector {venue}: {e}")

        # Update state
        self._running = False
        self._stop_event.set()

        self.log.info("Trading core stopped")

    async def shutdown(self) -> None:
        """Complete shutdown with cleanup."""
        await self.stop(cancel_orders=True)

        # Close connectors
        for venue, connector in self.connectors.items():
            try:
                if hasattr(connector, 'close'):
                    await connector.close()
                self.log.info(f"Closed connector: {venue}")
            except Exception as e:
                self.log.error(f"Error closing connector {venue}: {e}")

        # Clear references
        self.strategy = None
        self.connectors.clear()
        self._started_at = None

        self.log.info("Trading core shutdown complete")

    async def wait_until_stopped(self) -> None:
        """Wait until the core is stopped."""
        await self._stop_event.wait()

    async def _strategy_tick(self, now_ms: float) -> None:
        """Handle strategy tick execution."""
        if not self.strategy:
            return

        try:
            if hasattr(self.strategy, 'on_tick'):
                await self.strategy.on_tick(now_ms)
        except Exception as e:
            self.log.error(f"Strategy tick error: {e}")
            if self.debug_enabled:
                self.log.exception("Strategy tick exception details")

    async def _cancel_all_orders(self) -> None:
        """Cancel all orders on all connectors."""
        self.log.info("Cancelling all orders...")

        cancel_tasks = []
        for venue, connector in self.connectors.items():
            if hasattr(connector, 'cancel_all'):
                task = asyncio.create_task(
                    self._safe_cancel_all(venue, connector)
                )
                cancel_tasks.append(task)

        if cancel_tasks:
            await asyncio.gather(*cancel_tasks, return_exceptions=True)

    async def _safe_cancel_all(self, venue: str, connector: Any) -> None:
        """Safely cancel all orders for a connector."""
        try:
            cancelled = await connector.cancel_all(timeout=10.0)
            self.log.info(f"Cancelled {cancelled} orders on {venue}")
        except Exception as e:
            self.log.error(f"Failed to cancel orders on {venue}: {e}")

    def get_status(self) -> Dict[str, Any]:
        """Get current system status."""
        uptime = time.time() - self._started_at if self._started_at else 0

        return {
            "running": self._running,
            "uptime_seconds": uptime,
            "connectors": list(self.connectors.keys()),
            "strategy_active": self.strategy is not None,
            "clock_running": self.clock.is_running,
            "tick_handlers": self.clock.handler_count,
            "debug_enabled": self.debug_enabled
        }

    def dbg(self, message: str, **kwargs) -> None:
        """Debug logging helper."""
        if self.debug_enabled:
            self.log.debug(message, **kwargs)