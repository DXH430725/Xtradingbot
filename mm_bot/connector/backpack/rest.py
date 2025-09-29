from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from nacl.encoding import RawEncoder

from mm_bot.execution.orders import OrderState, TrackingLimitOrder, TrackingMarketOrder


class BackpackRESTMixin:
    """REST helpers shared by the Backpack connector."""

    async def _ensure_markets(self) -> None:
        if self._market_info:
            return
        await self._throttler.acquire("/api/v1/markets")
        async with self._session.get(
            self.config.base_url + "/api/v1/markets",
            headers={"X-BROKER-ID": self._broker_id},
            timeout=15,
        ) as resp:
            data = await resp.json()
        symbols = data if isinstance(data, list) else data.get("symbols")
        if not isinstance(symbols, list):
            raise RuntimeError("unexpected markets payload")
        for item in symbols:
            try:
                symbol = item.get("symbol") or item.get("name")
                if not symbol:
                    continue
                self._symbol_to_market[symbol] = symbol
                self._market_info[symbol] = item
                filters = item.get("filters") or {}
                price_filter = filters.get("price") or {}
                qty_filter = filters.get("quantity") or {}
                tick_size = str(price_filter.get("tickSize", "0.01"))
                step_size = str(qty_filter.get("stepSize", "0.0001"))
                price_dec = len(tick_size.split(".")[1]) if "." in tick_size else 0
                size_dec = len(step_size.split(".")[1]) if "." in step_size else 0
                self._price_decimals[symbol] = price_dec
                self._size_decimals[symbol] = size_dec
            except Exception:
                continue

    async def list_symbols(self) -> List[str]:
        await self._ensure_markets()
        return list(self._symbol_to_market.keys())

    async def get_market_id(self, symbol: str) -> str:
        await self._ensure_markets()
        try:
            return self._symbol_to_market[symbol]
        except KeyError as exc:
            raise ValueError(f"Unknown symbol {symbol}") from exc

    async def get_price_size_decimals(self, symbol: str) -> Tuple[int, int]:
        await self._ensure_markets()
        return self._price_decimals.get(symbol, 2), self._size_decimals.get(symbol, 6)

    async def best_effort_latency_ms(self) -> float:
        t0 = time.perf_counter()
        await self._throttler.acquire("/api/v1/status")
        async with self._session.get(
            self.config.base_url + "/api/v1/status",
            headers={"X-BROKER-ID": self._broker_id},
            timeout=10,
        ) as resp:
            await resp.text()
        return (time.perf_counter() - t0) * 1000.0

    def _auth_headers(self, instruction: str, params: Dict[str, Any]) -> Dict[str, str]:
        ts = int(time.time() * 1000)
        window = int(self.config.window_ms)
        encoded: List[Tuple[str, Any]] = []
        for key, value in (params or {}).items():
            if isinstance(value, bool):
                encoded.append((key, str(value).lower()))
            else:
                encoded.append((key, value))
        items = sorted(encoded)
        if items:
            kv = "&".join([f"{k}={v}" for k, v in items])
            msg = f"instruction={instruction}&{kv}&timestamp={ts}&window={window}"
        else:
            msg = f"instruction={instruction}&timestamp={ts}&window={window}"
        signature = self._signing_key.sign(msg.encode("utf-8"), encoder=RawEncoder).signature
        return {
            "X-API-KEY": str(self._api_pub or ""),
            "X-TIMESTAMP": str(ts),
            "X-WINDOW": str(window),
            "X-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "X-BROKER-ID": self._broker_id,
            "Content-Type": "application/json",
        }

    async def get_account_overview(self) -> Dict[str, Any]:
        await self._throttler.acquire("/api/v1/account")
        headers = self._auth_headers("accountQuery", {})
        async with self._session.get(self.config.base_url + "/api/v1/account", headers=headers, timeout=15) as resp:
            return await resp.json()

    async def get_balances(self) -> Dict[str, Any]:
        await self._throttler.acquire("/api/v1/capital")
        headers = self._auth_headers("balanceQuery", {})
        async with self._session.get(
            self.config.base_url + "/api/v1/capital",
            headers=headers,
            timeout=15,
        ) as resp:
            if resp.status != 200:
                return {}
            return await resp.json()

    async def get_collateral(self) -> Dict[str, Any]:
        await self._throttler.acquire("/api/v1/capital/collateral")
        headers = self._auth_headers("collateralQuery", {})
        async with self._session.get(
            self.config.base_url + "/api/v1/capital/collateral",
            headers=headers,
            timeout=15,
        ) as resp:
            if resp.status != 200:
                return {}
            return await resp.json()

    async def get_positions(self) -> List[Dict[str, Any]]:
        if self._positions_by_symbol:
            out: List[Dict[str, Any]] = []
            for sym, pos in self._positions_by_symbol.items():
                item = dict(pos)
                item.setdefault("symbol", sym)
                out.append(item)
            return out
        try:
            await self._throttler.acquire("/api/v1/positions")
            headers = self._auth_headers("positionQuery", {})
            async with self._session.get(
                self.config.base_url + "/api/v1/positions",
                headers=headers,
                timeout=15,
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
            return data.get("positions", []) if isinstance(data, dict) else []
        except Exception:
            return []

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        await self._ensure_markets()
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        await self._throttler.acquire("/api/v1/orders")
        headers = self._auth_headers("orderQueryAll", params)
        async with self._session.get(
            self.config.base_url + "/api/v1/orders",
            params=params,
            headers=headers,
            timeout=15,
        ) as resp:
            data = await resp.json()
        if isinstance(data, dict) and isinstance(data.get("orders"), list):
            return data["orders"]
        if isinstance(data, list):
            return data
        return []

    async def get_market_info(self, symbol: str) -> Dict[str, Any]:
        await self._ensure_markets()
        info = self._market_info.get(symbol)
        if not info:
            raise ValueError(f"Unknown symbol {symbol}")
        filters = info.get("filters") or {}
        price_filter = filters.get("price") or {}
        qty_filter = filters.get("quantity") or {}
        min_qty = float(qty_filter.get("min", qty_filter.get("minQty", qty_filter.get("minQuantity", 0.0))))
        min_step = float(qty_filter.get("stepSize", qty_filter.get("step", 0.0)) or 0.0)
        tick_size = float(price_filter.get("tickSize", price_filter.get("tick", 0.0)) or 0.0)
        return {
            "symbol": symbol,
            "base_asset": info.get("baseAsset") or info.get("baseAssetSymbol") or info.get("baseAssetName"),
            "quote_asset": info.get("quoteAsset") or info.get("quoteAssetSymbol") or info.get("quoteAssetName"),
            "tick_size": tick_size,
            "step_size": min_step,
            "min_qty": min_qty,
            "price_precision": self._price_decimals.get(symbol, 2),
            "quantity_precision": self._size_decimals.get(symbol, 6),
        }

    async def get_order_book(self, symbol: str, depth: int = 50) -> Dict[str, Any]:
        await self._ensure_markets()
        params = {"symbol": symbol, "limit": max(1, min(depth, 200))}
        await self._throttler.acquire("/api/v1/depth")
        async with self._session.get(
            self.config.base_url + "/api/v1/depth",
            params=params,
            headers={"X-BROKER-ID": self._broker_id},
            timeout=10,
        ) as resp:
            if resp.status != 200:
                return {}
            return await resp.json()

    async def get_top_of_book(self, symbol: str) -> Tuple[Optional[int], Optional[int], int]:
        await self._ensure_markets()
        key = str(symbol).upper()
        cached = self._top_of_book_cache.get(key)
        now = time.monotonic()
        if cached and now - cached[3] <= 3.0:
            return cached[0], cached[1], cached[2]
        event = self._top_of_book_events.get(key)
        if event and not event.is_set():
            try:
                await asyncio.wait_for(event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            cached = self._top_of_book_cache.get(key)
            now = time.monotonic()
            if cached and now - cached[3] <= 3.0:
                return cached[0], cached[1], cached[2]
        await self._load_depth_snapshot(key)
        cached = self._top_of_book_cache.get(key)
        if cached:
            return cached[0], cached[1], cached[2]
        price_decimals, _ = await self.get_price_size_decimals(symbol)
        scale = 10 ** int(price_decimals)
        return None, None, scale

    async def get_last_price(self, symbol: str) -> Optional[float]:
        await self._ensure_markets()
        params = {"symbol": symbol}
        await self._throttler.acquire("/api/v1/ticker")
        async with self._session.get(
            self.config.base_url + "/api/v1/ticker",
            params=params,
            headers={"X-BROKER-ID": self._broker_id},
            timeout=10,
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        if isinstance(data, dict):
            price = data.get("lastPrice") or data.get("last_price") or data.get("price")
            try:
                return float(price)
            except (TypeError, ValueError):
                return None
        return None

    def _interpret_execution_response(self, payload: Any) -> Tuple[Optional[str], Optional[str]]:
        if not isinstance(payload, dict):
            return None, "invalid_response"

        order_section: Dict[str, Any] = payload
        nested = payload.get("order")
        if isinstance(nested, dict):
            order_section = nested

        order_id = (
            order_section.get("id")
            or order_section.get("orderId")
            or order_section.get("order_id")
        )

        status_raw = order_section.get("status", payload.get("status"))
        status = str(status_raw).lower() if status_raw is not None else ""
        code = payload.get("code")

        success = False
        if order_id is not None:
            success = True
        elif code in {0, "0", 200, "200"}:
            success = True
        elif status in {"success", "ok", "accepted", "open", "new", "working"}:
            success = True

        if success:
            return (str(order_id) if order_id is not None else None), None

        message = (
            payload.get("message")
            or payload.get("error")
            or order_section.get("message")
            or order_section.get("error")
        )
        return None, str(message) if message is not None else "order_rejected"

    async def place_limit(
        self,
        symbol: str,
        client_order_index: int,
        base_amount: int,
        price: int,
        is_ask: bool,
        post_only: bool = False,
        reduce_only: int = 0,
    ) -> Tuple[Any, Any, Optional[str]]:
        await self._ensure_markets()
        price_decimals, size_decimals = await self.get_price_size_decimals(symbol)
        qty = float(base_amount) / (10 ** size_decimals)
        price_f = float(price) / (10 ** price_decimals)
        payload = {
            "symbol": symbol,
            "side": "Ask" if is_ask else "Bid",
            "orderType": "Limit",
            "quantity": f"{qty:.{size_decimals}f}",
            "price": f"{price_f:.{price_decimals}f}",
            "timeInForce": "GTC",
            "clientId": int(client_order_index),
        }
        if reduce_only:
            payload["reduceOnly"] = True
        if post_only:
            payload["postOnly"] = True
        headers = self._auth_headers("orderExecute", payload)
        self.create_tracking_limit_order(
            client_order_index,
            symbol=symbol,
            is_ask=is_ask,
            price_i=price,
            size_i=base_amount,
        )
        self._update_order_state(
            client_order_id=client_order_index,
            symbol=symbol,
            is_ask=is_ask,
            state=OrderState.SUBMITTING,
            info={"request": payload},
        )
        await self._throttler.acquire("/api/v1/order")
        async with self._session.post(
            self.config.base_url + "/api/v1/order",
            headers=headers,
            json=payload,
            timeout=20,
        ) as resp:
            try:
                ret = await resp.json()
            except Exception:
                text = await resp.text()
                error = f"http_{resp.status}:{text[:120]}"
                self._update_order_state(
                    client_order_id=client_order_index,
                    symbol=symbol,
                    is_ask=is_ask,
                    state=OrderState.FAILED,
                    info={"error": error},
                )
                return None, None, error
        order_id, err = self._interpret_execution_response(ret)
        if err:
            self._update_order_state(
                client_order_id=client_order_index,
                symbol=symbol,
                is_ask=is_ask,
                state=OrderState.FAILED,
                info=ret if isinstance(ret, dict) else {"error": err},
            )
            return ret, ret, err
        self._update_order_state(
            client_order_id=client_order_index,
            exchange_order_id=order_id,
            symbol=symbol,
            is_ask=is_ask,
            state=OrderState.OPEN,
            info=ret if isinstance(ret, dict) else {},
        )
        return ret, ret, err

    async def submit_limit_order(
        self,
        symbol: str,
        client_order_index: int,
        base_amount: int,
        price: int,
        is_ask: bool,
        *,
        post_only: bool = False,
        reduce_only: int = 0,
    ) -> TrackingLimitOrder:
        tracker = self.create_tracking_limit_order(
            client_order_index,
            symbol=symbol,
            is_ask=is_ask,
            price_i=price,
            size_i=base_amount,
        )
        await self.place_limit(
            symbol=symbol,
            client_order_index=client_order_index,
            base_amount=base_amount,
            price=price,
            is_ask=is_ask,
            post_only=post_only,
            reduce_only=reduce_only,
        )
        return tracker

    async def place_market(
        self,
        symbol: str,
        client_order_index: int,
        base_amount: int,
        is_ask: bool,
        reduce_only: int = 0,
    ) -> Tuple[Any, Any, Optional[str]]:
        await self._ensure_markets()
        _price_decimals, size_decimals = await self.get_price_size_decimals(symbol)
        qty = float(base_amount) / (10 ** size_decimals)
        payload = {
            "symbol": symbol,
            "side": "Ask" if is_ask else "Bid",
            "orderType": "Market",
            "quantity": f"{qty:.{size_decimals}f}",
            "clientId": int(client_order_index),
        }
        if reduce_only:
            payload["reduceOnly"] = True
        headers = self._auth_headers("orderExecute", payload)
        self.create_tracking_market_order(
            client_order_index,
            symbol=symbol,
            is_ask=is_ask,
            size_i=base_amount,
        )
        self._update_order_state(
            client_order_id=client_order_index,
            symbol=symbol,
            is_ask=is_ask,
            state=OrderState.SUBMITTING,
            info={"request": payload},
        )
        await self._throttler.acquire("/api/v1/order")
        async with self._session.post(
            self.config.base_url + "/api/v1/order",
            headers=headers,
            json=payload,
            timeout=20,
        ) as resp:
            try:
                ret = await resp.json()
            except Exception:
                text = await resp.text()
                error = f"http_{resp.status}:{text[:120]}"
                self._update_order_state(
                    client_order_id=client_order_index,
                    symbol=symbol,
                    is_ask=is_ask,
                    state=OrderState.FAILED,
                    info={"error": error},
                )
                return None, None, error
        order_id, err = self._interpret_execution_response(ret)
        if err:
            self._update_order_state(
                client_order_id=client_order_index,
                symbol=symbol,
                is_ask=is_ask,
                state=OrderState.FAILED,
                info=ret if isinstance(ret, dict) else {"error": err},
            )
            return ret, ret, err
        self._update_order_state(
            client_order_id=client_order_index,
            exchange_order_id=order_id,
            symbol=symbol,
            is_ask=is_ask,
            state=OrderState.OPEN,
            info=ret if isinstance(ret, dict) else {},
        )
        return ret, ret, err

    async def submit_market_order(
        self,
        symbol: str,
        client_order_index: int,
        base_amount: int,
        is_ask: bool,
        *,
        reduce_only: int = 0,
    ) -> TrackingMarketOrder:
        tracker = self.create_tracking_market_order(
            client_order_index,
            symbol=symbol,
            is_ask=is_ask,
            size_i=base_amount,
        )
        await self.place_market(
            symbol=symbol,
            client_order_index=client_order_index,
            base_amount=base_amount,
            is_ask=is_ask,
            reduce_only=reduce_only,
        )
        return tracker

    async def cancel_order(self, order_index: str, symbol: Optional[str] = None) -> Tuple[Any, Any, Optional[str]]:
        await self._ensure_markets()
        payload = {"orderId": order_index}
        if symbol:
            payload["symbol"] = symbol
        headers = self._auth_headers("orderCancel", payload)
        await self._throttler.acquire("/api/v1/order/cancel")
        async with self._session.post(
            self.config.base_url + "/api/v1/order/cancel",
            headers=headers,
            json=payload,
            timeout=20,
        ) as resp:
            try:
                data = await resp.json()
            except Exception:
                text = await resp.text()
                data = {"status": resp.status, "message": text[:120]}
        status = str(data.get("status", "")).lower() if isinstance(data, dict) else ""
        if resp.status == 200 and status in {"success", "ok", "cancelled", "canceled"}:
            return data, data, None
        orders = await self.get_open_orders(symbol)
        last_err = None
        for order in orders:
            try:
                oid = str(order.get("id") or order.get("orderId"))
                sym = order.get("symbol") or symbol
                _, _, err = await self.cancel_order(oid, sym)
                if err:
                    last_err = err
            except Exception as exc:
                last_err = str(exc)
        return data, data, last_err

    async def get_order(
        self,
        symbol: str,
        order_id: Optional[str] = None,
        client_id: Optional[int] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        if not order_id and client_id is None:
            return None, "missing_id"
        params: Dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if client_id is not None:
            params["clientId"] = int(client_id)
        headers = self._auth_headers("orderQuery", params)
        await self._throttler.acquire("/api/v1/order")
        async with self._session.get(
            self.config.base_url + "/api/v1/order",
            params=params,
            headers=headers,
            timeout=15,
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                return None, f"http_{resp.status}:{text[:120]}"
            data = await resp.json()
        if isinstance(data, dict):
            order = data.get("order") or data
            return order, None
        return None, "order_not_found"

    async def cancel_all(self, symbol: Optional[str] = None) -> Tuple[Any, Any, Optional[str]]:
        payload: Dict[str, Any] = {}
        if symbol:
            payload["symbol"] = symbol
        headers = self._auth_headers("cancelAll", payload)
        await self._throttler.acquire("/api/v1/orders")
        async with self._session.delete(
            self.config.base_url + "/api/v1/orders",
            headers=headers,
            json=payload,
            timeout=20,
        ) as resp:
            try:
                data = await resp.json()
            except Exception:
                text = await resp.text()
                data = {"status": resp.status, "message": text[:120]}
        status = str(data.get("status", "")).lower() if isinstance(data, dict) else ""
        if resp.status == 200 and status in {"success", "ok", "cancelled", "canceled"}:
            return data, data, None
        orders = await self.get_open_orders(symbol)
        last_err = None
        for order in orders:
            try:
                oid = str(order.get("id") or order.get("orderId"))
                sym = order.get("symbol") or symbol
                _, _, err = await self.cancel_order(oid, sym)
                if err:
                    last_err = err
            except Exception as exc:
                last_err = str(exc)
        return data, data, last_err

    async def cancel_by_client_id(self, symbol: str, client_id: int) -> Tuple[Any, Any, Optional[str]]:
        # Try direct cancel with clientId first (faster)
        await self._ensure_markets()
        payload = {"clientId": int(client_id), "symbol": symbol}
        headers = self._auth_headers("orderCancel", payload)
        await self._throttler.acquire("/api/v1/order/cancel")

        async with self._session.post(
            self.config.base_url + "/api/v1/order/cancel",
            headers=headers,
            json=payload,
            timeout=20,
        ) as resp:
            try:
                data = await resp.json()
                if resp.status == 200:
                    return data, None, None
                # If clientId cancel failed, fall back to the old method
            except Exception:
                pass

        # Fallback: get order first, then cancel by orderId
        try:
            order, err = await self.get_order(symbol, client_id=int(client_id))
        except Exception as exc:
            return None, None, str(exc)
        if err:
            return None, None, err
        if not order or order.get("id") is None:
            return None, None, "order_not_found"
        order_id = order.get("id") or order.get("orderId")
        return await self.cancel_order(order_id, symbol)


__all__ = ["BackpackRESTMixin"]
