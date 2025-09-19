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


@dataclass
class BackpackConfig:
    base_url: str = "https://api.backpack.exchange"
    ws_url: str = "wss://ws.backpack.exchange"
    keys_file: str = "Backpack_key.txt"
    window_ms: int = 5000
    rpm: int = 300  # conservative default


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


class BackpackConnector:
    def __init__(self, config: Optional[BackpackConfig] = None):
        self.config = config or BackpackConfig()
        self.log = logging.getLogger("mm_bot.connector.backpack")

        self._throttler = RateLimiter(capacity_per_minute=self.config.rpm, weights=lighter_default_weights())
        self._session: Optional[aiohttp.ClientSession] = None
        self._started = False

        # keys
        self._api_pub: Optional[str] = None
        self._api_priv: Optional[str] = None
        self._signing_key: Optional[SigningKey] = None

        # symbol map and scales
        self._symbol_to_market: Dict[str, str] = {}  # symbol string passthrough
        self._market_info: Dict[str, Dict[str, Any]] = {}
        self._price_decimals: Dict[str, int] = {}
        self._size_decimals: Dict[str, int] = {}

        # ws state
        self._ws_task: Optional[asyncio.Task] = None
        self._ws_stop: bool = False

        # event hooks
        self._on_order_filled: Optional[Callable[[Dict[str, Any]], None]] = None
        self._on_order_cancelled: Optional[Callable[[Dict[str, Any]], None]] = None
        self._on_trade: Optional[Callable[[Dict[str, Any]], None]] = None
        self._on_position_update: Optional[Callable[[Dict[str, Any]], None]] = None

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
        except Exception as e:
            raise RuntimeError(f"Invalid backpack private key: {e}")
        self._session = aiohttp.ClientSession()
        self._started = True

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    # event handlers ----------------------------------------------------------
    def set_event_handlers(
        self,
        *,
        on_order_filled: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_order_cancelled: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_trade: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_position_update: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._on_order_filled = on_order_filled
        self._on_order_cancelled = on_order_cancelled
        self._on_trade = on_trade
        self._on_position_update = on_position_update

    # helpers -----------------------------------------------------------------
    async def _ensure_markets(self):
        if self._market_info:
            return
        await self._throttler.acquire("/api/v1/markets")
        async with self._session.get(self.config.base_url + "/api/v1/markets", timeout=15) as resp:
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
        items = sorted((k, v) for k, v in (params or {}).items())
        if items:
            kv = "&".join([f"{k}={v}" for k, v in items])
            msg = f"instruction={instruction}&{kv}&timestamp={ts}&window={window}"
        else:
            msg = f"instruction={instruction}&timestamp={ts}&window={window}"
        sig = self._signing_key.sign(msg.encode("utf-8"), encoder=RawEncoder).signature
        return {
            "X-API-Key": str(self._api_pub or ""),
            "X-Timestamp": str(ts),
            "X-Window": str(window),
            "X-Signature": base64.b64encode(sig).decode("utf-8"),
            "Content-Type": "application/json",
        }

    # REST basics --------------------------------------------------------------
    async def best_effort_latency_ms(self) -> float:
        t0 = time.perf_counter()
        await self._throttler.acquire("/api/v1/status")
        async with self._session.get(self.config.base_url + "/api/v1/status", timeout=10) as resp:
            _ = await resp.text()
        return (time.perf_counter() - t0) * 1000.0

    async def get_account_overview(self) -> Dict[str, Any]:
        await self._throttler.acquire("/api/v1/account")
        params: Dict[str, Any] = {}
        headers = self._auth_headers("accountQuery", params)
        async with self._session.get(self.config.base_url + "/api/v1/account", headers=headers, timeout=15) as resp:
            return await resp.json()

    async def get_positions(self) -> List[Dict[str, Any]]:
        # positions endpoint may vary; best-effort return empty list if unsupported
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

    async def get_top_of_book(self, symbol: str) -> Tuple[Optional[int], Optional[int], int]:
        await self._ensure_markets()
        p_dec, _s_dec = await self.get_price_size_decimals(symbol)
        params = {"symbol": symbol, "limit": 1}
        await self._throttler.acquire("/api/v1/depth")
        async with self._session.get(self.config.base_url + "/api/v1/depth", params=params, timeout=10) as resp:
            data = await resp.json()
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        scale = 10 ** int(p_dec)
        def _to_i(x: Any) -> Optional[int]:
            try:
                return int(round(float(x) * scale))
            except Exception:
                return None
        bid_i = _to_i(bids[0][0]) if bids else None
        ask_i = _to_i(asks[0][0]) if asks else None
        return bid_i, ask_i, scale

    # orders ------------------------------------------------------------------
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
        await self._throttler.acquire("/api/v1/order")
        async with self._session.post(self.config.base_url + "/api/v1/order", headers=headers, json=body, timeout=20) as resp:
            try:
                ret = await resp.json()
            except Exception:
                txt = await resp.text()
                return None, None, f"http_{resp.status}:{txt[:120]}"
        err = None if (isinstance(ret, dict) and ret.get("id")) else (ret.get("message") if isinstance(ret, dict) else "unknown")
        return ret, ret, err

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
        await self._throttler.acquire("/api/v1/order")
        async with self._session.post(self.config.base_url + "/api/v1/order", headers=headers, json=body, timeout=20) as resp:
            try:
                ret = await resp.json()
            except Exception:
                txt = await resp.text()
                return None, None, f"http_{resp.status}:{txt[:120]}"
        err = None if (isinstance(ret, dict) and ret.get("id")) else (ret.get("message") if isinstance(ret, dict) else "unknown")
        return ret, ret, err

    async def cancel_order(self, order_index: str, symbol: Optional[str] = None) -> Tuple[Any, Any, Optional[str]]:
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        params["orderId"] = order_index
        headers = self._auth_headers("orderCancel", params)
        await self._throttler.acquire("/api/v1/order")
        async with self._session.delete(self.config.base_url + "/api/v1/order", params=params, headers=headers, timeout=15) as resp:
            try:
                ret = await resp.json()
            except Exception:
                txt = await resp.text()
                return None, None, f"http_{resp.status}:{txt[:120]}"
        # success if echo back orderId or status
        ok = isinstance(ret, dict) and (ret.get("orderId") or ret.get("status") == "Cancelled")
        return ret, ret, None if ok else "cancel_failed"

    async def cancel_all(self, symbol: Optional[str] = None) -> Tuple[Any, Any, Optional[str]]:
        # Backpack may support bulk cancel via /wapi/v1/order with action, otherwise loop existing
        orders = await self.get_open_orders(symbol)
        last_err = None
        for od in orders:
            try:
                oid = str(od.get("id") or od.get("orderId"))
                sym = od.get("symbol") or symbol
                _, _, err = await self.cancel_order(oid, sym)
                if err:
                    last_err = err
            except Exception as e:
                last_err = str(e)
        return None, None, last_err

    # ws state ---------------------------------------------------------------
    async def start_ws_state(self, symbols: Optional[List[str]] = None):
        if self._ws_task and not self._ws_task.done():
            return
        self._ws_stop = False
        self._ws_task = asyncio.create_task(self._ws_loop(symbols or []), name="backpack_ws")

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
                    # private topics (if available without extra auth)
                    subs.append({"method": "SUBSCRIBE", "params": ["orderUpdate"], "id": 1})
                    subs.append({"method": "SUBSCRIBE", "params": ["positionUpdate"], "id": 2})
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
                        if stream.endswith("orderupdate") or (isinstance(payload, dict) and (payload.get("id") or payload.get("clientId"))):
                            if self._on_order_filled and isinstance(payload, dict) and str(payload.get("status")).lower() in ("filled", "partiallyfilled"):
                                try:
                                    self._on_order_filled(payload)
                                except Exception:
                                    pass
                            if self._on_order_cancelled and isinstance(payload, dict) and str(payload.get("status")).lower() in ("cancelled", "canceled"):
                                try:
                                    self._on_order_cancelled(payload)
                                except Exception:
                                    pass
                        if stream.endswith("positionupdate") and self._on_position_update and isinstance(payload, dict):
                            try:
                                self._on_position_update(payload)
                            except Exception:
                                pass
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(1.0)
