from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class OrderState(str, Enum):
    SUBMITTING = "submitting"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


FINAL_STATES = {OrderState.FILLED, OrderState.CANCELLED, OrderState.FAILED}


@dataclass(slots=True)
class OrderEvent:
    state: OrderState
    ts: float = field(default_factory=lambda: time.time())
    info: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = {"state": self.state.value, "ts": self.ts}
        if self.info:
            payload["info"] = self.info
        return payload


class Order:
    """Represents a single order lifecycle and provides awaitable helpers."""

    def __init__(
        self,
        *,
        venue: str,
        symbol: str,
        client_order_index: int,
        is_ask: bool,
        log_dir: Optional[Path] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        self.venue = venue
        self.symbol = symbol
        self.client_order_index = client_order_index
        self.is_ask = is_ask
        self.trace_id = trace_id
        self.exchange_order_id: Optional[str] = None
        self._state = OrderState.SUBMITTING
        self._history: List[OrderEvent] = []
        self._loop = asyncio.get_event_loop()
        self._final_future: asyncio.Future[OrderEvent] = self._loop.create_future()
        self._update_waiters: List[asyncio.Future[OrderEvent]] = []
        self._lock = asyncio.Lock()
        self._log_dir = log_dir

    @property
    def state(self) -> OrderState:
        return self._state

    @property
    def history(self) -> List[OrderEvent]:
        return list(self._history)

    def snapshot(self) -> OrderEvent:
        return self._history[-1] if self._history else OrderEvent(state=self._state)

    async def wait_final(self, timeout: Optional[float] = None) -> OrderEvent:
        fut = asyncio.shield(self._final_future)
        if timeout is not None:
            return await asyncio.wait_for(fut, timeout)
        return await fut

    async def next_update(self, timeout: Optional[float] = None) -> OrderEvent:
        waiter: asyncio.Future[OrderEvent] = self._loop.create_future()
        async with self._lock:
            self._update_waiters.append(waiter)
        fut = asyncio.shield(waiter)
        if timeout is not None:
            return await asyncio.wait_for(fut, timeout)
        return await fut

    async def apply_update(self, event: OrderEvent, *, exchange_order_id: Optional[str] = None) -> OrderEvent:
        async with self._lock:
            if exchange_order_id:
                self.exchange_order_id = exchange_order_id
            self._state = event.state
            self._history.append(event)
            for waiter in self._update_waiters:
                if not waiter.done():
                    waiter.set_result(event)
            self._update_waiters.clear()
            if event.state in FINAL_STATES and not self._final_future.done():
                self._final_future.set_result(event)
            if self._log_dir:
                self._persist_event(event)
        return event

    def _persist_event(self, event: OrderEvent) -> None:
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{self.venue}-{self.symbol}-{self.client_order_index}.jsonl"
            target = self._log_dir / filename
            payload: Dict[str, Any] = {
                "trace_id": self.trace_id,
                "client_order_index": self.client_order_index,
                "exchange_order_id": self.exchange_order_id,
                **event.to_dict(),
            }
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
        except Exception:
            # Persistence must never break state propagation; defer to logging layer.
            pass


__all__ = ["OrderState", "FINAL_STATES", "OrderEvent", "Order"]
