from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

import hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_constants as CONSTANTS
from hummingbot.client.config.config_methods import using_exchange
from hummingbot.core.data_type.trade_fee import TradeFeeSchema


CENTRALIZED = True
EXAMPLE_PAIR = "BTC-USDC"
DEFAULT_FEES = TradeFeeSchema(
    maker_percent_fee_decimal=Decimal("0.0002"),  # 0.02% maker fee (estimated)
    taker_percent_fee_decimal=Decimal("0.0005"),  # 0.05% taker fee (estimated)
)


def is_exchange_information_valid(exchange_info: Dict[str, Any]) -> bool:
    """
    Validate exchange information response
    
    :param exchange_info: Exchange information from API
    :return: True if valid, False otherwise
    """
    if not isinstance(exchange_info, dict):
        return False
        
    # Check if we have markets data
    return "symbols" in exchange_info or len(exchange_info) > 0


def build_rate_limits_by_tier(api_tier: str = "1") -> list:
    """
    Build rate limits based on API tier
    
    :param api_tier: API tier level
    :return: List of rate limits
    """
    # For now, return default rate limits
    # This can be extended if Backpack provides tier-based limits
    return CONSTANTS.RATE_LIMITS


def convert_snapshot_message_to_order_book_row(message: Dict[str, Any]) -> Tuple[list, list]:
    """
    Convert depth snapshot message to order book format
    
    :param message: WebSocket depth message
    :return: Tuple of (bids, asks) lists
    """
    bids = []
    asks = []
    
    if "bids" in message:
        bids = [[price, size] for price, size in message["bids"]]
        
    if "asks" in message:
        asks = [[price, size] for price, size in message["asks"]]
        
    return bids, asks


def convert_diff_message_to_order_book_row(message: Dict[str, Any]) -> Tuple[list, list]:
    """
    Convert depth diff message to order book format
    
    :param message: WebSocket depth diff message  
    :return: Tuple of (bids, asks) lists
    """
    # For Backpack, diff messages might have the same format as snapshots
    return convert_snapshot_message_to_order_book_row(message)


def get_trading_pair_from_symbol(symbol: str) -> str:
    """
    Convert Backpack symbol to Hummingbot trading pair format
    
    :param symbol: Backpack symbol (e.g., BTC_USDC)
    :return: Hummingbot trading pair (e.g., BTC-USDC)
    """
    return symbol.replace("_", "-")


def get_symbol_from_trading_pair(trading_pair: str) -> str:
    """
    Convert Hummingbot trading pair to Backpack symbol format
    
    :param trading_pair: Hummingbot trading pair (e.g., BTC-USDC)
    :return: Backpack symbol (e.g., BTC_USDC)
    """
    return trading_pair.replace("-", "_")


def is_linear_perpetual(symbol: str) -> bool:
    """
    Check if symbol is a linear perpetual contract
    
    :param symbol: Trading symbol
    :return: True if linear perpetual, False otherwise
    """
    # Backpack perpetual contracts typically end with _PERP or similar
    # This may need adjustment based on actual Backpack naming conventions
    return "_PERP" in symbol.upper() or "USDC" in symbol or "USDT" in symbol


def is_inverse_perpetual(symbol: str) -> bool:
    """
    Check if symbol is an inverse perpetual contract
    
    :param symbol: Trading symbol  
    :return: True if inverse perpetual, False otherwise
    """
    # Backpack may have inverse perpetuals, but format is unclear
    # This is a placeholder and needs adjustment based on actual contracts
    return False


def get_position_mode_from_api(api_position_mode: str) -> str:
    """
    Convert API position mode to standard format
    
    :param api_position_mode: Position mode from API
    :return: Standard position mode
    """
    mode_map = {v: k for k, v in CONSTANTS.POSITION_MODE_MAP.items()}
    return mode_map.get(api_position_mode, api_position_mode)


def get_api_position_mode(position_mode: str) -> str:
    """
    Convert standard position mode to API format
    
    :param position_mode: Standard position mode
    :return: API position mode
    """
    return CONSTANTS.POSITION_MODE_MAP.get(position_mode, position_mode)


@using_exchange("backpack_perpetual")
def get_backpack_perpetual_mid_price(trading_pair: str) -> Optional[Decimal]:
    """
    Get mid price for a Backpack Perpetual trading pair
    This is a placeholder for price retrieval functionality
    
    :param trading_pair: The trading pair
    :return: Mid price or None
    """
    # This would need to be implemented with actual price fetching logic
    return None


def get_trading_fees() -> TradeFeeSchema:
    """
    Get trading fees for Backpack Perpetual
    
    :return: TradeFeeSchema with maker and taker fees
    """
    return DEFAULT_FEES


def normalize_trading_symbol(symbol: str) -> str:
    """
    Normalize trading symbol to standard format
    
    :param symbol: Raw symbol from API
    :return: Normalized symbol
    """
    # Remove any extra formatting and ensure consistent naming
    return symbol.upper().strip()


def parse_timestamp(timestamp_str: str) -> int:
    """
    Parse timestamp string to integer milliseconds
    
    :param timestamp_str: Timestamp as string
    :return: Timestamp as integer milliseconds
    """
    try:
        # Handle both string and numeric timestamps
        if isinstance(timestamp_str, str):
            return int(float(timestamp_str))
        else:
            return int(timestamp_str)
    except (ValueError, TypeError):
        return 0