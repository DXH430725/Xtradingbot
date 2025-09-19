"""
Simplified constants for testing without complex dependencies
"""
from decimal import Decimal

EXCHANGE_NAME = "backpack_perpetual"
DEFAULT_DOMAIN = "backpack_perpetual_main"
DEFAULT_TIME_IN_FORCE = "GTC"

# Base URLs
REST_URL = "https://api.backpack.exchange"
WSS_URL = "wss://ws.backpack.exchange"

# API endpoints
SYSTEM_STATUS_PATH_URL = "/api/v1/status"
MARKETS_PATH_URL = "/api/v1/markets"
TICKER_PATH_URL = "/api/v1/ticker"
DEPTH_PATH_URL = "/api/v1/depth"

# Private endpoints
ACCOUNT_PATH_URL = "/wapi/v1/account"
BALANCES_PATH_URL = "/wapi/v1/balances"
ORDER_PATH_URL = "/wapi/v1/order"
ORDERS_PATH_URL = "/wapi/v1/orders"
POSITIONS_PATH_URL = "/wapi/v1/positions"

# Order types mapping - simple string mapping
ORDER_TYPE_MAP = {
    "LIMIT": "Limit",
    "MARKET": "Market",
}

# Order states mapping - simple string mapping  
ORDER_STATE_MAP = {
    "New": "OPEN",
    "PartiallyFilled": "PARTIALLY_FILLED",
    "Filled": "FILLED",
    "Cancelled": "CANCELED",
    "Rejected": "FAILED",
    "Expired": "FAILED",
}

# Order ID settings
HBOT_ORDER_ID_PREFIX = "BPCK-"
MAX_ORDER_ID_LEN = 36

# Authentication settings
X_API_WINDOW_DEFAULT = 5000
X_API_WINDOW_MAX = 60000

# Error codes
RET_CODE_OK = "200"
RET_CODE_AUTH_ERROR = "401"