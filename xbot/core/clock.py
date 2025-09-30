"""Simple event-driven clock for strategy execution.

Target: < 200 lines.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, List, Optional


class SimpleClock:
    """Simple asyncio-based clock for periodic strategy execution."""

    def __init__(
        self,
        tick_interval_ms: float = 1000.0,
        *,
        logger: Optional[logging.Logger] = None
    ):
        self.tick_interval_ms = max(tick_interval_ms, 100.0)  # Minimum 100ms
        self.log = logger or logging.getLogger("xbot.core.clock")

        self._tick_handlers: List[Callable[[float], Awaitable[None]]] = []
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._stop_event = asyncio.Event()

    def add_tick_handler(self, handler: Callable[[float], Awaitable[None]]) -> None:
        """Add a tick handler function.

        Args:
            handler: Async function that takes timestamp in milliseconds
        """
        self._tick_handlers.append(handler)
        self.log.debug(f"Added tick handler: {len(self._tick_handlers)} total")

    def remove_tick_handler(self, handler: Callable[[float], Awaitable[None]]) -> bool:
        """Remove a tick handler.

        Args:
            handler: Handler to remove

        Returns:
            True if handler was found and removed
        """
        try:
            self._tick_handlers.remove(handler)
            self.log.debug(f"Removed tick handler: {len(self._tick_handlers)} remaining")
            return True
        except ValueError:
            return False

    def start(self) -> None:
        """Start the clock."""
        if self._running:
            self.log.warning("Clock already running")
            return

        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        self._running = True
        self.log.info(f"Clock started with {self.tick_interval_ms}ms interval")

    async def stop(self) -> None:
        """Stop the clock."""
        if not self._running:
            return

        self._running = False
        self._stop_event.set()

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        self._task = None
        self.log.info("Clock stopped")

    async def _run_loop(self) -> None:
        """Main clock loop."""
        self.log.debug("Clock loop started")

        try:
            while self._running:
                loop_start = time.time()
                now_ms = loop_start * 1000

                # Execute all tick handlers
                if self._tick_handlers:
                    try:
                        await asyncio.gather(
                            *[handler(now_ms) for handler in self._tick_handlers],
                            return_exceptions=True
                        )
                    except Exception as e:
                        self.log.error(f"Error in tick handlers: {e}")

                # Calculate sleep time to maintain interval
                loop_duration = time.time() - loop_start
                sleep_time = max(0.001, (self.tick_interval_ms / 1000.0) - loop_duration)

                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_time)
                    break  # Stop event was set
                except asyncio.TimeoutError:
                    pass  # Normal timeout, continue loop

        except asyncio.CancelledError:
            self.log.debug("Clock loop cancelled")
        except Exception as e:
            self.log.error(f"Clock loop error: {e}")
        finally:
            self._running = False
            self.log.debug("Clock loop ended")

    @property
    def is_running(self) -> bool:
        """Check if clock is running."""
        return self._running

    @property
    def handler_count(self) -> int:
        """Get number of registered handlers."""
        return len(self._tick_handlers)