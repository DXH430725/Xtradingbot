import asyncio
import logging
import time
from typing import Awaitable, Callable, List, Optional


def _get_logger() -> logging.Logger:
    return logging.getLogger("mm_bot.core.clock")


class SimpleClock:
    """
    Minimal asyncio-based clock that periodically calls registered async tick handlers
    with the current wall time in milliseconds.
    """

    def __init__(self, tick_size: float = 1.0, logger: Optional[logging.Logger] = None):
        self._tick_size = float(tick_size)
        self._tick_handlers: List[Callable[[float], Awaitable[None]]] = []
        self._task: Optional[asyncio.Task] = None
        self._running: bool = False
        self._logger = logger or _get_logger()

    def add_tick_handler(self, handler: Callable[[float], Awaitable[None]]):
        self._tick_handlers.append(handler)

    async def _run(self):
        self._running = True
        try:
            while self._running:
                now_ms = time.time() * 1000
                if self._tick_handlers:
                    await asyncio.gather(*(h(now_ms) for h in self._tick_handlers), return_exceptions=True)
                await asyncio.sleep(self._tick_size)
        finally:
            self._running = False

    def start(self):
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="mm_bot_simple_clock")

    async def stop(self):
        if self._task and not self._task.done():
            self._running = False
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

