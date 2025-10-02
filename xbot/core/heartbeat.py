from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Optional

import httpx

from connector.interface import IConnector
from .clock import WallClock
from ..execution.router import ExecutionRouter


@dataclass(slots=True)
class HeartbeatConfig:
    url: str
    interval_secs: float = 30.0
    timeout_secs: float = 5.0
    bearer_token: Optional[str] = None


class HeartbeatService:
    def __init__(
        self,
        *,
        connector: IConnector,
        router: ExecutionRouter,
        clock: WallClock,
        strategy_name: str,
        venue: str,
        config: HeartbeatConfig,
    ) -> None:
        self._connector = connector
        self._router = router
        self._clock = clock
        self._strategy = strategy_name
        self._venue = venue
        self._config = config
        self._client = httpx.AsyncClient(timeout=config.timeout_secs)
        self._task: Optional[asyncio.Task] = None
        self._running = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running.set()
        self._task = asyncio.create_task(self._run(), name="heartbeat")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._running.clear()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        await self._client.aclose()

    async def _run(self) -> None:
        while self._running.is_set():
            await self._emit_once()
            await asyncio.sleep(self._config.interval_secs)

    async def _emit_once(self) -> None:
        try:
            positions = await self._connector.get_positions()
        except Exception:
            positions = []
        try:
            margin = await self._connector.get_margin()
        except Exception:
            margin = {}
        payload = {
            "ts": int(self._clock.now()),
            "strategy": self._strategy,
            "venue": self._venue,
            "positions": positions,
            "margin": margin,
        }
        headers = {"Content-Type": "application/json"}
        if self._config.bearer_token:
            headers["Authorization"] = f"Bearer {self._config.bearer_token}"
        try:
            await self._client.post(self._config.url, json=payload, headers=headers)
        except Exception:
            # Heartbeat failures should not break trading; rely on logs
            pass


__all__ = ["HeartbeatService", "HeartbeatConfig"]
