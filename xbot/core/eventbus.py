from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, DefaultDict, List

Callback = Callable[[dict], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._listeners: DefaultDict[str, List[Callback]] = DefaultDict(list)

    def on(self, event: str, cb: Callback) -> None:
        self._listeners[event].append(cb)

    def emit(self, event: str, payload: dict) -> None:
        for cb in self._listeners.get(event, []):
            asyncio.create_task(cb(payload))

