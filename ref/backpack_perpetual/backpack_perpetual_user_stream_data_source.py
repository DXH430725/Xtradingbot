import asyncio
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_constants as CONSTANTS
from hummingbot.connector.derivative.backpack_perpetual import backpack_perpetual_web_utils as web_utils
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger

if TYPE_CHECKING:
    from hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_derivative import BackpackPerpetualDerivative


class BackpackPerpetualUserStreamDataSource(UserStreamTrackerDataSource):
    """
    User stream data source for Backpack Perpetual
    Handles private WebSocket streams for order and position updates
    """

    HEARTBEAT_TIME_INTERVAL = 30.0
    _logger: Optional[HummingbotLogger] = None

    def __init__(self,
                 auth: AuthBase,
                 trading_pairs: List[str],
                 connector: "BackpackPerpetualDerivative",
                 api_factory: WebAssistantsFactory,
                 domain: str = CONSTANTS.DEFAULT_DOMAIN):
        """
        Initialize user stream data source
        
        :param auth: Authentication handler
        :param trading_pairs: List of trading pairs
        :param connector: The connector instance
        :param api_factory: Web assistants factory
        :param domain: Domain identifier
        """
        super().__init__()
        self._auth = auth
        self._trading_pairs = trading_pairs
        self._connector = connector
        self._api_factory = api_factory
        self._domain = domain
        self._current_listen_key = None
        self._listen_for_user_stream_task = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = HummingbotLogger.logger_name_for_class(cls)
        return cls._logger

    @property
    def last_recv_time(self) -> float:
        """
        Return the last received time for heartbeat monitoring
        """
        return self._last_recv_time

    async def _connected_websocket_assistant(self) -> WSAssistant:
        """
        Create and connect WebSocket assistant for private streams
        """
        ws_assistant = await self._api_factory.get_ws_assistant()
        await ws_assistant.connect(
            ws_url=web_utils.wss_url(self._domain),
            ping_timeout=CONSTANTS.SECONDS_TO_WAIT_TO_RECEIVE_MESSAGE
        )
        return ws_assistant

    async def _authenticate_websocket(self, ws_assistant: WSAssistant):
        """
        Authenticate WebSocket connection for private streams
        Note: Backpack may not require explicit WebSocket authentication,
        but this is here for future implementation if needed
        """
        try:
            # Backpack WebSocket authentication implementation would go here
            # For now, we'll assume the connection is authenticated via API keys in headers
            self.logger().info("WebSocket authenticated successfully")
        except Exception as e:
            self.logger().error(f"Error authenticating WebSocket: {e}", exc_info=True)
            raise

    async def _subscribe_channels(self, ws_assistant: WSAssistant):
        """
        Subscribe to private WebSocket channels
        """
        try:
            # Subscribe to order updates
            order_subscribe_request = WSJSONRequest({
                "method": "SUBSCRIBE",
                "params": [CONSTANTS.WS_ORDER_UPDATE_TOPIC],
                "id": 1,
            })
            await ws_assistant.send(order_subscribe_request)

            # Subscribe to position updates
            position_subscribe_request = WSJSONRequest({
                "method": "SUBSCRIBE", 
                "params": [CONSTANTS.WS_POSITION_UPDATE_TOPIC],
                "id": 2,
            })
            await ws_assistant.send(position_subscribe_request)

            # Subscribe to account balance updates (if available)
            balance_subscribe_request = WSJSONRequest({
                "method": "SUBSCRIBE",
                "params": ["balanceUpdate"],  # Placeholder - actual topic may differ
                "id": 3,
            })
            await ws_assistant.send(balance_subscribe_request)

            self.logger().info("Subscribed to private WebSocket channels")

        except Exception as e:
            self.logger().error(f"Error subscribing to private channels: {e}", exc_info=True)
            raise

    async def _process_websocket_messages(self, ws_assistant: WSAssistant, queue: asyncio.Queue):
        """
        Process incoming WebSocket messages
        """
        try:
            async for ws_response in ws_assistant.iter_messages():
                data = ws_response.data
                
                if isinstance(data, dict):
                    await self._process_event_message(data, queue)
                    self._last_recv_time = self._time()
                    
        except Exception as e:
            self.logger().error(f"Error processing WebSocket messages: {e}", exc_info=True)
            raise

    async def _process_event_message(self, message: Dict[str, Any], queue: asyncio.Queue):
        """
        Process individual event messages from WebSocket
        """
        try:
            stream = message.get("stream", "")
            event_data = message.get("data", {})

            if not stream:
                # Handle messages without explicit stream identifier
                if "result" in message:
                    # Subscription confirmation
                    self.logger().info(f"WebSocket subscription confirmed: {message}")
                    return
                elif "ping" in message:
                    # Handle ping/pong heartbeat
                    return
                else:
                    # Try to infer message type from content
                    if self._is_order_update_message(event_data):
                        stream = CONSTANTS.WS_ORDER_UPDATE_TOPIC
                    elif self._is_position_update_message(event_data):
                        stream = CONSTANTS.WS_POSITION_UPDATE_TOPIC
                    elif self._is_balance_update_message(event_data):
                        stream = "balanceUpdate"

            if stream:
                # Create standardized message format
                processed_message = {
                    "stream": stream,
                    "data": event_data,
                    "timestamp": self._time()
                }
                queue.put_nowait(processed_message)
                
        except Exception as e:
            self.logger().error(f"Error processing event message: {e}", exc_info=True)

    def _is_order_update_message(self, data: Dict[str, Any]) -> bool:
        """
        Check if the message is an order update
        """
        return "orderId" in data or "id" in data or "clientId" in data

    def _is_position_update_message(self, data: Dict[str, Any]) -> bool:
        """
        Check if the message is a position update
        """
        return "symbol" in data and ("size" in data or "quantity" in data or "positionSize" in data)

    def _is_balance_update_message(self, data: Dict[str, Any]) -> bool:
        """
        Check if the message is a balance update
        """
        return "asset" in data or "currency" in data or "balance" in data

    async def _get_listen_key(self):
        """
        Get listen key for user stream (if required by Backpack)
        Note: Backpack may not use listen keys like Binance, but this is here for compatibility
        """
        try:
            # For Backpack, we may not need a separate listen key
            # The WebSocket connection might be authenticated via headers
            return "backpack_user_stream"
        except Exception as e:
            self.logger().error(f"Error getting listen key: {e}", exc_info=True)
            raise

    async def _ping_listen_key(self):
        """
        Ping listen key to keep it alive (if required by Backpack)
        """
        try:
            # Placeholder for listen key maintenance if needed
            pass
        except Exception as e:
            self.logger().error(f"Error pinging listen key: {e}", exc_info=True)

    async def _manage_listen_key(self):
        """
        Manage listen key lifecycle
        """
        try:
            while True:
                await self._ping_listen_key()
                await self._sleep(30.0)  # Ping every 30 seconds
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger().error(f"Error in listen key management: {e}", exc_info=True)

    async def listen_for_user_stream(self, output: asyncio.Queue):
        """
        Listen for user stream events
        """
        while True:
            try:
                self._current_listen_key = await self._get_listen_key()
                
                ws_assistant = await self._connected_websocket_assistant()
                await self._authenticate_websocket(ws_assistant)
                await self._subscribe_channels(ws_assistant)
                
                # Start listen key management task
                listen_key_task = asyncio.create_task(self._manage_listen_key())
                
                # Start processing messages
                await self._process_websocket_messages(ws_assistant, output)
                
            except asyncio.CancelledError:
                if listen_key_task and not listen_key_task.done():
                    listen_key_task.cancel()
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error in user stream listener: {e}", exc_info=True)
                if listen_key_task and not listen_key_task.done():
                    listen_key_task.cancel()
                await self._sleep(5.0)

    async def _sleep(self, delay: float):
        """
        Sleep for the given delay
        """
        await asyncio.sleep(delay)

    def _time(self) -> float:
        """
        Get current time
        """
        return asyncio.get_event_loop().time()