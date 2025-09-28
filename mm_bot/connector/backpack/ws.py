from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any, Dict, List, Optional

import websockets
from nacl.encoding import RawEncoder


class BackpackWSMixin:
    """Websocket helpers and depth cache management for Backpack."""

    async def start_ws_state(self, symbols: Optional[List[str]] = None) -> None:
        if self._ws_task and not self._ws_task.done():
            return
        targets = [str(sym).upper() for sym in (symbols or []) if sym]
        for sym in targets:
            self._top_of_book_events[sym] = asyncio.Event()
            self._top_of_book_cache.pop(sym, None)
            self._depth_state.pop(sym, None)
        if targets:
            await asyncio.gather(*(self._prime_depth_cache(sym) for sym in targets))
        self._ws_stop = False
        self._ws_task = asyncio.create_task(self._ws_loop(targets), name="backpack_ws")

    async def stop_ws_state(self) -> None:
        self._ws_stop = True
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        self._ws_task = None

    async def _ws_loop(self, symbols: List[str]) -> None:
        while not self._ws_stop:
            try:
                async with websockets.connect(self.config.ws_url, ping_interval=20) as ws:
                    subscriptions = []
                    for symbol in symbols:
                        subscriptions.append(
                            {
                                "method": "SUBSCRIBE",
                                "params": [f"depth.1000ms.{symbol}"],
                                "id": int(time.time()) % 1_000_000,
                            }
                        )
                    subscriptions.extend(self._private_ws_subscriptions(symbols))
                    for payload in subscriptions:
                        await ws.send(json.dumps(payload))
                    while not self._ws_stop:
                        raw = await ws.recv()
                        if isinstance(raw, (bytes, bytearray)):
                            continue
                        try:
                            message = json.loads(raw)
                        except Exception:
                            continue
                        stream = str(message.get("stream") or "").lower()
                        payload = message.get("data") if isinstance(message, dict) else None
                        if not isinstance(payload, dict):
                            payload = message if isinstance(message, dict) else None
                        if not isinstance(payload, dict):
                            continue
                        if stream.startswith("account.orderupdate") or payload.get("e", "").lower().startswith("order"):
                            self._handle_private_order_event(payload)
                            continue
                        if stream.startswith("account.positionupdate") or payload.get("e", "").lower().startswith("position"):
                            self._handle_private_position_event(payload)
                            continue
                        if stream.startswith("depth.") or str(payload.get("e", "")).lower() == "depth":
                            await self._handle_depth_event(payload)
                            continue
                        if stream.endswith("orderupdate") or payload.get("id") or payload.get("clientId"):
                            self._handle_private_order_event(payload)
                            continue
                        if stream.endswith("positionupdate"):
                            self._handle_private_position_event(payload)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(1.0)

    def _private_ws_subscriptions(self, symbols: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        if not self._signing_key:
            return []
        streams = {"account.positionUpdate"}
        if symbols:
            for sym in symbols:
                if not sym:
                    continue
                symbol = str(sym).upper()
                streams.add(f"account.orderUpdate.{symbol}")
                streams.add(f"account.positionUpdate.{symbol}")
        else:
            streams.add("account.orderUpdate")
        payloads: List[Dict[str, Any]] = []
        window = int(self.config.window_ms)
        verifying = self._api_pub or self._verifying_key_b64 or ""
        for stream in streams:
            ts = int(time.time() * 1000)
            msg = f"instruction=subscribe&timestamp={ts}&window={window}"
            try:
                signature = self._signing_key.sign(msg.encode("utf-8"), encoder=RawEncoder).signature
            except Exception:
                continue
            payloads.append(
                {
                    "method": "SUBSCRIBE",
                    "params": [stream],
                    "signature": [verifying, base64.b64encode(signature).decode("utf-8"), str(ts), str(window)],
                }
            )
        return payloads

    def _handle_private_order_event(self, payload: Dict[str, Any]) -> None:
        normalized = dict(payload)
        if "X" in payload and "status" not in normalized:
            normalized["status"] = payload.get("X")
        if "c" in payload and "clientId" not in normalized:
            normalized["clientId"] = payload.get("c")
        if "i" in payload:
            normalized.setdefault("id", payload.get("i"))
        if "s" in payload:
            normalized.setdefault("symbol", payload.get("s"))

        status = str(normalized.get("status", "") or "").lower()
        event = str(payload.get("e", "") or "").lower()
        if status:
            normalized["status"] = status
        if event:
            normalized["event"] = event

        client_raw = normalized.get("clientId") or normalized.get("clientID") or normalized.get("c")
        try:
            client_id = int(client_raw) if client_raw is not None else None
        except (TypeError, ValueError):
            client_id = None
        exch_id = normalized.get("id") or normalized.get("orderId") or normalized.get("i")
        symbol = normalized.get("symbol") or normalized.get("s")
        side = normalized.get("side") or normalized.get("S")
        is_ask = None
        if isinstance(side, str):
            side_lower = side.lower()
            if side_lower in {"ask", "sell", "s"}:
                is_ask = True
            elif side_lower in {"bid", "buy", "b"}:
                is_ask = False

        filled_qty = normalized.get("filledQuantity") or normalized.get("z")
        remaining_qty = normalized.get("remainingQuantity") or normalized.get("l")
        try:
            filled = float(filled_qty) if filled_qty is not None else None
        except (TypeError, ValueError):
            filled = None
        try:
            remaining = float(remaining_qty) if remaining_qty is not None else None
        except (TypeError, ValueError):
            remaining = None

        self._update_order_state(
            client_order_id=client_id,
            exchange_order_id=str(exch_id) if exch_id is not None else None,
            status=status,
            symbol=symbol,
            is_ask=is_ask,
            filled_base=filled,
            remaining_base=remaining,
            info=normalized,
        )

    def _handle_private_position_event(self, payload: Dict[str, Any]) -> None:
        normalized = dict(payload)
        symbol = payload.get("symbol") or payload.get("s")
        qty_val = payload.get("q") or payload.get("netQuantity")
        try:
            normalized["position"] = float(qty_val)
        except Exception:
            pass
        if symbol:
            normalized["symbol"] = symbol
            self._positions_by_symbol[str(symbol).upper()] = normalized
        self.emit_position(normalized)

    async def _prime_depth_cache(self, symbol: str) -> None:
        try:
            await self._load_depth_snapshot(symbol)
        except Exception as exc:
            self.log.warning("%s depth snapshot failed: %s", symbol, exc)

    async def _load_depth_snapshot(self, symbol: str) -> None:
        await self._ensure_markets()
        params = {"symbol": symbol, "limit": 200}
        await self._throttler.acquire("/api/v1/depth")
        async with self._session.get(
            self.config.base_url + "/api/v1/depth",
            params=params,
            headers={"X-BROKER-ID": self._broker_id},
            timeout=10,
        ) as resp:
            data = await resp.json()
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        last_update_id = data.get("lastUpdateId")
        scale = self._resolve_depth_scale(symbol, asks, bids)
        state = self._depth_state.setdefault(
            symbol,
            {"scale": scale, "bids": {}, "asks": {}, "last_update_id": 0},
        )
        state["scale"] = scale
        state["bids"] = self._levels_to_map(bids, scale)
        state["asks"] = self._levels_to_map(asks, scale)
        if last_update_id is not None:
            state["last_update_id"] = int(last_update_id)
        bid_i = self._best_bid(state)
        ask_i = self._best_ask(state)
        ts = time.monotonic()
        self._top_of_book_cache[symbol] = (bid_i, ask_i, scale, ts)
        event = self._top_of_book_events.get(symbol)
        if event and not event.is_set():
            event.set()

    async def _handle_depth_event(self, payload: Dict[str, Any]) -> None:
        symbol_raw = payload.get("s") or payload.get("symbol")
        if not symbol_raw:
            return
        symbol = str(symbol_raw).upper()
        state = self._depth_state.get(symbol)
        if state is None:
            await self._load_depth_snapshot(symbol)
            state = self._depth_state.get(symbol)
            if state is None:
                return
        bids = payload.get("b") or payload.get("bids") or []
        asks = payload.get("a") or payload.get("asks") or []
        first_id = self._safe_int(payload.get("U"))
        last_id = self._safe_int(payload.get("u"))
        prev = state.get("last_update_id", 0)
        if last_id is not None and prev and last_id <= prev:
            return
        if first_id is not None and prev and first_id > prev + 1:
            await self._load_depth_snapshot(symbol)
            state = self._depth_state.get(symbol)
            if state is None:
                return
        scale = state.get("scale", self._resolve_depth_scale(symbol, asks, bids))
        if bids:
            self._apply_depth_levels(state["bids"], bids, scale)
        if asks:
            self._apply_depth_levels(state["asks"], asks, scale)
        if last_id is not None:
            state["last_update_id"] = last_id
        bid_i = self._best_bid(state)
        ask_i = self._best_ask(state)
        if bid_i is None and ask_i is None:
            return
        ts = time.monotonic()
        self._top_of_book_cache[symbol] = (bid_i, ask_i, scale, ts)
        event = self._top_of_book_events.get(symbol)
        if event and not event.is_set():
            event.set()

    def _resolve_depth_scale(self, symbol: str, asks: Any, bids: Any) -> int:
        decimals = self._price_decimals.get(symbol)
        if decimals is not None:
            try:
                return 10 ** int(decimals)
            except Exception:
                pass
        candidate = self._first_price_value(asks)
        if candidate is None:
            candidate = self._first_price_value(bids)
        if candidate is not None:
            text = str(candidate)
            if "." in text:
                try:
                    return 10 ** len(text.split(".", 1)[1])
                except Exception:
                    pass
        return 1

    def _first_price_value(self, entries: Any) -> Optional[Any]:
        if not isinstance(entries, list):
            return None
        for item in entries:
            if isinstance(item, (list, tuple)) and item:
                return item[0]
        return None

    def _levels_to_map(self, entries: Any, scale: int) -> Dict[int, float]:
        out: Dict[int, float] = {}
        if not isinstance(entries, list):
            return out
        for item in entries:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            price_i = self._price_to_int(item[0], scale)
            if price_i is None:
                continue
            qty = self._safe_float(item[1])
            if qty is None or qty <= 0:
                continue
            out[price_i] = qty
        return out

    def _apply_depth_levels(self, book: Dict[int, float], updates: Any, scale: int) -> None:
        if not isinstance(updates, list):
            return
        for item in updates:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            price_i = self._price_to_int(item[0], scale)
            if price_i is None:
                continue
            qty = self._safe_float(item[1])
            if qty is None or qty <= 0:
                book.pop(price_i, None)
            else:
                book[price_i] = qty

    def _price_to_int(self, value: Any, scale: int) -> Optional[int]:
        try:
            return int(round(float(value) * scale))
        except (TypeError, ValueError):
            return None

    def _safe_float(self, value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _safe_int(self, value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _best_bid(self, state: Dict[str, Any]) -> Optional[int]:
        bids = state.get("bids", {})
        return max(bids.keys()) if bids else None

    def _best_ask(self, state: Dict[str, Any]) -> Optional[int]:
        asks = state.get("asks", {})
        return min(asks.keys()) if asks else None


__all__ = ["BackpackWSMixin"]
