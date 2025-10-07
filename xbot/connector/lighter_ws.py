from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Optional, Dict, Any, Callable, Awaitable

import websockets

from xbot.core.cache import MarketCache
from xbot.utils.logging import get_logger
from xbot.execution.order_service import OrderUpdatePayload
from xbot.execution.models import OrderState


class LighterWsClient:
    """Lighter WebSocket client with reconnect, trades, and account updates."""

    WS_URL = "wss://mainnet.zklighter.elliot.ai/stream"

    def __init__(
        self,
        *,
        market_index: int,
        venue_symbol: str,
        account_index: Optional[int],
        cache: MarketCache,
        key_file: Optional[Path] = None,
        base_url: str = "https://mainnet.zklighter.elliot.ai",
        reconnect_delay: float = 3.0,
        ping_interval: float = 55.0,
        ping_timeout: float = 10.0,
        on_order_update: Optional[Callable[[OrderUpdatePayload], Awaitable[None]]] = None,
    ) -> None:
        self._market_index = market_index
        self._venue_symbol = venue_symbol
        self._account_index = account_index
        self._cache = cache
        self._key_file = key_file
        self._base_url = base_url
        self._reconnect_delay = reconnect_delay
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._on_order_update = on_order_update
        self._logger = get_logger(__name__)
        self._running = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

        self._bids: Dict[float, float] = {}
        self._asks: Dict[float, float] = {}
        self._ob_offset: Optional[int] = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running.set()
        self._task = asyncio.create_task(self._run(), name="lighter-ws")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._running.clear()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def _load_keys(self) -> Dict[str, str]:
        try:
            if not self._key_file or not self._key_file.exists():
                return {}
            content = self._key_file.read_text(encoding="utf-8")
            lines = dict(line.split(":", 1) for line in content.splitlines() if ":" in line)
            return {k.strip().lower(): v.strip() for k, v in lines.items()}
        except Exception:
            return {}

    async def _build_auth_token(self) -> Optional[str]:
        try:
            if self._account_index is None:
                return None
            keys = self._load_keys()

            def _kg(*names: str) -> Optional[str]:
                for n in names:
                    if n in keys and keys[n]:
                        return keys[n]
                return None

            priv = _kg("api_key_private_key", "apikeyprivatekey", "private_key", "privatekey") or ""
            if not priv:
                self._logger.info(
                    "ws_auth_missing_priv",
                    extra={"venue": "lighter", "key_file": str(self._key_file) if self._key_file else None},
                )
                return None
            import lighter  # type: ignore
            acct_idx = int(_kg("account_index", "accountindex") or 0)
            key_idx = int(_kg("api_key_index", "apikeyindex") or 0)
            self._logger.info(
                "ws_auth_params",
                extra={"venue": "lighter", "account_index": acct_idx, "api_key_index": key_idx},
            )
            signer = lighter.SignerClient(
                url=self._base_url,
                private_key=priv,
                account_index=acct_idx,
                api_key_index=key_idx,
            )
            deadline = int(time.time() + 10 * 60)
            token, err = signer.create_auth_token_with_expiry(deadline)
            try:
                if getattr(signer, "api_client", None) is not None:
                    await signer.api_client.close()  # type: ignore[attr-defined]
            except Exception:
                pass
            if err is not None:
                self._logger.info("ws_auth_error", extra={"venue": "lighter", "error": str(err)})
                return None
            return token
        except Exception as exc:
            try:
                import lighter  # type: ignore
                default_client = lighter.ApiClient.get_default()
                if default_client is not None:
                    await default_client.close()
                    lighter.ApiClient.set_default(None)  # type: ignore[arg-type]
            except Exception:
                pass
            self._logger.info("ws_auth_exception", extra={"venue": "lighter", "error": str(exc)})
            return None

    async def _subscribe(self, ws, private: bool, auth_token: Optional[str]) -> None:
        await ws.send(
            json.dumps({"type": "subscribe", "channel": f"order_book/{self._market_index}"})
        )
        with contextlib.suppress(Exception):
            await ws.send(
                json.dumps({"type": "subscribe", "channel": f"trade/{self._market_index}"})
            )
        if self._account_index is not None:
            if private and auth_token:
                await ws.send(
                    json.dumps(
                        {
                            "type": "subscribe",
                            "channel": f"account_orders/{self._market_index}/{self._account_index}",
                            "auth": auth_token,
                        }
                    )
                )
            # Fallback aggregate account stream
            with contextlib.suppress(Exception):
                payload = {"type": "subscribe", "channel": f"account_all/{self._account_index}"}
                if private and auth_token:
                    payload["auth"] = auth_token
                await ws.send(json.dumps(payload))

    def _apply_ob_updates(self, side: str, updates: list[dict]) -> None:
        book = self._bids if side == "bids" else self._asks
        for u in updates or []:
            try:
                px = float(u.get("price"))
                sz = float(u.get("size"))
                if sz <= 0:
                    book.pop(px, None)
                else:
                    book[px] = sz
            except Exception:
                continue

    async def _run(self) -> None:
        backoff = self._reconnect_delay
        while self._running.is_set():
            auth_token = await self._build_auth_token()
            has_private = bool(auth_token and self._account_index is not None)
            try:
                self._logger.info(
                    "ws_connecting",
                    extra={
                        "venue": "lighter",
                        "streams": [
                            f"order_book/{self._market_index}",
                            f"trade/{self._market_index}",
                            (
                                f"account_orders/{self._market_index}/{self._account_index}"
                                if (auth_token and self._account_index is not None)
                                else None
                            ),
                        ],
                        "has_private": has_private,
                    },
                )
                async with websockets.connect(
                    self.WS_URL,
                    ping_interval=self._ping_interval,
                    ping_timeout=self._ping_timeout,
                    max_size=2 ** 22,
                ) as ws:
                    await self._subscribe(ws, private=has_private, auth_token=auth_token)
                    self._logger.info(
                        "ws_connected", extra={"venue": "lighter", "has_private": has_private}
                    )
                    self._bids.clear()
                    self._asks.clear()
                    self._ob_offset = None
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        await self._handle_message(msg)
                backoff = self._reconnect_delay
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._logger.info("ws_error", extra={"venue": "lighter", "error": str(exc)})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _handle_message(self, msg: Dict[str, Any]) -> None:
        et = msg.get("type") or msg.get("event")
        try:
            if et == "subscribed/order_book":
                ob = msg.get("order_book") or {}
                self._ob_offset = ob.get("offset")
                self._bids.clear()
                self._asks.clear()
                self._apply_ob_updates("bids", ob.get("bids") or [])
                self._apply_ob_updates("asks", ob.get("asks") or [])
                await self._publish_top()
            elif et == "update/order_book":
                ob = msg.get("order_book") or {}
                new_offset = ob.get("offset")
                if isinstance(self._ob_offset, int) and isinstance(new_offset, int):
                    if new_offset == self._ob_offset:
                        return
                    if new_offset > self._ob_offset + 1:
                        raise RuntimeError(
                            f"order_book offset gap: have {self._ob_offset}, got {new_offset}"
                        )
                self._apply_ob_updates("bids", ob.get("bids") or [])
                self._apply_ob_updates("asks", ob.get("asks") or [])
                self._ob_offset = new_offset if isinstance(new_offset, int) else self._ob_offset
                await self._publish_top()
            elif et and (et.startswith("trade") or et == "update/trade"):
                if isinstance(msg.get("trades"), list):
                    for tr in msg["trades"][:10]:
                        norm = {
                            "p": tr.get("price") or tr.get("p"),
                            "q": tr.get("size") or tr.get("q"),
                            "t": tr.get("ts") or tr.get("t"),
                            "m": tr.get("is_maker") or tr.get("m"),
                        }
                        await self._cache.add_trade(self._venue_symbol, norm)
                else:
                    trade = msg.get("trade") or msg.get("data") or {}
                    norm = {
                        "p": trade.get("price") or trade.get("p"),
                        "q": trade.get("size") or trade.get("q"),
                        "t": trade.get("ts") or trade.get("t"),
                        "m": trade.get("is_maker") or trade.get("m"),
                    }
                    await self._cache.add_trade(self._venue_symbol, norm)
            elif et in ("subscribed/account_orders", "update/account_orders") or (
                isinstance(et, str) and et.startswith("update/account")
            ) or (
                isinstance(et, str) and et.startswith("subscribed/account")
            ):
                await self._handle_account_msg(msg)
        except Exception as exc:
            self._logger.info("ws_handle_error", extra={"venue": "lighter", "error": str(exc)})

    async def _publish_top(self) -> None:
        top_b = max(self._bids.keys()) if self._bids else None
        top_a = min(self._asks.keys()) if self._asks else None
        await self._cache.set_top(self._venue_symbol, top_b, top_a)

    async def _handle_account_msg(self, msg: Dict[str, Any]) -> None:
        # Orders can arrive under various shapes: order_updates, orders,
        # nested in data/account, dict-of-lists, or dict keyed by market_index.
        parsed_any = False
        candidates: list[Any] = []
        # top-level
        candidates.append(msg.get("order_updates"))
        candidates.append(msg.get("orders"))
        # nested data
        data = msg.get("data")
        if isinstance(data, dict):
            candidates.append(data.get("order_updates"))
            candidates.append(data.get("orders"))
        # nested account
        acc = msg.get("account")
        if isinstance(acc, dict):
            candidates.append(acc.get("order_updates"))
            candidates.append(acc.get("orders"))

        def _flatten_orders(obj: Any) -> list[dict]:
            out: list[dict] = []
            if isinstance(obj, list):
                for it in obj:
                    if isinstance(it, dict):
                        out.append(it)
            elif isinstance(obj, dict):
                for v in obj.values():
                    if isinstance(v, list):
                        for it in v:
                            if isinstance(it, dict):
                                out.append(it)
                    elif isinstance(v, dict):
                        # one more level (e.g., {"2": {"created": [...], "updated": [...]}})
                        for vv in v.values():
                            if isinstance(vv, list):
                                for it in vv:
                                    if isinstance(it, dict):
                                        out.append(it)
            return out

        for cand in candidates:
            for item in _flatten_orders(cand):
                await self._ingest_order_update(item)
                parsed_any = True

        if not parsed_any:
            self._logger.info(
                "ws_account_no_orders",
                extra={"venue": "lighter", "keys": list(msg.keys())[:10]},
            )

        # Positions, guarded by type checks to avoid calling .get on non-dicts
        positions: Any = None
        if isinstance(msg.get("positions"), list):
            positions = msg.get("positions")
        elif isinstance(data, dict) and isinstance(data.get("positions"), list):
            positions = data.get("positions")
        elif isinstance(acc, dict) and isinstance(acc.get("positions"), list):
            positions = acc.get("positions")
        if isinstance(positions, list):
            for p in positions:
                try:
                    if not isinstance(p, dict):
                        continue
                    sym = p.get("symbol") or self._venue_symbol
                    pos_str = p.get("position") or p.get("net_size") or "0"
                    q = float(str(pos_str))
                    if sym:
                        await self._cache.set_position(sym, q)
                except Exception:
                    continue

    async def _ingest_order_update(self, data: Dict[str, Any]) -> None:
        if not self._on_order_update:
            return
        try:
            coi = (
                data.get("client_order_index")
                or data.get("client_order_id")
                or data.get("coi")
                or data.get("clientId")
            )
            if coi is None:
                return
            state_raw = (data.get("status") or data.get("state") or "").lower()
            mapping = {
                "open": OrderState.OPEN,
                "accepted": OrderState.OPEN,
                "in-progress": OrderState.OPEN,
                "pending": OrderState.OPEN,
                "filled": OrderState.FILLED,
                "canceled": OrderState.CANCELLED,
                "cancelled": OrderState.CANCELLED,
                "rejected": OrderState.FAILED,
                "failed": OrderState.FAILED,
            }
            state = mapping.get(state_raw)
            if state is None and state_raw.startswith("canceled"):
                state = OrderState.CANCELLED
            if state in (None, OrderState.OPEN):
                try:
                    filled_s = data.get("filled_base_amount") or data.get("filled") or "0"
                    remaining_s = data.get("remaining_base_amount") or data.get("remaining") or "0"
                    init_s = data.get("initial_base_amount") or data.get("size") or "0"
                    filled = float(str(filled_s))
                    remaining = float(str(remaining_s))
                    initial = float(str(init_s))
                    if remaining <= 0 and (filled > 0 or (initial > 0 and filled >= initial)):
                        state = OrderState.FILLED
                    elif filled > 0:
                        state = OrderState.PARTIALLY_FILLED
                except Exception:
                    pass
            if state is None:
                state = OrderState.OPEN
            exchange_order_id = str(
                data.get("order_index") or data.get("orderId") or data.get("i") or ""
            )
            payload = OrderUpdatePayload(
                client_order_index=int(coi),
                state=state,
                exchange_order_id=exchange_order_id,
                info=data,
            )
            await self._on_order_update(payload)
        except Exception as exc:
            self._logger.info("ws_ingest_error", extra={"venue": "lighter", "error": str(exc)})


__all__ = ["LighterWsClient"]
