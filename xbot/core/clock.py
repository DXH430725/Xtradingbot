from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional, List, Protocol


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


class TimeIterator(Protocol):
    async def on_tick(self, ts: float) -> None:  # pragma: no cover - protocol
        ...


class Clock:
    def __init__(self, interval: float = 1.0) -> None:
        self._interval = interval
        self._iters: List[TimeIterator] = []
        self._running = False

    def add_iterator(self, it: TimeIterator) -> None:
        self._iters.append(it)

    async def start(self, *, max_ticks: int | None = None) -> None:
        self._running = True
        tick = 0
        try:
            while self._running:
                ts = time.time()
                for it in list(self._iters):
                    await it.on_tick(ts)
                tick += 1
                if max_ticks is not None and tick >= max_ticks:
                    break
                await asyncio.sleep(self._interval)
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False


__all__ = ["WallClock", "TimeIterator", "Clock"]
