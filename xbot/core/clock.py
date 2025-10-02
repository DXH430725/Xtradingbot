from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional


class WallClock:
    """Async friendly wall clock helper used by services and strategies."""

    def __init__(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self._loop = loop or asyncio.get_event_loop()

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def call_later(self, delay: float, callback: Callable[..., None], *args, **kwargs) -> asyncio.TimerHandle:
        return self._loop.call_later(delay, callback, *args, **kwargs)

    def run_coroutine(self, coro: Awaitable[object]) -> asyncio.Task:
        return self._loop.create_task(coro)


__all__ = ["WallClock"]
