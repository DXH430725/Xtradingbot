import time
from typing import Any, Dict, Optional

import hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_constants as CONSTANTS
from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest
from hummingbot.core.web_assistant.rest_pre_processors import RESTPreProcessorBase
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


def public_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    """
    Creates a full URL for public REST API endpoints
    
    :param path_url: The API endpoint path
    :param domain: The domain identifier (not used for Backpack, kept for consistency)
    :return: The complete URL
    """
    return CONSTANTS.REST_URL + path_url


def private_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    """
    Creates a full URL for private REST API endpoints
    
    :param path_url: The API endpoint path  
    :param domain: The domain identifier (not used for Backpack, kept for consistency)
    :return: The complete URL
    """
    return CONSTANTS.REST_URL + path_url


def wss_url(domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    """
    Creates WebSocket URL for the given domain
    
    :param domain: The domain identifier (not used for Backpack, kept for consistency) 
    :return: WebSocket URL
    """
    return CONSTANTS.WSS_URL


class BackpackPerpetualRESTPreProcessor(RESTPreProcessorBase):
    """REST pre-processor for Backpack Perpetual API requests"""

    async def pre_process(self, request: RESTRequest) -> RESTRequest:
        """
        Pre-process REST requests before sending
        
        :param request: The request to pre-process
        :return: The processed request
        """
        if request.headers is None:
            request.headers = {}
            
        # Add standard headers
        request.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "hummingbot/backpack-connector"
        })
        
        return request


def build_api_factory(
        throttler: Optional[AsyncThrottler] = None,
        time_synchronizer: Optional[TimeSynchronizer] = None,
        domain: str = CONSTANTS.DEFAULT_DOMAIN,
        time_provider: Optional[TimeSynchronizer] = None,
        auth: Optional[AuthBase] = None,
) -> WebAssistantsFactory:
    """
    Create a WebAssistantsFactory for Backpack Perpetual API
    
    :param throttler: The API request throttler
    :param time_synchronizer: Time synchronizer for timestamps
    :param domain: The domain identifier
    :param time_provider: Time provider (deprecated, use time_synchronizer)
    :param auth: Authentication handler
    :return: WebAssistantsFactory instance
    """
    if time_provider is not None:
        time_synchronizer = time_provider
        
    throttler = throttler or create_throttler()
    api_factory = WebAssistantsFactory(
        throttler=throttler,
        auth=auth,
        rest_pre_processors=[BackpackPerpetualRESTPreProcessor()],
    )
    return api_factory


def build_api_factory_without_time_synchronizer_pre_processor(throttler: AsyncThrottler) -> WebAssistantsFactory:
    """
    Create a simplified WebAssistantsFactory without time synchronizer pre-processor
    
    :param throttler: The API request throttler
    :return: WebAssistantsFactory instance
    """
    api_factory = WebAssistantsFactory(
        throttler=throttler,
        rest_pre_processors=[BackpackPerpetualRESTPreProcessor()],
    )
    return api_factory


def create_throttler() -> AsyncThrottler:
    """
    Create an AsyncThrottler with Backpack Perpetual rate limits
    
    :return: AsyncThrottler instance
    """
    return AsyncThrottler(CONSTANTS.RATE_LIMITS)


def format_trading_pair(trading_pair: str) -> str:
    """
    Convert Hummingbot trading pair to Backpack format
    
    :param trading_pair: Hummingbot format trading pair (e.g., BTC-USDC)
    :return: Backpack format trading pair (e.g., BTC_USDC)
    """
    return trading_pair.replace("-", "_")


def convert_from_exchange_trading_pair(exchange_trading_pair: str) -> str:
    """
    Convert Backpack trading pair format to Hummingbot format
    
    :param exchange_trading_pair: Backpack format trading pair (e.g., BTC_USDC)
    :return: Hummingbot format trading pair (e.g., BTC-USDC) 
    """
    return exchange_trading_pair.replace("_", "-")


def get_current_server_time() -> int:
    """
    Get current server time in milliseconds
    For Backpack, we use local time as there's no dedicated server time endpoint
    
    :return: Current time in milliseconds
    """
    return int(time.time() * 1000)


def ms_timestamp_to_s(timestamp: int) -> float:
    """
    Convert millisecond timestamp to seconds
    
    :param timestamp: Timestamp in milliseconds
    :return: Timestamp in seconds
    """
    return timestamp / 1000


def s_timestamp_to_ms(timestamp: float) -> int:
    """
    Convert seconds timestamp to milliseconds
    
    :param timestamp: Timestamp in seconds
    :return: Timestamp in milliseconds  
    """
    return int(timestamp * 1000)