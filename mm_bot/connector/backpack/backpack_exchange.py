import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Callable

import aiohttp
import websockets
from nacl.signing import SigningKey
from nacl.encoding import RawEncoder

from mm_bot.utils.throttler import RateLimiter, lighter_default_weights
from mm_bot.connector.base import BaseConnector
from mm_bot.execution.orders import OrderState, TrackingLimitOrder, TrackingMarketOrder


@dataclass
class BackpackConfig:
    base_url: str = "https://api.backpack.exchange"
    ws_url: str = "wss://ws.backpack.exchange"
    keys_file: str = "Backpack_key.txt"
    window_ms: int = 5000
    rpm: int = 300  # conservative default
    broker_id: str = "1500"


def load_backpack_keys(path: str) -> Tuple[Optional[str], Optional[str]]:
    pub = priv = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line.lower().startswith("api key:"):
                    pub = line.split(":", 1)[1].strip()
                elif line.lower().startswith("api secret:"):
                    priv = line.split(":", 1)[1].strip()
    except Exception:
        pass
    return pub, priv


class BackpackConnector(BaseConnector):
    def __init__(self, config: Optional[BackpackConfig] = None, debug: bool = False):
        super().__init__("backpack", debug=debug)
        self.config = config or BackpackConfig()
        self.log = logging.getLogger("mm_bot.connector.backpack")

        self._throttler = RateLimiter(capacity_per_minute=self.config.rpm, weights=lighter_default_weights())
        self._session: Optional[aiohttp.ClientSession] = None
        self._started = False

        # keys
        self._api_pub: Optional[str] = None
        self._api_priv: Optional[str] = None
        self._signing_key: Optional[SigningKey] = None
        self._verifying_key_b64: Optional[str] = None
        self._broker_id: str = self.config.broker_id

        # symbol map and scales
        self._symbol_to_market: Dict[str, str] = {}  # symbol string passthrough
        self._market_info: Dict[str, Dict[str, Any]] = {}
        self._price_decimals: Dict[str, int] = {}
        self._size_decimals: Dict[str, int] = {}

        # ws state
        self._ws_task: Optional[asyncio.Task] = None
        self._ws_stop: bool = False
        self._top_of_book_cache: Dict[str, Tuple[Optional[int], Optional[int], int, float]] = {}
        self._top_of_book_events: Dict[str, asyncio.Event] = {}
        self._depth_state: Dict[str, Dict[str, Any]] = {}

        # simple caches fed by private WS
        self._positions_by_symbol: Dict[str, Dict[str, Any]] = {}

    # lifecycle ---------------------------------------------------------------
    def start(self, core=None):
        if self._started:
            return
        pub, priv = load_backpack_keys(self.config.keys_file)
        if not pub or not priv:
            raise RuntimeError(f"Missing backpack keys; check {self.config.keys_file}")
        self._api_pub = pub
        self._api_priv = priv
        try:
            sk_bytes = base64.b64decode(priv)
            self._signing_key = SigningKey(sk_bytes)
            vk = self._signing_key.verify_key.encode(RawEncoder)
            self._verifying_key_b64 = base64.b64encode(vk).decode("utf-8")
        except Exception as e:
            raise RuntimeError(f"Invalid backpack private key: {e}")
        self._session = aiohttp.ClientSession()
        self._started = True

    async def close(self):
        await self.stop_ws_state()
        if self._session:
            await self._session.close()
            self._session = None
        self._started = False

    # event handlers ----------------------------------------------------------
    def set_event_handlers(
        self,
        *,
        on_order_filled: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_order_cancelled: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_trade: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_position_update: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        super().set_event_handlers(
            on_order_filled=on_order_filled,
            on_order_cancelled=on_order_cancelled,
            on_trade=on_trade,
            on_position_update=on_position_update,
        )

    # helpers -----------------------------------------------------------------
    async def _ensure_markets(self):
        if self._market_info:
            return
        await self._throttler.acquire("/api/v1/markets")
        async with self._session.get(
            self.config.base_url + "/api/v1/markets",
            headers={"X-BROKER-ID": self._broker_id},
            timeout=15,
        ) as resp:
            data = await resp.json()
        # expect array of market dicts
        symbols = data if isinstance(data, list) else data.get("symbols")
        if not isinstance(symbols, list):
            raise RuntimeError("unexpected markets payload")
        for it in symbols:
            try:
                sym = it.get("symbol") or it.get("name")
                if not sym:
                    continue
                self._symbol_to_market[sym] = sym
                self._market_info[sym] = it
                # decimals from nested filters
                price_f = (it.get("filters") or {}).get("price") or {}
                qty_f = (it.get("filters") or {}).get("quantity") or {}
                ts = str(price_f.get("tickSize", "0.01"))
                ss = str(qty_f.get("stepSize", "0.0001"))
                pdec = len(ts.split(".")[1]) if "." in ts else 0
                sdec = len(ss.split(".")[1]) if "." in ss else 0
                self._price_decimals[sym] = pdec
                self._size_decimals[sym] = sdec
            except Exception:
                continue

    async def list_symbols(self) -> List[str]:
        await self._ensure_markets()
        return list(self._symbol_to_market.keys())

    async def get_market_id(self, symbol: str) -> str:
        await self._ensure_markets()
        if symbol not in self._symbol_to_market:
            raise ValueError(f"Unknown symbol {symbol}")
        return self._symbol_to_market[symbol]

    async def get_price_size_decimals(self, symbol: str) -> Tuple[int, int]:
        await self._ensure_markets()
        return self._price_decimals.get(symbol, 2), self._size_decimals.get(symbol, 6)

    # signing -----------------------------------------------------------------
    def _auth_headers(self, instruction: str, params: Dict[str, Any]) -> Dict[str, str]:
        ts = int(time.time() * 1000)
        window = int(self.config.window_ms)
        encoded: List[Tuple[str, Any]] = []
        for k, v in (params or {}).items():
            if isinstance(v, bool):
                encoded.append((k, str(v).lower()))
            else:
                encoded.append((k, v))
        items = sorted(encoded)
        if items:
            kv = "&".join([f"{k}={v}" for k, v in items])
            msg = f"instruction={instruction}&{kv}&timestamp={ts}&window={window}"
        else:
            msg = f"instruction={instruction}&timestamp={ts}&window={window}"
        sig = self._signing_key.sign(msg.encode("utf-8"), encoder=RawEncoder).signature
        return {
            "X-API-KEY": str(self._api_pub or ""),
            "X-TIMESTAMP": str(ts),
            "X-WINDOW": str(window),
            "X-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
            "X-BROKER-ID": self._broker_id,
            "Content-Type": "application/json",
        }

    # REST basics --------------------------------------------------------------
    async def best_effort_latency_ms(self) -> float:
        t0 = time.perf_counter()
        await self._throttler.acquire("/api/v1/status")
        async with self._session.get(
            self.config.base_url + "/api/v1/status",
            headers={"X-BROKER-ID": self._broker_id},
            timeout=10,
        ) as resp:
            _ = await resp.text()
        return (time.perf_counter() - t0) * 1000.0

    async def get_account_overview(self) -> Dict[str, Any]:
        await self._throttler.acquire("/api/v1/account")
        params: Dict[str, Any] = {}
        headers = self._auth_headers("accountQuery", params)
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
        # fallback to REST endpoint if WS data unavailable
        try:
            await self._throttler.acquire("/api/v1/positions")
            headers = self._auth_headers("positionQuery", {})
            async with self._session.get(self.config.base_url + "/api/v1/positions", headers=headers, timeout=15) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
            arr = data.get("positions", []) if isinstance(data, dict) else []
            return arr
        except Exception:
            return []

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        await self._ensure_markets()
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        await self._throttler.acquire("/api/v1/orders")
        headers = self._auth_headers("orderQueryAll", params)
        async with self._session.get(self.config.base_url + "/api/v1/orders", params=params, headers=headers, timeout=15) as resp:
            data = await resp.json()
        if isinstance(data, dict) and "orders" in data and isinstance(data["orders"], list):
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
        p_dec, _s_dec = await self.get_price_size_decimals(symbol)
        scale = 10 ** int(p_dec)
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

    # orders ------------------------------------------------------------------
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
        p_dec, s_dec = await self.get_price_size_decimals(symbol)
        qty = float(base_amount) / (10 ** s_dec)
        price_f = float(price) / (10 ** p_dec)
        qty_s = f"{qty:.{s_dec}f}"
        price_s = f"{price_f:.{p_dec}f}"
        body = {
            "symbol": symbol,
            "side": "Ask" if is_ask else "Bid",
            "orderType": "Limit",
            "quantity": qty_s,
            "price": price_s,
            "timeInForce": "GTC",
            "clientId": str(int(client_order_index)),
        }
        if reduce_only:
            body["reduceOnly"] = True
        if post_only:
            body["postOnly"] = True
        # clientId must be integer
        body["clientId"] = int(body.get("clientId"))
        headers = self._auth_headers("orderExecute", body)
        self.create_tracking_limit_order(client_order_index, symbol=symbol, is_ask=is_ask, price_i=price, size_i=base_amount)
        self._update_order_state(
            client_order_id=client_order_index,
            symbol=symbol,
            is_ask=is_ask,
            state=OrderState.SUBMITTING,
            info={"request": body},
        )
        await self._throttler.acquire("/api/v1/order")
        async with self._session.post(
            self.config.base_url + "/api/v1/order",
            headers=headers,
            json=body,
            timeout=20,
        ) as resp:
            try:
                ret = await resp.json()
            except Exception:
                txt = await resp.text()
                err_msg = f"http_{resp.status}:{txt[:120]}"
                self._update_order_state(
                    client_order_id=client_order_index,
                    symbol=symbol,
                    is_ask=is_ask,
                    state=OrderState.FAILED,
                    info={"error": err_msg},
                )
                return None, None, err_msg
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
        _p_dec, s_dec = await self.get_price_size_decimals(symbol)
        qty = float(base_amount) / (10 ** s_dec)
        qty_s = f"{qty:.{s_dec}f}"
        body = {
            "symbol": symbol,
            "side": "Ask" if is_ask else "Bid",
            "orderType": "Market",
            "quantity": qty_s,
            "clientId": str(int(client_order_index)),
        }
        if reduce_only:
            body["reduceOnly"] = True
        # clientId must be integer
        body["clientId"] = int(body.get("clientId"))
        headers = self._auth_headers("orderExecute", body)
        self.create_tracking_market_order(client_order_index, symbol=symbol, is_ask=is_ask)
        self._update_order_state(
            client_order_id=client_order_index,
            symbol=symbol,
            is_ask=is_ask,
            state=OrderState.SUBMITTING,
            info={"request": body},
        )
        await self._throttler.acquire("/api/v1/order")
        async with self._session.post(
            self.config.base_url + "/api/v1/order",
            headers=headers,
            json=body,
            timeout=20,
        ) as resp:
            try:
                ret = await resp.json()
            except Exception:
                txt = await resp.text()
                err_msg = f"http_{resp.status}:{txt[:120]}"
                self._update_order_state(
                    client_order_id=client_order_index,
                    symbol=symbol,
                    is_ask=is_ask,
                    state=OrderState.FAILED,
                    info={"error": err_msg},
                )
                return None, None, err_msg
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
            state=OrderState.FILLED,  # market assumed immediate
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
        tracker = self.create_tracking_market_order(client_order_index, symbol=symbol, is_ask=is_ask)
        await self.place_market(
            symbol=symbol,
            client_order_index=client_order_index,
            base_amount=base_amount,
            is_ask=is_ask,
            reduce_only=reduce_only,
        )
        return tracker

    async def cancel_order(self, order_index: str, symbol: Optional[str] = None) -> Tuple[Any, Any, Optional[str]]:
        payload: Dict[str, Any] = {}
        if symbol:
            payload["symbol"] = symbol
        try:
            payload["orderId"] = int(str(order_index))
        except Exception:
            payload["orderId"] = str(order_index)
        headers = self._auth_headers("orderCancel", payload)
        await self._throttler.acquire("/api/v1/order")
        async with self._session.request(
            "DELETE",
            self.config.base_url + "/api/v1/order",
            headers=headers,
            json=payload,
            timeout=15,
        ) as resp:
            try:
                ret = await resp.json()
            except Exception:
                txt = await resp.text()
                err_msg = f"http_{resp.status}:{txt[:120]}"
                self._update_order_state(
                    exchange_order_id=str(order_index),
                    symbol=symbol,
                    state=OrderState.FAILED,
                    info={"error": err_msg},
                )
                return None, None, err_msg
        if isinstance(ret, dict):
            status = str(ret.get("status", "")).lower()
            if status in {"filled", "cancelled", "canceled", "partiallyfilled", "expired"}:
                self._update_order_state(
                    exchange_order_id=str(order_index),
                    symbol=symbol,
                    status=status,
                    info=ret,
                )
                return ret, ret, None
        self._update_order_state(
            exchange_order_id=str(order_index),
            symbol=symbol,
            state=OrderState.FAILED,
            info=ret if isinstance(ret, dict) else {},
        )
        return ret, ret, "cancel_failed"

    async def get_order(
        self,
        symbol: str,
        order_id: Optional[str] = None,
        client_id: Optional[int] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        params: Dict[str, Any] = {"symbol": symbol}
        if order_id:
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
            if resp.status == 404:
                return None, "not_found"
            if resp.status == 410:
                return None, "gone"
            try:
                data = await resp.json()
            except Exception:
                txt = await resp.text()
                return None, f"http_{resp.status}:{txt[:120]}"
        if isinstance(data, dict) and data.get("id"):
            return data, None
        return data if isinstance(data, dict) else None, None

    async def cancel_all(self, symbol: Optional[str] = None) -> Tuple[Any, Any, Optional[str]]:
        payload: Dict[str, Any] = {}
        if symbol:
            payload["symbol"] = symbol
        headers = self._auth_headers("orderCancelAll", payload)
        await self._throttler.acquire("/api/v1/orders")
        async with self._session.request(
            "DELETE",
            self.config.base_url + "/api/v1/orders",
            headers=headers,
            json=payload,
            timeout=20,
        ) as resp:
            try:
                data = await resp.json()
            except Exception:
                txt = await resp.text()
                data = {"status": resp.status, "message": txt[:120]}
        status = str(data.get("status", "")).lower() if isinstance(data, dict) else ""
        if resp.status == 200 and status in {"success", "ok", "cancelled", "canceled"}:
            return data, data, None
        # fallback
        orders = await self.get_open_orders(symbol)
        last_err = None
        for od in orders:
            try:
                oid = str(od.get("id") or od.get("orderId"))
                sym = od.get("symbol") or symbol
                _, _, err = await self.cancel_order(oid, sym)
                if err:
                    last_err = err
            except Exception as exc:
                last_err = str(exc)
        return data, data, last_err

    async def cancel_by_client_id(self, symbol: str, client_id: int) -> Tuple[Any, Any, Optional[str]]:
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

    # ws state ---------------------------------------------------------------
    async def start_ws_state(self, symbols: Optional[List[str]] = None):
        if self._ws_task and not self._ws_task.done():
            return
        targets = [str(s).upper() for s in (symbols or []) if s]
        for sym in targets:
            self._top_of_book_events[sym] = asyncio.Event()
            self._top_of_book_cache.pop(sym, None)
            self._depth_state.pop(sym, None)
        if targets:
            await asyncio.gather(*(self._prime_depth_cache(sym) for sym in targets))
        self._ws_stop = False
        self._ws_task = asyncio.create_task(self._ws_loop(targets), name="backpack_ws")

    async def stop_ws_state(self):
        self._ws_stop = True
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        self._ws_task = None

    async def _ws_loop(self, symbols: List[str]):
        # simple WS subscribe: public depth and private order/position topics
        while not self._ws_stop:
            try:
                async with websockets.connect(self.config.ws_url, ping_interval=20) as ws:
                    # subscribe public depth if provided
                    subs = []
                    for s in symbols:
                        subs.append({"method": "SUBSCRIBE", "params": [f"depth.1000ms.{s}"], "id": int(time.time()) % 1_000_000})
                    # private topics with ED25519 signature
                    subs.extend(self._private_ws_subscriptions(symbols))
                    for p in subs:
                        await ws.send(json.dumps(p))
                    # consume
                    while not self._ws_stop:
                        raw = await ws.recv()
                        if isinstance(raw, (bytes, bytearray)):
                            continue
                        try:
                            data = json.loads(raw)
                        except Exception:
                            continue
                        # detect order updates
                        stream = str(data.get("stream") or "").lower()
                        payload = data.get("data") if isinstance(data, dict) else None
                        if not isinstance(payload, dict):
                            payload = data if isinstance(data, dict) else None
                        handled = False
                        if stream.startswith("account.orderupdate") or (isinstance(payload, dict) and payload.get("e", "").lower().startswith("order")):
                            handled = True
                            if isinstance(payload, dict):
                                self._handle_private_order_event(payload)
                        if stream.startswith("account.positionupdate") or (isinstance(payload, dict) and payload.get("e", "").lower().startswith("position")):
                            handled = True
                            if isinstance(payload, dict):
                                self._handle_private_position_event(payload)
                        if handled:
                            continue
                        if stream.startswith("depth.") or str(payload.get("e", "")).lower() == "depth":
                            await self._handle_depth_event(payload)
                            continue
                        if stream.endswith("orderupdate") or (isinstance(payload, dict) and (payload.get("id") or payload.get("clientId"))):
                            if isinstance(payload, dict):
                                self._handle_private_order_event(payload)
                        if stream.endswith("positionupdate") and isinstance(payload, dict):
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
                sym_u = str(sym).upper()
                streams.add(f"account.orderUpdate.{sym_u}")
                streams.add(f"account.positionUpdate.{sym_u}")
        else:
            streams.add("account.orderUpdate")
        payloads: List[Dict[str, Any]] = []
        window = int(self.config.window_ms)
        verifying = self._api_pub or self._verifying_key_b64 or ""
        for stream in streams:
            ts = int(time.time() * 1000)
            msg = f"instruction=subscribe&timestamp={ts}&window={window}"
            try:
                sig = self._signing_key.sign(msg.encode("utf-8"), encoder=RawEncoder).signature
            except Exception:
                continue
            sig_b64 = base64.b64encode(sig).decode("utf-8")
            payloads.append(
                {
                    "method": "SUBSCRIBE",
                    "params": [stream],
                    "signature": [verifying, sig_b64, str(ts), str(window)],
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

    # ------------------------------------------------------------------
    # Order book helpers
    # ------------------------------------------------------------------
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
        try:
            state["last_update_id"] = int(last_update_id)
        except (TypeError, ValueError):
            state["last_update_id"] = 0
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
