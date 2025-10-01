"""Standalone Backpack connector implementation.

Completely independent implementation with no mm_bot dependencies.
Implements REST API, Ed25519 authentication, and order management.
Target: < 600 lines.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from nacl.encoding import RawEncoder
from nacl.signing import SigningKey

from ..execution.order_model import Order, OrderState, Position
from ..app.config import ConnectorConfig
from .interface import ConnectorError, InvalidSymbolError


class BackpackConnector:
    """Standalone Backpack exchange connector.

    Features:
    - REST API with Ed25519 authentication
    - Market data and order book access
    - Limit and market order submission
    - Order cancellation
    - Position and balance queries
    """

    # Symbol mapping: canonical -> exchange format
    SYMBOL_MAP = {
        'BTC': 'BTC_USDC_PERP',
        'ETH': 'ETH_USDC_PERP',
        'SOL': 'SOL_USDC_PERP',
        'DOGE': 'DOGE_USDC_PERP',
        'WIF': 'WIF_USDC_PERP',
        'BONK': 'BONK_USDC_PERP',
        'JTO': 'JTO_USDC_PERP',
        'JUP': 'JUP_USDC_PERP',
        'RNDR': 'RNDR_USDC_PERP',
    }

    def __init__(self, config: ConnectorConfig):
        self.config = config
        self.log = logging.getLogger(f"xbot.connector.backpack.{config.venue_name}")

        # API configuration
        self.base_url = config.base_url or "https://api.backpack.exchange"
        self.ws_url = config.ws_url or "wss://ws.backpack.exchange"
        self.broker_id = str(config.broker_id or "1500")
        self.window_ms = 5000  # Signature window

        # Authentication
        self._api_key: Optional[str] = None
        self._api_secret: Optional[str] = None
        self._signing_key: Optional[SigningKey] = None

        # Session management
        self._session: Optional[aiohttp.ClientSession] = None
        self._started = False
        self._closed = False

        # Market data cache
        self._markets: Dict[str, Dict[str, Any]] = {}
        self._price_decimals: Dict[str, int] = {}
        self._size_decimals: Dict[str, int] = {}
        self._min_sizes: Dict[str, float] = {}

        # Order tracking
        self._orders: Dict[int, Order] = {}  # coi -> Order
        self._exchange_id_to_coi: Dict[str, int] = {}  # exchange_id -> coi

        # Position tracking
        self._positions: Dict[str, Position] = {}  # symbol -> Position

        # WebSocket
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._ws_seq = 0  # WebSocket message sequence
        self._ws_ready = asyncio.Event()  # Signal when WS is ready

    def _load_keys(self) -> None:
        """Load API keys from file."""
        keys_file = self.config.keys_file or "Backpack_key.txt"

        try:
            with open(keys_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if line.lower().startswith("api key:"):
                        self._api_key = line.split(":", 1)[1].strip()
                    elif line.lower().startswith("api secret:"):
                        self._api_secret = line.split(":", 1)[1].strip()
        except Exception as e:
            raise ConnectorError(f"Failed to load keys from {keys_file}: {e}")

        if not self._api_key or not self._api_secret:
            raise ConnectorError(f"Missing API key or secret in {keys_file}")

        # Initialize signing key
        try:
            signing_bytes = base64.b64decode(self._api_secret)
            self._signing_key = SigningKey(signing_bytes)
        except Exception as e:
            raise ConnectorError(f"Invalid API secret (not valid base64 Ed25519 key): {e}")

        self.log.info("Loaded API keys successfully")

    def _sign_message(self, message: str) -> str:
        """Sign message with Ed25519 key."""
        if not self._signing_key:
            raise ConnectorError("Signing key not initialized")

        signature = self._signing_key.sign(message.encode('utf-8'), encoder=RawEncoder).signature
        return base64.b64encode(signature).decode('utf-8')

    def _auth_headers(self, instruction: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        """Generate authenticated headers for API request."""
        timestamp = int(time.time() * 1000)
        window = self.window_ms

        # Build signature message
        if params:
            # Sort and encode parameters
            encoded_params = []
            for key, value in sorted(params.items()):
                if isinstance(value, bool):
                    encoded_params.append(f"{key}={str(value).lower()}")
                else:
                    encoded_params.append(f"{key}={value}")
            param_str = "&".join(encoded_params)
            message = f"instruction={instruction}&{param_str}&timestamp={timestamp}&window={window}"
        else:
            message = f"instruction={instruction}&timestamp={timestamp}&window={window}"

        # DEBUG logging for signature (mask sensitive data)
        if self.log.isEnabledFor(logging.DEBUG):
            safe_params = {k: '***' if 'id' in k.lower() else v for k, v in (params or {}).items()}
            self.log.debug(f"Sign request: instruction={instruction}, params={safe_params}, ts={timestamp}")

        signature = self._sign_message(message)

        return {
            "X-API-KEY": self._api_key or "",
            "X-TIMESTAMP": str(timestamp),
            "X-WINDOW": str(window),
            "X-SIGNATURE": signature,
            "X-BROKER-ID": self.broker_id,
            "Content-Type": "application/json",
        }

    def map_symbol(self, canonical: str) -> str:
        """Map canonical symbol to exchange format."""
        return self.SYMBOL_MAP.get(canonical.upper(), canonical)

    async def start(self) -> None:
        """Start the connector."""
        if self._started:
            return

        # Load API keys
        self._load_keys()

        # Create HTTP session
        self._session = aiohttp.ClientSession()

        # Load market info
        await self._load_markets()

        # Start WebSocket订单流
        self._ws_task = asyncio.create_task(self._ws_loop())

        # Wait for WS to be ready before allowing orders
        try:
            await asyncio.wait_for(self._ws_ready.wait(), timeout=10.0)
            self.log.info("[backpack] WS ready before order submission")
        except asyncio.TimeoutError:
            self.log.warning("[backpack] WS connection timeout, proceeding without WS")

        self._started = True
        self.log.info(f"Backpack connector started: {self.config.venue_name}")

    async def stop(self) -> None:
        """Stop the connector."""
        if not self._started:
            return

        # Stop WebSocket
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None

        if self._ws:
            await self._ws.close()
            self._ws = None

        if self._session:
            await self._session.close()
            self._session = None

        self._started = False
        self.log.info("Backpack connector stopped")

    async def close(self) -> None:
        """Close the connector."""
        await self.stop()
        self._closed = True
        self.log.info("Backpack connector closed")

    async def _load_markets(self) -> None:
        """Load market information from exchange."""
        if not self._session:
            raise ConnectorError("Session not initialized")

        try:
            async with self._session.get(
                f"{self.base_url}/api/v1/markets",
                headers={"X-BROKER-ID": self.broker_id},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise ConnectorError(f"Failed to load markets: {resp.status} {text}")

                data = await resp.json()

            # Parse market data
            symbols = data if isinstance(data, list) else data.get("symbols", [])

            for item in symbols:
                try:
                    symbol = item.get("symbol") or item.get("name")
                    if not symbol:
                        continue

                    self._markets[symbol] = item

                    # Extract filters
                    filters = item.get("filters", {})
                    price_filter = filters.get("price", {})
                    qty_filter = filters.get("quantity", {})

                    # Parse tick size and step size
                    tick_size = str(price_filter.get("tickSize", "0.01"))
                    step_size = str(qty_filter.get("stepSize", "0.0001"))

                    # Calculate decimal places
                    price_dec = len(tick_size.split(".")[1]) if "." in tick_size else 0
                    size_dec = len(step_size.split(".")[1]) if "." in step_size else 0

                    self._price_decimals[symbol] = price_dec
                    self._size_decimals[symbol] = size_dec

                    # Store minimum quantity
                    min_qty = float(qty_filter.get("min", qty_filter.get("minQty", 0.0)))
                    self._min_sizes[symbol] = min_qty

                except Exception as e:
                    self.log.warning(f"Failed to parse market {symbol}: {e}")
                    continue

            self.log.info(f"Loaded {len(self._markets)} markets")

        except Exception as e:
            raise ConnectorError(f"Failed to load markets: {e}")

    async def get_price_size_decimals(self, symbol: str) -> Tuple[int, int]:
        """Get price and size decimal places."""
        mapped = self.map_symbol(symbol)

        if mapped not in self._markets:
            raise InvalidSymbolError(f"Unknown symbol: {symbol}")

        price_dec = self._price_decimals.get(mapped, 2)
        size_dec = self._size_decimals.get(mapped, 6)

        return price_dec, size_dec

    async def get_min_size_i(self, symbol: str) -> int:
        """Get minimum order size in integer format."""
        mapped = self.map_symbol(symbol)

        if mapped not in self._markets:
            raise InvalidSymbolError(f"Unknown symbol: {symbol}")

        min_size = self._min_sizes.get(mapped, 0.0)
        size_decimals = self._size_decimals.get(mapped, 6)
        size_scale = 10 ** size_decimals

        min_size_i = int(min_size * size_scale)
        return max(min_size_i, 1)

    async def get_top_of_book(self, symbol: str) -> Tuple[Optional[int], Optional[int], int]:
        """Get top of book (bid_i, ask_i, scale)."""
        if not self._session:
            raise ConnectorError("Session not initialized")

        mapped = self.map_symbol(symbol)
        price_decimals = self._price_decimals.get(mapped, 2)
        scale = 10 ** price_decimals

        try:
            params = {"symbol": mapped, "limit": 1}
            if self.log.isEnabledFor(logging.DEBUG):
                self.log.debug(f"Fetching depth: {params}")

            async with self._session.get(
                f"{self.base_url}/api/v1/depth",
                params=params,
                headers={"X-BROKER-ID": self.broker_id},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    # Fallback to ticker
                    return await self._get_top_from_ticker(mapped, scale)

                data = await resp.json()

            if self.log.isEnabledFor(logging.DEBUG):
                self.log.debug(f"Depth API response for {mapped}: {data}")

            bids = data.get("bids", [])
            asks = data.get("asks", [])

            # If no bids or asks, use ticker as fallback
            if not bids or not asks:
                self.log.warning(f"No bids/asks in order book for {symbol}, using ticker")
                return await self._get_top_from_ticker(mapped, scale)

            bid_i = int(float(bids[0][0]) * scale)
            ask_i = int(float(asks[0][0]) * scale)

            # Sanity check: if spread > 5%, likely bad data
            if bid_i > 0 and ask_i > bid_i:
                spread_pct = (ask_i - bid_i) / bid_i
                if spread_pct > 0.05:  # 5% spread threshold
                    self.log.warning(
                        f"Abnormal spread for {symbol}: {spread_pct*100:.1f}%, "
                        f"bid={bid_i/scale:.2f} ask={ask_i/scale:.2f}, using ticker"
                    )
                    return await self._get_top_from_ticker(mapped, scale)

            return bid_i, ask_i, scale

        except Exception as e:
            self.log.error(f"Failed to get top of book for {symbol}: {e}")
            # Fallback to ticker
            return await self._get_top_from_ticker(mapped, scale)

    async def _get_top_from_ticker(self, mapped_symbol: str, scale: int) -> Tuple[Optional[int], Optional[int], int]:
        """Get approximate top of book from ticker (fallback)."""
        try:
            async with self._session.get(
                f"{self.base_url}/api/v1/ticker",
                params={"symbol": mapped_symbol},
                headers={"X-BROKER-ID": self.broker_id},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return None, None, scale

                data = await resp.json()

            last_price = float(data.get("lastPrice", 0))
            if last_price <= 0:
                return None, None, scale

            # Estimate bid/ask with 0.01% spread
            mid_i = int(last_price * scale)
            spread = max(1, mid_i // 10000)  # 0.01%

            bid_i = mid_i - spread
            ask_i = mid_i + spread

            return bid_i, ask_i, scale

        except Exception as e:
            self.log.error(f"Failed to get ticker for {mapped_symbol}: {e}")
            return None, None, scale

    async def get_order_book(self, symbol: str, depth: int = 5) -> Dict[str, Any]:
        """Get order book."""
        if not self._session:
            raise ConnectorError("Session not initialized")

        mapped = self.map_symbol(symbol)

        try:
            async with self._session.get(
                f"{self.base_url}/api/v1/depth",
                params={"symbol": mapped, "limit": min(depth, 200)},
                headers={"X-BROKER-ID": self.broker_id},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return {"bids": [], "asks": []}

                return await resp.json()

        except Exception as e:
            self.log.error(f"Failed to get order book for {symbol}: {e}")
            return {"bids": [], "asks": []}

    async def submit_limit_order(
        self,
        *,
        symbol: str,
        client_order_index: int,
        base_amount: int,
        price: int,
        is_ask: bool,
        post_only: bool = False,
        reduce_only: int = 0
    ) -> Order:
        """Submit limit order."""
        if not self._session:
            raise ConnectorError("Session not initialized")

        # Wait for WS to be ready before submitting order
        if not self._ws_ready.is_set():
            self.log.warning("WS not ready, waiting before order submission...")
            try:
                await asyncio.wait_for(self._ws_ready.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.log.error("WS ready timeout, proceeding with order anyway")

        mapped = self.map_symbol(symbol)

        # Convert integer format to float
        price_decimals = self._price_decimals.get(mapped, 2)
        size_decimals = self._size_decimals.get(mapped, 6)

        price_scale = 10 ** price_decimals
        size_scale = 10 ** size_decimals

        price_float = price / price_scale
        size_float = base_amount / size_scale

        # Get current bid/ask for logging
        try:
            bid_i, ask_i, _ = await self.get_top_of_book(symbol)
            bid_float = bid_i / price_scale if bid_i else None
            ask_float = ask_i / price_scale if ask_i else None
        except Exception:
            bid_float = None
            ask_float = None

        # Prepare order parameters
        side_str = "Ask" if is_ask else "Bid"
        order_params = {
            "symbol": mapped,
            "side": side_str,
            "orderType": "Limit",
            "price": str(price_float),
            "quantity": str(size_float),
            "clientId": client_order_index,
            "postOnly": post_only,
        }

        if reduce_only:
            order_params["timeInForce"] = "IOC"

        # Log order details before submission
        bid_str = f"{bid_float:.4f}" if bid_float is not None else "N/A"
        ask_str = f"{ask_float:.4f}" if ask_float is not None else "N/A"
        self.log.info(
            f"[ORDER SUBMIT] symbol={symbol} coi={client_order_index} "
            f"side={side_str} is_ask={is_ask} order_type=Limit "
            f"chosen_price={price_float:.4f} (price_i={price}) "
            f"bid={bid_str} ask={ask_str} "
            f"qty={size_float:.4f} post_only={post_only} reduce_only={reduce_only}"
        )

        # Create order object BEFORE REST request to catch early WS events
        order = Order(
            coi=client_order_index,
            venue=self.config.venue_name,
            symbol=symbol,
            side="sell" if is_ask else "buy",
            size_i=base_amount,
            price_i=price,
            state=OrderState.SUBMITTING,
            is_limit=True,
            post_only=post_only,
            reduce_only=reduce_only
        )
        self._orders[client_order_index] = order

        try:
            headers = self._auth_headers("orderExecute", order_params)

            async with self._session.post(
                f"{self.base_url}/api/v1/order",
                json=order_params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status not in [200, 201]:
                    text = await resp.text()
                    raise ConnectorError(f"Order submission failed: {resp.status} {text}")

                result = await resp.json()

            # Log the exchange response
            exchange_id = result.get("id") or result.get("orderId")
            result_status = result.get("status", "unknown").lower()
            result_type = result.get("orderType", result.get("type", "unknown"))
            self.log.info(
                f"[ORDER RESPONSE] exchange_id={exchange_id} "
                f"status={result_status} type={result_type} "
                f"full_response={result}"
            )

            # Update existing order with REST response
            order.exchange_order_id = exchange_id
            if exchange_id:
                self._exchange_id_to_coi[exchange_id] = client_order_index

            # Map API status to OrderState
            state_map = {
                "new": OrderState.OPEN,
                "filled": OrderState.FILLED,
                "cancelled": OrderState.CANCELLED,
                "rejected": OrderState.FAILED,
            }
            rest_state = state_map.get(result_status, OrderState.OPEN)

            # Update order state if it hasn't been updated by WS yet
            if order.state == OrderState.SUBMITTING:
                order.update_state(rest_state, "rest_response")

            # Get filled quantity
            executed_qty = result.get("executedQuantity", "0")
            filled_size_i = int(float(executed_qty) * size_scale)

            # Set filled quantity if order is filled
            if rest_state == OrderState.FILLED and filled_size_i > 0:
                order.filled_base_i = filled_size_i
                self.log.info(f"Order immediately filled: coi={client_order_index} filled={filled_size_i}/{base_amount}")
            self.log.info(f"Submitted limit order: {symbol} coi={client_order_index} exchange_id={order.exchange_order_id}")

            return order

        except Exception as e:
            self.log.error(f"Failed to submit limit order: {e}")
            raise ConnectorError(f"Failed to submit limit order: {e}")

    async def submit_market_order(
        self,
        *,
        symbol: str,
        client_order_index: int,
        size_i: int,
        is_ask: bool,
        reduce_only: int = 0
    ) -> Order:
        """Submit market order."""
        if not self._session:
            raise ConnectorError("Session not initialized")

        # Wait for WS to be ready before submitting order
        if not self._ws_ready.is_set():
            self.log.warning("WS not ready, waiting before order submission...")
            try:
                await asyncio.wait_for(self._ws_ready.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.log.error("WS ready timeout, proceeding with order anyway")

        mapped = self.map_symbol(symbol)

        # Convert integer format to float
        size_decimals = self._size_decimals.get(mapped, 6)
        size_scale = 10 ** size_decimals
        size_float = size_i / size_scale

        # Prepare order parameters
        order_params = {
            "symbol": mapped,
            "side": "Ask" if is_ask else "Bid",
            "orderType": "Market",
            "quantity": str(size_float),
            "clientId": client_order_index,
        }

        if reduce_only:
            order_params["timeInForce"] = "IOC"

        try:
            headers = self._auth_headers("orderExecute", order_params)

            async with self._session.post(
                f"{self.base_url}/api/v1/order",
                json=order_params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status not in [200, 201]:
                    text = await resp.text()
                    raise ConnectorError(f"Market order submission failed: {resp.status} {text}")

                result = await resp.json()

            # Market orders typically fill immediately
            order = Order(
                coi=client_order_index,
                venue=self.config.venue_name,
                symbol=symbol,
                side="sell" if is_ask else "buy",
                size_i=size_i,
                price_i=None,
                state=OrderState.FILLED,
                is_limit=False,
                reduce_only=reduce_only,
                exchange_order_id=result.get("id") or result.get("orderId")
            )

            order.filled_base_i = size_i
            self._orders[client_order_index] = order
            if order.exchange_order_id:
                self._exchange_id_to_coi[order.exchange_order_id] = client_order_index

            self.log.info(f"Submitted market order: {symbol} coi={client_order_index}")
            return order

        except Exception as e:
            self.log.error(f"Failed to submit market order: {e}")
            raise ConnectorError(f"Failed to submit market order: {e}")

    async def cancel_by_client_id(self, symbol: str, client_order_index: int) -> None:
        """Cancel order by client ID."""
        if not self._session:
            raise ConnectorError("Session not initialized")

        mapped = self.map_symbol(symbol)

        try:
            params = {
                "symbol": mapped,
                "clientId": client_order_index,
            }

            headers = self._auth_headers("orderCancel", params)

            async with self._session.delete(
                f"{self.base_url}/api/v1/order",
                json=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status not in [200, 204]:
                    text = await resp.text()
                    self.log.warning(f"Cancel failed: {resp.status} {text}")
                    return

            # Update order state if tracking
            order = self._orders.get(client_order_index)
            if order:
                order.update_state(OrderState.CANCELLED, "manual_cancel")

            self.log.info(f"Cancelled order: {symbol} coi={client_order_index}")

        except Exception as e:
            self.log.error(f"Failed to cancel order: {e}")
            raise ConnectorError(f"Failed to cancel order: {e}")

    async def cancel_all(self, timeout: Optional[float] = None) -> Any:
        """Cancel all open orders."""
        # Not implemented for safety
        raise NotImplementedError("cancel_all not implemented for safety")

    async def get_order(self, symbol: str, client_order_index: int) -> Dict[str, Any]:
        """Get order status."""
        # Return cached order if available
        order = self._orders.get(client_order_index)
        if order:
            return {
                "clientId": order.coi,
                "symbol": symbol,
                "side": order.side,
                "state": order.state.value,
            }
        return {}

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get open orders."""
        if not self._session:
            raise ConnectorError("Session not initialized")

        try:
            params = {}
            if symbol:
                params["symbol"] = self.map_symbol(symbol)

            headers = self._auth_headers("orderQueryAll", params)

            async with self._session.get(
                f"{self.base_url}/api/v1/orders",
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    return []

                data = await resp.json()

            if isinstance(data, dict) and isinstance(data.get("orders"), list):
                return data["orders"]
            if isinstance(data, list):
                return data
            return []

        except Exception as e:
            self.log.error(f"Failed to get open orders: {e}")
            return []

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Get current positions."""
        if not self._session:
            raise ConnectorError("Session not initialized")

        try:
            headers = self._auth_headers("positionQuery", {})

            async with self._session.get(
                f"{self.base_url}/api/v1/positions",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    return []

                data = await resp.json()

            return data.get("positions", []) if isinstance(data, dict) else []

        except Exception as e:
            self.log.error(f"Failed to get positions: {e}")
            return []

    async def get_account_overview(self) -> Dict[str, Any]:
        """Get account overview."""
        if not self._session:
            raise ConnectorError("Session not initialized")

        try:
            headers = self._auth_headers("balanceQuery", {})

            async with self._session.get(
                f"{self.base_url}/api/v1/capital",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    return {}

                return await resp.json()

        except Exception as e:
            self.log.error(f"Failed to get account overview: {e}")
            return {}

    async def best_effort_latency_ms(self) -> float:
        """Get latency estimate."""
        if not self._session:
            return 100.0

        try:
            start = time.perf_counter()
            async with self._session.get(
                f"{self.base_url}/api/v1/status",
                headers={"X-BROKER-ID": self.broker_id},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                await resp.text()

            return (time.perf_counter() - start) * 1000.0

        except Exception:
            return 100.0

    async def list_symbols(self) -> List[str]:
        """List available symbols."""
        return list(self._markets.keys())

    # ===== WebSocket Order Events =====

    async def _ws_loop(self) -> None:
        """WebSocket event loop for real-time order updates."""
        while not self._closed:
            try:
                await self._ws_connect()
                await self._ws_listen()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log.error(f"WebSocket error: {e}, reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _ws_connect(self) -> None:
        """Connect to WebSocket and subscribe to order events."""
        if not self._session:
            return

        try:
            self._ws = await self._session.ws_connect(
                f"{self.ws_url}",
                headers={"X-BROKER-ID": self.broker_id}
            )

            # Subscribe to order and position updates with signature authentication
            if self._signing_key:
                # Subscribe to both orderUpdate and positionUpdate
                order_sub = self._create_signed_subscription("account.orderUpdate")
                position_sub = self._create_signed_subscription("account.positionUpdate")

                await self._ws.send_json(order_sub)
                self.log.info(f"Sent orderUpdate subscription")

                await self._ws.send_json(position_sub)
                self.log.info(f"Sent positionUpdate subscription")
            else:
                # Fallback to unsigned (will likely not receive private events)
                await self._ws.send_json({
                    "method": "SUBSCRIBE",
                    "params": ["account.orderUpdate"]
                })
                await self._ws.send_json({
                    "method": "SUBSCRIBE",
                    "params": ["account.positionUpdate"]
                })
                self.log.warning("No signing key available, WS subscription may fail")

            self.log.info("WebSocket connected, sent subscription requests")

            # Wait briefly for subscription response/confirmation
            try:
                msg = await asyncio.wait_for(self._ws.receive(), timeout=2.0)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    response = json.loads(msg.data)
                    self.log.info(f"Subscription response: {response}")
                else:
                    self.log.warning(f"Unexpected subscription response type: {msg.type}")
            except asyncio.TimeoutError:
                self.log.info("No subscription response received (may be normal)")
            except Exception as e:
                self.log.warning(f"Error reading subscription response: {e}")

            # Signal that WS is ready
            self._ws_ready.set()

        except Exception as e:
            self.log.error(f"WebSocket connect failed: {e}")
            raise

    def _create_signed_subscription(self, stream: str) -> Dict[str, Any]:
        """Create signed WebSocket subscription message."""
        ts = int(time.time() * 1000)
        window = self.window_ms

        # Build signature message
        message = f"instruction=subscribe&timestamp={ts}&window={window}"

        # Sign the message
        signature = self._signing_key.sign(message.encode("utf-8"), encoder=RawEncoder).signature
        signature_b64 = base64.b64encode(signature).decode("utf-8")

        # Get verifying key (public key)
        verifying_key = self._signing_key.verify_key
        verifying_key_b64 = base64.b64encode(bytes(verifying_key)).decode("utf-8")

        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug(
                f"WS subscription signature: stream={stream}, ts={ts}, "
                f"verifying_key={verifying_key_b64[:20]}..."
            )

        return {
            "method": "SUBSCRIBE",
            "params": [stream],
            "signature": [verifying_key_b64, signature_b64, str(ts), str(window)]
        }

    async def _ws_listen(self) -> None:
        """Listen for WebSocket messages."""
        if not self._ws:
            return

        self.log.info("WebSocket listening for messages...")
        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                # Print raw text at DEBUG level
                if self.log.isEnabledFor(logging.DEBUG):
                    self.log.debug(f"[WS RAW TEXT] {msg.data}")
                try:
                    data = json.loads(msg.data)
                    await self._handle_ws_message(data)
                except Exception as e:
                    self.log.error(f"Failed to parse WS message: {e}, raw: {msg.data}")

            elif msg.type == aiohttp.WSMsgType.CLOSED:
                self.log.warning(f"WebSocket closed: {msg}")
                break

            elif msg.type == aiohttp.WSMsgType.ERROR:
                self.log.error(f"WebSocket error: {msg}")
                break

            else:
                self.log.debug(f"WebSocket message type {msg.type}: {msg}")

    async def _handle_ws_message(self, data: Dict[str, Any]) -> None:
        """Handle incoming WebSocket message."""
        self._ws_seq += 1

        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug(f"[WS RAW] {data}")

        msg_type = data.get("stream")

        # Route message by type
        if msg_type == "account.orderUpdate":
            await self._handle_order_update(data.get("data", {}))
        elif msg_type == "account.positionUpdate":
            await self._handle_position_update(data.get("data", {}))
        else:
            if self.log.isEnabledFor(logging.DEBUG):
                self.log.debug(f"Ignoring WS message type: {msg_type}")

    async def _handle_order_update(self, payload: Dict[str, Any]) -> None:
        """Handle orderUpdate WebSocket message."""
        if not payload:
            return

        # Normalize field names - Backpack uses shortened fields in WS
        # Map: X->status, c->clientId, i->id, s->symbol, S->side, z->executedQuantity
        # Map: e->event, l->lastFillQuantity, L->lastFillPrice
        normalized = dict(payload)
        if "X" in payload and "status" not in normalized:
            normalized["status"] = payload.get("X")
        if "c" in payload and "clientId" not in normalized:
            normalized["clientId"] = payload.get("c")
        if "i" in payload:
            normalized.setdefault("id", payload.get("i"))
        if "s" in payload:
            normalized.setdefault("symbol", payload.get("s"))
        if "S" in payload:
            normalized.setdefault("side", payload.get("S"))
        if "z" in payload:
            normalized.setdefault("executedQuantity", payload.get("z"))
        if "e" in payload:
            normalized.setdefault("event", payload.get("e"))
        if "l" in payload:
            normalized.setdefault("lastFillQuantity", payload.get("l"))
        if "L" in payload:
            normalized.setdefault("lastFillPrice", payload.get("L"))

        # Extract order info from normalized payload
        exchange_id = normalized.get("id")
        client_id = normalized.get("clientId")
        status = str(normalized.get("status", "")).lower()
        event_type = str(normalized.get("event", "")).lower()

        # Parse quantities
        filled_qty_str = normalized.get("executedQuantity", "0")
        try:
            filled_qty = float(filled_qty_str) if filled_qty_str else 0.0
        except (TypeError, ValueError):
            filled_qty = 0.0

        last_fill_qty_str = normalized.get("lastFillQuantity", "0")
        try:
            last_fill_qty = float(last_fill_qty_str) if last_fill_qty_str else 0.0
        except (TypeError, ValueError):
            last_fill_qty = 0.0

        last_fill_price_str = normalized.get("lastFillPrice", "0")
        try:
            last_fill_price = float(last_fill_price_str) if last_fill_price_str else 0.0
        except (TypeError, ValueError):
            last_fill_price = 0.0

        symbol = normalized.get("symbol", "")

        # Find order by clientId
        order = self._orders.get(client_id) if client_id else None
        if not order and exchange_id:
            # Fallback: find by exchange_id
            coi = self._exchange_id_to_coi.get(exchange_id)
            order = self._orders.get(coi) if coi else None

        if not order:
            if self.log.isEnabledFor(logging.DEBUG):
                self.log.debug(f"WS event for unknown order: client_id={client_id}, exchange_id={exchange_id}")
            return

        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug(f"WS event for order coi={order.coi}: event={event_type}, status={status}, current_state={order.state.value}")

        size_decimals = self._size_decimals.get(symbol, 6)
        size_scale = 10 ** size_decimals

        # Handle orderFill event (partial or full fill)
        if event_type == "orderfill":
            # Update cumulative filled quantity
            if filled_qty > 0:
                order.filled_base_i = int(filled_qty * size_scale)

            # Determine if partially filled or completely filled
            filled_ratio = order.filled_base_i / order.size_i if order.size_i > 0 else 0

            if filled_ratio >= 0.9999:  # Allow tiny rounding errors
                new_state = OrderState.FILLED
                reason = f"ws:fill:complete:seq={self._ws_seq}"
            else:
                new_state = OrderState.PARTIALLY_FILLED
                reason = f"ws:fill:partial:seq={self._ws_seq}"

            order.update_state(new_state, reason)

            self.log.info(
                f"WS order fill: coi={order.coi} filled={order.filled_base_i}/{order.size_i} "
                f"price={last_fill_price:.2f} state={order.state.value}"
            )
            return

        # Handle other status updates
        state_map = {
            "new": OrderState.OPEN,
            "filled": OrderState.FILLED,
            "partiallyfilled": OrderState.PARTIALLY_FILLED,
            "cancelled": OrderState.CANCELLED,
            "rejected": OrderState.FAILED,
            "expired": OrderState.CANCELLED,
        }

        new_state = state_map.get(status, order.state)
        if new_state != order.state:
            # Update state with timeline tracking
            reason = f"ws:{status}:seq={self._ws_seq}"
            order.update_state(new_state, reason)

            # Update filled amount if provided
            if filled_qty > 0:
                order.filled_base_i = int(filled_qty * size_scale)

            self.log.info(
                f"WS order update: coi={order.coi} {order.state.value} "
                f"filled={order.filled_base_i}/{order.size_i} seq={self._ws_seq}"
            )

    async def _handle_position_update(self, payload: Dict[str, Any]) -> None:
        """Handle positionUpdate WebSocket message."""
        if not payload:
            return

        # Extract position info
        # Fields: e (event), s (symbol), q (qty), n (notional), p (realized PnL), P (unrealized PnL)
        event_type = payload.get("e", "")
        symbol = payload.get("s", "")
        if not symbol:
            return

        # Parse numeric fields
        try:
            qty = float(payload.get("q", 0))
            notional = float(payload.get("n", 0))
            realized_pnl = float(payload.get("p", 0))
            unrealized_pnl = float(payload.get("P", 0))
        except (TypeError, ValueError) as e:
            self.log.warning(f"Failed to parse position fields: {e}, payload={payload}")
            return

        # Update position cache
        position = Position(
            symbol=symbol,
            qty=qty,
            notional=notional,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            last_update_ts=time.time()
        )
        self._positions[symbol] = position

        self.log.info(
            f"WS position update: symbol={symbol} qty={qty} notional={notional} "
            f"pnl_realized={realized_pnl} pnl_unrealized={unrealized_pnl}"
        )

    def get_position(self, symbol: str) -> Optional[Position]:
        """Get cached position for a symbol.

        Args:
            symbol: Trading symbol (canonical or exchange format)

        Returns:
            Position object if found, None otherwise
        """
        # Try both canonical and mapped symbol
        mapped = self.map_symbol(symbol)
        return self._positions.get(mapped) or self._positions.get(symbol)


__all__ = ["BackpackConnector"]
