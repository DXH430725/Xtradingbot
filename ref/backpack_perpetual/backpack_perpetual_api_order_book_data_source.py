import asyncio
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_constants as CONSTANTS
from hummingbot.connector.derivative.backpack_perpetual import backpack_perpetual_web_utils as web_utils
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.utils.async_utils import safe_gather
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_derivative import BackpackPerpetualDerivative


class BackpackPerpetualAPIOrderBookDataSource(OrderBookTrackerDataSource):
    """
    Order book data source for Backpack Perpetual
    """

    HEARTBEAT_TIME_INTERVAL = 30.0
    TRADE_STREAM_ID = 1
    DIFF_STREAM_ID = 2

    _logger: Optional[HummingbotLogger] = None

    def __init__(self,
                 trading_pairs: List[str],
                 connector: "BackpackPerpetualDerivative",
                 api_factory: WebAssistantsFactory,
                 domain: str = CONSTANTS.DEFAULT_DOMAIN):
        """
        Initialize the order book data source
        
        :param trading_pairs: List of trading pairs to track
        :param connector: The connector instance
        :param api_factory: Web assistants factory
        :param domain: Domain identifier
        """
        super().__init__(trading_pairs)
        self._connector = connector
        self._api_factory = api_factory
        self._domain = domain
        self._throttler = api_factory._throttler

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = HummingbotLogger.logger_name_for_class(cls)
        return cls._logger

    @classmethod
    def exchange_symbol_associated_to_pair(cls, trading_pair: str) -> str:
        """
        Convert trading pair to exchange symbol
        """
        return web_utils.format_trading_pair(trading_pair)

    @classmethod
    def trading_pair_associated_to_exchange_symbol(cls, symbol: str) -> str:
        """
        Convert exchange symbol to trading pair
        """
        return web_utils.convert_from_exchange_trading_pair(symbol)

    async def get_last_traded_prices(self,
                                   trading_pairs: List[str],
                                   domain: Optional[str] = None) -> Dict[str, float]:
        """
        Get last traded prices for trading pairs
        """
        return await self._get_last_traded_prices(trading_pairs=trading_pairs)

    async def _get_last_traded_prices(self, trading_pairs: List[str]) -> Dict[str, float]:
        """
        Fetch last traded prices from the exchange
        """
        results = {}
        try:
            rest_assistant = await self._api_factory.get_rest_assistant()
            
            for trading_pair in trading_pairs:
                symbol = self.exchange_symbol_associated_to_pair(trading_pair)
                params = {"symbol": symbol}
                
                response = await rest_assistant.execute_request(
                    url=web_utils.public_rest_url(CONSTANTS.TICKER_PATH_URL, domain=self._domain),
                    method=RESTMethod.GET,
                    params=params,
                    throttler_limit_id=CONSTANTS.TICKER_PATH_URL,
                )
                
                if response and "price" in response:
                    results[trading_pair] = float(response["price"])
                    
        except Exception as e:
            self.logger().error(f"Error fetching last traded prices: {e}", exc_info=True)
            
        return results

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        """
        Fetch order book snapshot
        """
        try:
            symbol = self.exchange_symbol_associated_to_pair(trading_pair)
            params = {"symbol": symbol}
            
            rest_assistant = await self._api_factory.get_rest_assistant()
            snapshot = await rest_assistant.execute_request(
                url=web_utils.public_rest_url(CONSTANTS.DEPTH_PATH_URL, domain=self._domain),
                method=RESTMethod.GET,
                params=params,
                throttler_limit_id=CONSTANTS.DEPTH_PATH_URL,
            )
            
            snapshot_timestamp = time.time()
            update_id = int(snapshot_timestamp * 1000)
            
            order_book_message = OrderBookMessage(
                message_type=OrderBookMessageType.SNAPSHOT,
                content={
                    "trading_pair": trading_pair,
                    "update_id": update_id,
                    "bids": [[Decimal(bid[0]), Decimal(bid[1])] for bid in snapshot.get("bids", [])],
                    "asks": [[Decimal(ask[0]), Decimal(ask[1])] for ask in snapshot.get("asks", [])],
                },
                timestamp=snapshot_timestamp,
            )
            
            return order_book_message
            
        except Exception as e:
            self.logger().error(f"Error fetching order book snapshot for {trading_pair}: {e}", exc_info=True)
            raise

    async def _parse_trade_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        """
        Parse trade message from WebSocket
        """
        try:
            stream = raw_message.get("stream", "")
            data = raw_message.get("data", {})
            
            if "trades" in stream:
                symbol = data.get("s", "")
                trading_pair = self.trading_pair_associated_to_exchange_symbol(symbol)
                
                if trading_pair in self._trading_pairs:
                    trade_message = OrderBookMessage(
                        message_type=OrderBookMessageType.TRADE,
                        content={
                            "trading_pair": trading_pair,
                            "trade_type": data.get("m", ""),  # True if buyer is market maker
                            "trade_id": data.get("t", ""),
                            "update_id": data.get("T", 0),
                            "price": Decimal(data.get("p", "0")),
                            "amount": Decimal(data.get("q", "0")),
                        },
                        timestamp=data.get("T", 0) / 1000,
                    )
                    
                    message_queue.put_nowait(trade_message)
                    
        except Exception as e:
            self.logger().error(f"Error parsing trade message: {e}", exc_info=True)

    async def _parse_order_book_diff_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        """
        Parse order book diff message from WebSocket
        """
        try:
            stream = raw_message.get("stream", "")
            data = raw_message.get("data", {})
            
            if "depth" in stream:
                symbol = data.get("s", "")
                trading_pair = self.trading_pair_associated_to_exchange_symbol(symbol)
                
                if trading_pair in self._trading_pairs:
                    diff_message = OrderBookMessage(
                        message_type=OrderBookMessageType.DIFF,
                        content={
                            "trading_pair": trading_pair,
                            "update_id": data.get("u", 0),
                            "first_update_id": data.get("U", 0),
                            "bids": [[Decimal(bid[0]), Decimal(bid[1])] for bid in data.get("b", [])],
                            "asks": [[Decimal(ask[0]), Decimal(ask[1])] for ask in data.get("a", [])],
                        },
                        timestamp=data.get("E", 0) / 1000,
                    )
                    
                    message_queue.put_nowait(diff_message)
                    
        except Exception as e:
            self.logger().error(f"Error parsing order book diff message: {e}", exc_info=True)

    async def _connected_websocket_assistant(self) -> WSAssistant:
        """
        Create and connect WebSocket assistant
        """
        ws_assistant = await self._api_factory.get_ws_assistant()
        await ws_assistant.connect(ws_url=web_utils.wss_url(self._domain), ping_timeout=CONSTANTS.SECONDS_TO_WAIT_TO_RECEIVE_MESSAGE)
        return ws_assistant

    async def _subscribe_channels(self, ws_assistant: WSAssistant):
        """
        Subscribe to WebSocket channels for order book data
        """
        try:
            # Subscribe to depth streams for each trading pair
            for trading_pair in self._trading_pairs:
                symbol = self.exchange_symbol_associated_to_pair(trading_pair)
                
                # Subscribe to depth updates
                depth_request = WSJSONRequest({
                    "method": "SUBSCRIBE",
                    "params": [f"{symbol.lower()}@depth"],
                    "id": self.DIFF_STREAM_ID,
                })
                await ws_assistant.send(depth_request)
                
                # Subscribe to trade updates
                trade_request = WSJSONRequest({
                    "method": "SUBSCRIBE", 
                    "params": [f"{symbol.lower()}@trade"],
                    "id": self.TRADE_STREAM_ID,
                })
                await ws_assistant.send(trade_request)
                
            self.logger().info("Subscribed to WebSocket channels")
            
        except Exception as e:
            self.logger().error(f"Error subscribing to WebSocket channels: {e}", exc_info=True)
            raise

    async def _process_websocket_messages(self, websocket_assistant: WSAssistant, message_queue: asyncio.Queue):
        """
        Process WebSocket messages
        """
        try:
            async for ws_response in websocket_assistant.iter_messages():
                data = ws_response.data
                
                if isinstance(data, dict):
                    stream = data.get("stream", "")
                    
                    if "depth" in stream:
                        await self._parse_order_book_diff_message(data, message_queue)
                    elif "trade" in stream:
                        await self._parse_trade_message(data, message_queue)
                    elif data.get("result") is not None:
                        # Subscription confirmation
                        self.logger().info(f"WebSocket subscription confirmed: {data}")
                    elif "ping" in data:
                        # Handle ping/pong
                        await websocket_assistant.send(WSJSONRequest({"pong": data["ping"]}))
                        
        except Exception as e:
            self.logger().error(f"Error processing WebSocket messages: {e}", exc_info=True)
            raise

    async def listen_for_snapshots(self, ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue):
        """
        Listen for order book snapshots
        """
        while True:
            try:
                for trading_pair in self._trading_pairs:
                    try:
                        snapshot_msg = await self._order_book_snapshot(trading_pair)
                        output.put_nowait(snapshot_msg)
                        self.logger().debug(f"Saved order book snapshot for {trading_pair}")
                        await self._sleep(1.0)  # Rate limiting between snapshots
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self.logger().error(f"Unexpected error fetching order book snapshot for {trading_pair}: {e}",
                                          exc_info=True)
                        await self._sleep(5.0)
                        
                await self._sleep(30.0)  # Refresh snapshots every 30 seconds
                
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error in listen_for_snapshots: {e}", exc_info=True)
                await self._sleep(5.0)

    async def listen_for_trades(self, ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue):
        """
        Listen for trade updates via WebSocket
        """
        message_queue = asyncio.Queue()
        while True:
            try:
                ws_assistant = await self._connected_websocket_assistant()
                await self._subscribe_channels(ws_assistant)
                
                listen_task = asyncio.create_task(
                    self._process_websocket_messages(ws_assistant, message_queue)
                )
                
                # Process messages from the queue
                while True:
                    try:
                        message = await asyncio.wait_for(message_queue.get(), timeout=1.0)
                        if message.type == OrderBookMessageType.TRADE:
                            output.put_nowait(message)
                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        listen_task.cancel()
                        raise
                        
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error in listen_for_trades: {e}", exc_info=True)
                await self._sleep(5.0)

    async def listen_for_order_book_diffs(self, ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue):
        """
        Listen for order book diff updates via WebSocket
        """
        message_queue = asyncio.Queue()
        while True:
            try:
                ws_assistant = await self._connected_websocket_assistant()
                await self._subscribe_channels(ws_assistant)
                
                listen_task = asyncio.create_task(
                    self._process_websocket_messages(ws_assistant, message_queue)
                )
                
                # Process messages from the queue
                while True:
                    try:
                        message = await asyncio.wait_for(message_queue.get(), timeout=1.0)
                        if message.type == OrderBookMessageType.DIFF:
                            output.put_nowait(message)
                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        listen_task.cancel()
                        raise
                        
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error in listen_for_order_book_diffs: {e}", exc_info=True)
                await self._sleep(5.0)

    async def _sleep(self, delay: float):
        """
        Sleep for the given delay
        """
        await asyncio.sleep(delay)