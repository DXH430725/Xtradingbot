from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from pathlib import Path
from typing import Iterable, List, Optional, Callable, Awaitable, Dict, Any

import websockets
from cryptography.hazmat.primitives.asymmetric import ed25519

from xbot.core.cache import MarketCache
from xbot.execution.order_service import OrderUpdatePayload
from xbot.execution.models import OrderState
from xbot.utils.logging import get_logger


class BackpackWsClient:
    """Backpack WebSocket client implemented using websockets and ED25519 auth.

    - Public streams: depth.<symbol>, trade.<symbol>
    - Private streams: account.orderUpdate, account.positionUpdate (if keys present)
    - Auto reconnect with backoff; graceful shutdown via stop()
    """

    WS_URL = "wss://ws.backpack.exchange"

    def __init__(
        self,
        *,
        symbols: Iterable[str],
        key_file: Path,
        cache: MarketCache,
        reconnect_delay: float = 3.0,
        ping_interval: float = 55.0,
        ping_timeout: float = 10.0,
        on_order_update: Optional[Callable[[OrderUpdatePayload], Awaitable[None]]] = None,
    ) -> None:
        self._symbols = list(symbols)
        self._key_file = key_file
        self._cache = cache
        self._reconnect_delay = reconnect_delay
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._logger = get_logger(__name__)
        self._running = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._on_order_update = on_order_update

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running.set()
        self._task = asyncio.create_task(self._run(), name="backpack-ws")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._running.clear()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def _load_keys(self) -> tuple[str | None, str | None]:
        if not self._key_file.exists():
            return None, None
        try:
            content = self._key_file.read_text(encoding="utf-8")
            lines = dict(
                line.split(":", 1)
                for line in content.splitlines()
                if ":" in line
            )
            pub = (lines.get("api key") or lines.get("api_key") or lines.get("apiKey") or "").strip()
            sec = (lines.get("api secret") or lines.get("api_secret") or lines.get("apiSecret") or "").strip()
            return (pub or None), (sec or None)
        except Exception:
            return None, None

    def _signature_tuple(self) -> Optional[list[str]]:
        pub, sec = self._load_keys()
        if not pub or not sec:
            return None
        ts = int(time.time() * 1000)
        window = 5000
        message = f"instruction=subscribe&timestamp={ts}&window={window}"
        try:
            priv = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(sec))
            sig = priv.sign(message.encode("utf-8"))
            sig_b64 = base64.b64encode(sig).decode()
            return [pub, sig_b64, str(ts), str(window)]
        except Exception as exc:
            self._logger.info("ws_sign_error", extra={"venue": "backpack", "error": str(exc)})
            return None

    async def _subscribe(self, ws, streams: List[str], signature: Optional[list[str]] = None) -> None:
        if not streams:
            return
        payload = {"method": "SUBSCRIBE", "params": streams}
        if signature:
            payload["signature"] = signature
        await ws.send(json.dumps(payload))

    async def _run(self) -> None:
        public_streams: List[str] = [f"depth.{s}" for s in self._symbols] + [f"trade.{s}" for s in self._symbols]
        private_streams: List[str] = ["account.orderUpdate", "account.positionUpdate"]

        while self._running.is_set():
            signature = self._signature_tuple()
            has_private = bool(signature)
            try:
                self._logger.info(
                    "ws_connecting",
                    extra={
                        "venue": "backpack",
                        "public_streams": public_streams,
                        "has_private": has_private,
                    },
                )
                async with websockets.connect(
                    self.WS_URL,
                    ping_interval=self._ping_interval,
                    ping_timeout=self._ping_timeout,
                    max_size=2 ** 22,
                ) as ws:
                    await self._subscribe(ws, public_streams)
                    if has_private:
                        await self._subscribe(ws, private_streams, signature=signature)
                    self._logger.info("ws_connected", extra={"venue": "backpack", "has_private": has_private})
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        await self._handle_message(msg)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._logger.info("ws_error", extra={"venue": "backpack", "error": str(exc)})
                await asyncio.sleep(self._reconnect_delay)

    async def _handle_message(self, msg: dict) -> None:
        stream = msg.get("stream")
        data = msg.get("data")
        if not stream or data is None:
            return
        try:
            if stream.startswith("depth."):
                symbol = stream.split(".", 1)[1]
                bid = data.get("b") or data.get("bids")
                ask = data.get("a") or data.get("asks")
                top_b = float(bid[0][0]) if bid else None
                top_a = float(ask[0][0]) if ask else None
                await self._cache.set_top(symbol, top_b, top_a)
            elif stream.startswith("trade."):
                symbol = stream.split(".", 1)[1]
                trade = {
                    "p": data.get("p") or data.get("price"),
                    "q": data.get("q") or data.get("size"),
                    "t": data.get("t") or data.get("ts"),
                    "m": data.get("m") or data.get("is_maker"),
                }
                await self._cache.add_trade(symbol, trade)
            elif stream.startswith("account.positionUpdate"):
                symbol = data.get("s") or data.get("symbol")
                q = float(data.get("q") or data.get("quantity") or 0.0)
                if symbol:
                    await self._cache.set_position(symbol, q)
            elif stream.startswith("account.orderUpdate"):
                self._logger.info("order_update", extra={"venue": "backpack", "data": data})
                # Ingest into order service when client order id is present
                await self._ingest_order_update(data)
        except Exception as exc:
            self._logger.info("ws_handle_error", extra={"venue": "backpack", "error": str(exc)})

    async def _ingest_order_update(self, data: Dict[str, Any]) -> None:
        if not self._on_order_update:
            return
        try:
            coi = data.get("c") or data.get("clientId")
            if coi is None:
                return
            try:
                client_order_index = int(coi)
            except Exception:
                return
            # Map order state
            state_raw = (data.get("X") or data.get("status") or "").lower()
            mapping = {
                "new": OrderState.OPEN,
                "accepted": OrderState.OPEN,
                "open": OrderState.OPEN,
                "partially_filled": OrderState.PARTIALLY_FILLED,
                "partiallyfilled": OrderState.PARTIALLY_FILLED,
                "filled": OrderState.FILLED,
                "cancelled": OrderState.CANCELLED,
                "canceled": OrderState.CANCELLED,
                "failed": OrderState.FAILED,
                "rejected": OrderState.FAILED,
            }
            state = mapping.get(state_raw)
            if state is None:
                # Derive from event type when needed
                et = (data.get("e") or "").lower()
                if et == "orderfill":
                    state = OrderState.FILLED if str(data.get("q") or "") == str(data.get("z") or "") else OrderState.PARTIALLY_FILLED
                elif et in ("ordercancelled", "orderexpired"):
                    state = OrderState.CANCELLED
                elif et in ("orderaccepted",):
                    state = OrderState.OPEN
                else:
                    state = OrderState.OPEN
            payload = OrderUpdatePayload(
                client_order_index=client_order_index,
                state=state,
                exchange_order_id=str(data.get("i") or ""),
                info=data,
            )
            await self._on_order_update(payload)
        except Exception as exc:
            self._logger.info("ws_ingest_error", extra={"venue": "backpack", "error": str(exc)})


__all__ = ["BackpackWsClient"]
