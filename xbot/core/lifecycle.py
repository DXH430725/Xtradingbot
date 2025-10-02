from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Awaitable, Callable, List, Optional

from connector.interface import IConnector


class LifecycleController:
    """Coordinates startup/shutdown for connector-scoped resources."""

    def __init__(
        self,
        *,
        connector: IConnector,
        background_tasks: Optional[List[Callable[[], Awaitable[None]]]] = None,
    ) -> None:
        self._connector = connector
        self._background_factories = background_tasks or []
        self._tasks: List[asyncio.Task] = []
        self._stack = AsyncExitStack()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        await self._connector.start()
        for factory in self._background_factories:
            task = asyncio.create_task(factory())
            self._tasks.append(task)
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._connector.stop()
        await self._stack.aclose()
        self._tasks.clear()
        self._started = False


__all__ = ["LifecycleController"]
