from hummingbot.core.api_throttler.data_types import RateLimit
from hummingbot.core.data_type.common import OrderType, PositionMode
from hummingbot.core.data_type.in_flight_order import OrderState

EXCHANGE_NAME = "backpack_perpetual"

DEFAULT_DOMAIN = "backpack_perpetual_main"

DEFAULT_TIME_IN_FORCE = "GTC"

# Base URLs
REST_URL = "https://api.backpack.exchange"
WSS_URL = "wss://ws.backpack.exchange"

# API endpoints
SYSTEM_STATUS_PATH_URL = "/api/v1/status"
MARKETS_PATH_URL = "/api/v1/markets"
ASSETS_PATH_URL = "/api/v1/assets"
TICKER_PATH_URL = "/api/v1/ticker"
DEPTH_PATH_URL = "/api/v1/depth"
TRADES_PATH_URL = "/api/v1/trades"
KLINES_PATH_URL = "/api/v1/klines"

# Private endpoints
ACCOUNT_PATH_URL = "/wapi/v1/account"
BALANCES_PATH_URL = "/wapi/v1/balances"
DEPOSITS_PATH_URL = "/wapi/v1/deposits"
WITHDRAWALS_PATH_URL = "/wapi/v1/withdrawals"
ORDER_PATH_URL = "/wapi/v1/order"
ORDERS_PATH_URL = "/wapi/v1/orders"
FILLS_PATH_URL = "/wapi/v1/fills"
POSITIONS_PATH_URL = "/wapi/v1/positions"

# WebSocket topics
WS_HEARTBEAT_REQUEST = "ping"
WS_HEARTBEAT_RESPONSE = "pong"
WS_DEPTH_TOPIC = "depth"
WS_TICKER_TOPIC = "ticker"
WS_TRADES_TOPIC = "trades"
WS_KLINES_TOPIC = "klines"
WS_BOOK_TICKER_TOPIC = "bookTicker"
WS_MARK_PRICE_TOPIC = "markPrice"
WS_LIQUIDATION_TOPIC = "liquidation"
WS_OPEN_INTEREST_TOPIC = "openInterest"

# Private WebSocket topics
WS_ORDER_UPDATE_TOPIC = "orderUpdate"
WS_POSITION_UPDATE_TOPIC = "positionUpdate"

# Order types mapping
ORDER_TYPE_MAP = {
    OrderType.LIMIT: "Limit",
    OrderType.MARKET: "Market",
}

# Position modes
POSITION_MODE_API_ONEWAY = "OneWay"
POSITION_MODE_API_HEDGE = "Hedge"
POSITION_MODE_MAP = {
    PositionMode.ONEWAY: POSITION_MODE_API_ONEWAY,
    PositionMode.HEDGE: POSITION_MODE_API_HEDGE,
}

# Order states mapping
ORDER_STATE_MAP = {
    "New": OrderState.OPEN,
    "PartiallyFilled": OrderState.PARTIALLY_FILLED,
    "Filled": OrderState.FILLED,
    "Cancelled": OrderState.CANCELED,
    "Rejected": OrderState.FAILED,
    "Expired": OrderState.FAILED,
}

# Order ID and client order ID settings
HBOT_ORDER_ID_PREFIX = "BPCK-"
MAX_ORDER_ID_LEN = 36

# Request timeout settings
REQUEST_TIMEOUT = 30.0
SECONDS_TO_WAIT_TO_RECEIVE_MESSAGE = 30

# Authentication settings
X_API_WINDOW_DEFAULT = 5000
X_API_WINDOW_MAX = 60000

# Rate limits
RATE_LIMITS = [
    # Public endpoints
    RateLimit(
        limit_id=SYSTEM_STATUS_PATH_URL,
        limit=100,
        time_interval=60,
    ),
    RateLimit(
        limit_id=MARKETS_PATH_URL,
        limit=100,
        time_interval=60,
    ),
    RateLimit(
        limit_id=TICKER_PATH_URL,
        limit=100,
        time_interval=60,
    ),
    RateLimit(
        limit_id=DEPTH_PATH_URL,
        limit=100,
        time_interval=60,
    ),
    RateLimit(
        limit_id=TRADES_PATH_URL,
        limit=100,
        time_interval=60,
    ),
    RateLimit(
        limit_id=KLINES_PATH_URL,
        limit=100,
        time_interval=60,
    ),
    
    # Private endpoints (more restrictive)
    RateLimit(
        limit_id=ACCOUNT_PATH_URL,
        limit=10,
        time_interval=60,
    ),
    RateLimit(
        limit_id=BALANCES_PATH_URL,
        limit=10,
        time_interval=60,
    ),
    RateLimit(
        limit_id=ORDER_PATH_URL,
        limit=50,
        time_interval=60,
    ),
    RateLimit(
        limit_id=ORDERS_PATH_URL,
        limit=20,
        time_interval=60,
    ),
    RateLimit(
        limit_id=FILLS_PATH_URL,
        limit=20,
        time_interval=60,
    ),
    RateLimit(
        limit_id=POSITIONS_PATH_URL,
        limit=10,
        time_interval=60,
    ),
    RateLimit(
        limit_id=DEPOSITS_PATH_URL,
        limit=5,
        time_interval=60,
    ),
    RateLimit(
        limit_id=WITHDRAWALS_PATH_URL,
        limit=5,
        time_interval=60,
    ),
]

# Error codes
RET_CODE_OK = "200"
RET_CODE_AUTH_ERROR = "401" 
RET_CODE_FORBIDDEN = "403"
RET_CODE_NOT_FOUND = "404"
RET_CODE_RATE_LIMIT = "429"
RET_CODE_SERVER_ERROR = "500"