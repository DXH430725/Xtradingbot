import asyncio
import logging
import os
import time
from typing import Any, Callable, Dict, Optional

from .clock import SimpleClock


def _get_logger() -> logging.Logger:
    return logging.getLogger("mm_bot.core.trading_core")


class TradingCore:
    """
    A simplified, standalone trading core tailored for a single-exchange, single-strategy MVP.

    Responsibilities:
    - Manage a single clock and a set of connectors
    - Host one strategy (pluggable)
    - Provide lifecycle controls (start, stop, shutdown)
    - Optional debug output gated by a switch
    - No dependency on Hummingbot internals
    """

    def __init__(
        self,
        tick_size: float = 1.0,
        debug: Optional[bool] = None,
        clock_factory: Callable[[float, Optional[logging.Logger]], SimpleClock] = SimpleClock,
        logger: Optional[logging.Logger] = None,
    ):
        self.logger = logger or _get_logger()
        self.debug_enabled = self._resolve_debug_flag(debug)

        self.tick_size = float(tick_size)
        self.clock_factory = clock_factory
        self.clock: Optional[SimpleClock] = None

        self.connectors: Dict[str, Any] = {}
        self.strategy: Optional[Any] = None

        self._is_running: bool = False
        self._started_at_ms: Optional[float] = None
        self._stopped_event = asyncio.Event()
        self._stopped_event.set()

    def dbg(self, msg: str, exc_info: bool = False):
        if self.debug_enabled:
            try:
                self.logger.debug(msg, exc_info=exc_info)
            except Exception:
                pass

    def _resolve_debug_flag(self, arg_value: Optional[bool]) -> bool:
        if arg_value is not None:
            return bool(arg_value)
        env_val = os.getenv("XTB_DEBUG")
        if env_val is not None:
            return env_val.strip().lower() in {"1", "true", "yes", "on"}
        return False

    # Connector management -------------------------------------------------
    def add_connector(self, name: str, connector: Any):
        """
        Register a connector. A connector may optionally implement:
        - start(core)
        - stop(core)
        - cancel_all(timeout: float)
        - ready (bool)
        """
        self.connectors[name] = connector
        self.dbg(f"Connector added: {name}")

    def remove_connector(self, name: str):
        if name in self.connectors:
            conn = self.connectors.pop(name)
            try:
                stop = getattr(conn, "stop", None)
                if callable(stop):
                    stop(self)
            except Exception:
                self.dbg(f"Error stopping connector {name}", exc_info=True)
            self.dbg(f"Connector removed: {name}")
            return True
        return False

    # Strategy management --------------------------------------------------
    def set_strategy(self, strategy: Any):
        """
        Set the active strategy. Strategy should implement:
        - start(core)
        - stop()
        - on_tick(now_ms: float) -> awaitable
        """
        self.strategy = strategy
        self.dbg("Strategy set")

    # Lifecycle ------------------------------------------------------------
    async def start(self) -> bool:
        if self._is_running:
            self.logger.warning("TradingCore already running")
            self.dbg("start called while already running")
            return False

        # Start connectors if they have a start method
        for name, conn in self.connectors.items():
            try:
                start = getattr(conn, "start", None)
                if callable(start):
                    start(self)
                    self.dbg(f"Connector started: {name}")
            except Exception:
                self.logger.error(f"Failed to start connector {name}")
                self.dbg("connector.start exception", exc_info=True)

        # Start strategy
        if self.strategy is not None:
            try:
                start = getattr(self.strategy, "start", None)
                if callable(start):
                    start(self)
                self.dbg("Strategy started")
            except Exception:
                self.logger.error("Failed to start strategy")
                self.dbg("strategy.start exception", exc_info=True)
                return False

        # Build and start the clock
        self.clock = self.clock_factory(self.tick_size, self.logger)
        if self.strategy is not None:
            # Register tick handler
            async def _tick(now_ms: float):
                try:
                    await self.strategy.on_tick(now_ms)
                except Exception:
                    self.dbg("strategy.on_tick exception", exc_info=True)

            self.clock.add_tick_handler(_tick)

        self.clock.start()
        self._is_running = True
        self._started_at_ms = time.time() * 1000
        self._stopped_event.clear()
        self.logger.info("TradingCore started")
        self.dbg(f"Clock started with tick_size={self.tick_size}")
        return True

    async def stop(self, cancel_orders: bool = True) -> bool:
        if not self._is_running:
            self.logger.warning("TradingCore is not running")
            self._stopped_event.set()
            return False

        # Stop clock first to avoid further ticks
        if self.clock is not None:
            await self.clock.stop()
            self.dbg("Clock stopped")
            self.clock = None

        # Strategy stop
        if self.strategy is not None:
            try:
                stop = getattr(self.strategy, "stop", None)
                if callable(stop):
                    stop()
                self.dbg("Strategy stopped")
            except Exception:
                self.logger.error("Error while stopping strategy")
                self.dbg("strategy.stop exception", exc_info=True)

        cancel_flag = bool(cancel_orders)
        if cancel_flag and self.strategy is not None:
            preserve = bool(getattr(self.strategy, "preserve_orders_on_stop", False))
            if preserve:
                self.logger.info("Strategy requested to keep open orders; skipping cancel_all on stop")
                cancel_flag = False

        # Optionally cancel outstanding orders on connectors
        if cancel_flag:
            for name, conn in self.connectors.items():
                try:
                    cancel_all = getattr(conn, "cancel_all", None)
                    if callable(cancel_all):
                        try:
                            # try with a timeout parameter first
                            res = cancel_all(10.0)
                            if asyncio.iscoroutine(res):
                                await res
                        except TypeError:
                            # fallback: no-arg signature
                            res = cancel_all()
                            if asyncio.iscoroutine(res):
                                await res
                        self.dbg(f"Connector orders cancelled: {name}")
                except Exception:
                    self.logger.error(f"Error cancelling orders for {name}")
                    self.dbg("connector.cancel_all exception", exc_info=True)

        # Stop connectors
        for name, conn in list(self.connectors.items()):
            try:
                stop = getattr(conn, "stop", None)
                if callable(stop):
                    res = stop(self)
                    if asyncio.iscoroutine(res):
                        await res
                self.dbg(f"Connector stopped: {name}")
            except Exception:
                self.logger.error(f"Error stopping connector {name}")
                self.dbg("connector.stop exception", exc_info=True)
            try:
                close = getattr(conn, "close", None)
                if callable(close):
                    res = close()
                    if asyncio.iscoroutine(res):
                        await res
                self.dbg(f"Connector closed: {name}")
            except Exception:
                self.logger.error(f"Error closing connector {name}")
                self.dbg("connector.close exception", exc_info=True)

        self._is_running = False
        self.logger.info("TradingCore stopped")
        self._stopped_event.set()
        return True

    async def shutdown(self, cancel_orders: bool = True) -> bool:
        ok = await self.stop(cancel_orders=cancel_orders)
        # Clear references
        self.strategy = None
        self.connectors.clear()
        self._started_at_ms = None
        self.dbg("Shutdown complete; state cleared")
        return ok

    async def wait_until_stopped(self) -> None:
        await self._stopped_event.wait()

    # Introspection --------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        return {
            "running": self._is_running,
            "tick_size": self.tick_size,
            "uptime_ms": (time.time() * 1000 - self._started_at_ms) if self._started_at_ms else 0,
            "connectors": list(self.connectors.keys()),
            "strategy_set": self.strategy is not None,
        }
