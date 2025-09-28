from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp


@dataclass
class TelemetryConfig:
    base_url: str
    token: Optional[str] = None
    group: Optional[str] = None
    bots: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: str | Path) -> "TelemetryConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            base_url=str(data.get("base_url") or "").rstrip('/'),
            token=data.get("token"),
            group=data.get("group"),
            bots=dict(data.get("bots") or {}),
        )


class TelemetryClient:
    def __init__(self, config: TelemetryConfig, *, logger: Optional[logging.Logger] = None) -> None:
        if not config.base_url:
            raise ValueError("telemetry base_url required")
        self.config = config
        self.log = logger or logging.getLogger("mm_bot.execution.telemetry")
        self._session: Optional[aiohttp.ClientSession] = None

    async def ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def dispatch(self, payloads: Dict[str, Dict[str, Any]], *, interval: float) -> None:
        session = await self.ensure_session()
        for role, payload in payloads.items():
            bot_name = self.config.bots.get(role, f"triad-{role.lower()}") if self.config.bots else f"telemetry-{role.lower()}"
            url = f"{self.config.base_url.rstrip('/')}/ingest/{bot_name}"
            headers = {"Content-Type": "application/json"}
            if self.config.token:
                headers["x-auth-token"] = self.config.token
            payload = dict(payload)
            payload.setdefault("timestamp", time.time())
            payload.setdefault("group", self.config.group)
            payload.setdefault("telemetry_interval_secs", interval)
            try:
                async with session.post(url, json=payload, headers=headers, timeout=5) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        self.log.debug("telemetry post %s failed status=%s body=%s", role, resp.status, text[:200])
            except Exception as exc:
                self.log.debug("telemetry post %s error: %s", role, exc)

    async def run_periodic(self, supplier, *, interval: float, stop_event: Optional[asyncio.Event] = None) -> None:
        """Continuously fetch payloads from supplier coroutine and dispatch."""

        while stop_event is None or not stop_event.is_set():
            try:
                payloads = await supplier()
                if payloads:
                    await self.dispatch(payloads, interval=interval)
            except Exception as exc:
                self.log.debug("telemetry loop error: %s", exc)
            await asyncio.sleep(interval)


__all__ = ["TelemetryClient", "TelemetryConfig"]
