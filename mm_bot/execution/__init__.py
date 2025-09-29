"""Execution layer helpers (order tracking, executors)."""

from .orders import FINAL_STATES, OrderState, OrderTracker, OrderUpdate, TrackingLimitOrder, TrackingMarketOrder, TrackingOrder
from .tracking_limit import TrackingLimitTimeoutError, place_tracking_limit_order
from .order_actions import place_tracking_market_order
from .ids import COIManager, NonceManager
from .positions import confirm_position, rebalance_position, resolve_filled_amount
from .data import get_position, get_collateral, plan_order_size
from .utils import wait_random
# Import new router as main interface, with backwards compatibility
from .router import ExecutionRouter, ExecutionLayer  # ExecutionLayer is alias for backwards compatibility
from .order import Order, OrderEvent  # New unified order classes
from .telemetry import TelemetryClient, TelemetryConfig
from .notifier import TelegramNotifier, load_telegram_keys
from .symbols import SymbolMapper
from .emergency import emergency_unwind

__all__ = [
    # Legacy order classes (deprecated, use Order/OrderEvent)
    "FINAL_STATES",
    "OrderState",
    "OrderTracker",
    "OrderUpdate",
    "TrackingLimitOrder",
    "TrackingMarketOrder",
    "TrackingOrder",
    # New unified order classes
    "Order",
    "OrderEvent",
    # Core functions
    "TrackingLimitTimeoutError",
    "place_tracking_limit_order",
    "place_tracking_market_order",
    "COIManager",
    "NonceManager",
    "confirm_position",
    "rebalance_position",
    "resolve_filled_amount",
    "get_position",
    "get_collateral",
    "plan_order_size",
    "wait_random",
    # Execution interfaces (ExecutionLayer = ExecutionRouter for compatibility)
    "ExecutionLayer",
    "ExecutionRouter",
    # Utilities
    "TelemetryClient",
    "TelemetryConfig",
    "TelegramNotifier",
    "load_telegram_keys",
    "SymbolMapper",
    "emergency_unwind",
]
