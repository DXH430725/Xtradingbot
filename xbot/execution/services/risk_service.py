"""Risk management and pre/post order checks service.

Handles order risk validation, exposure limits, and circuit breakers.
Target: < 300 lines.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, List
from dataclasses import dataclass, field


@dataclass
class RiskLimits:
    """Risk limits configuration."""
    max_order_size: float = 1000000.0  # Max single order size
    max_daily_volume: float = 10000000.0  # Max daily trading volume
    max_net_exposure: float = 5000000.0  # Max net exposure per symbol
    max_venue_exposure: float = 2000000.0  # Max exposure per venue
    min_collateral_ratio: float = 0.1  # Min collateral ratio
    max_error_rate: float = 0.05  # Max error rate before circuit breaker
    error_window_seconds: int = 300  # Error rate calculation window


@dataclass
class ErrorEvent:
    """Error event for circuit breaker tracking."""
    timestamp: float
    venue: str
    error_type: str
    message: str


class RiskService:
    """Service for order risk management and validation."""

    def __init__(self, *, logger: Optional[logging.Logger] = None) -> None:
        self.log = logger or logging.getLogger("xbot.execution.risk_service")

        # Connectors registry
        self._connectors: Dict[str, Any] = {}

        # Risk limits per venue
        self._risk_limits: Dict[str, RiskLimits] = {}

        # Circuit breaker state
        self._circuit_breaker_active: Dict[str, bool] = {}

        # Error tracking for circuit breaker
        self._error_events: List[ErrorEvent] = []

        # Daily volume tracking: venue -> symbol -> volume
        self._daily_volumes: Dict[str, Dict[str, float]] = {}
        self._volume_reset_time = time.time()

    def register_connector(
        self,
        venue: str,
        connector: Any,
        *,
        coi_limit: Optional[int] = None,
        api_key_index: Optional[int] = None,
    ) -> None:
        """Register connector for this venue."""
        key = venue.lower()
        self._connectors[key] = connector

        # Initialize risk limits with defaults
        if key not in self._risk_limits:
            self._risk_limits[key] = RiskLimits()

        # Initialize circuit breaker state
        self._circuit_breaker_active[key] = False

        self.log.debug(f"Risk service registered connector: {key}")

    def set_risk_limits(self, venue: str, limits: RiskLimits) -> None:
        """Set risk limits for venue."""
        venue_key = venue.lower()
        self._risk_limits[venue_key] = limits
        self.log.info(f"Updated risk limits for {venue}: {limits}")

    async def check_order_risk(
        self,
        venue: str,
        symbol: str,
        size_i: int,
        *,
        price_i: Optional[int] = None,
        is_ask: bool = False,
        **kwargs
    ) -> bool:
        """Check if order passes risk validation.

        Args:
            venue: Exchange venue
            symbol: Symbol to trade
            size_i: Order size in integer format
            price_i: Order price in integer format (if limit order)
            is_ask: True for sell, False for buy

        Returns:
            True if order passes risk checks, False otherwise
        """
        venue_key = venue.lower()

        # Check circuit breaker
        if self._circuit_breaker_active.get(venue_key, False):
            self.log.warning(f"Circuit breaker active for {venue}, rejecting order")
            return False

        # Get risk limits
        limits = self._risk_limits.get(venue_key, RiskLimits())

        try:
            # Check order size limits
            if not await self._check_order_size(venue, symbol, size_i, limits):
                return False

            # Check daily volume limits
            if not await self._check_daily_volume(venue, symbol, size_i, limits):
                return False

            # Check exposure limits
            if not await self._check_exposure_limits(venue, symbol, size_i, is_ask, limits):
                return False

            # Check collateral requirements
            if not await self._check_collateral(venue, symbol, size_i, price_i, limits):
                return False

            self.log.debug(f"Risk check passed for {venue}:{symbol} size={size_i}")
            return True

        except Exception as e:
            self.log.error(f"Risk check failed for {venue}:{symbol}: {e}")
            self._record_error(venue, "risk_check_error", str(e))
            return False

    async def _check_order_size(
        self,
        venue: str,
        symbol: str,
        size_i: int,
        limits: RiskLimits
    ) -> bool:
        """Check order size against limits."""
        # Convert to float for comparison (assuming 6 decimal places)
        size_float = size_i / 1_000_000

        if size_float > limits.max_order_size:
            self.log.warning(
                f"Order size {size_float} exceeds limit {limits.max_order_size} "
                f"for {venue}:{symbol}"
            )
            return False

        return True

    async def _check_daily_volume(
        self,
        venue: str,
        symbol: str,
        size_i: int,
        limits: RiskLimits
    ) -> bool:
        """Check daily volume limits."""
        self._reset_daily_volumes_if_needed()

        venue_key = venue.lower()
        symbol_key = symbol.upper()

        # Get current daily volume
        venue_volumes = self._daily_volumes.setdefault(venue_key, {})
        current_volume = venue_volumes.get(symbol_key, 0.0)

        # Convert size to float
        size_float = size_i / 1_000_000
        new_volume = current_volume + size_float

        if new_volume > limits.max_daily_volume:
            self.log.warning(
                f"Daily volume {new_volume} would exceed limit {limits.max_daily_volume} "
                f"for {venue}:{symbol}"
            )
            return False

        # Update volume tracking
        venue_volumes[symbol_key] = new_volume
        return True

    async def _check_exposure_limits(
        self,
        venue: str,
        symbol: str,
        size_i: int,
        is_ask: bool,
        limits: RiskLimits
    ) -> bool:
        """Check exposure limits."""
        # This would require position service integration
        # For now, implement basic check
        size_float = size_i / 1_000_000

        # Simple check: no single order exceeds venue exposure limit
        if size_float > limits.max_venue_exposure:
            self.log.warning(
                f"Order size {size_float} exceeds venue exposure limit "
                f"{limits.max_venue_exposure} for {venue}:{symbol}"
            )
            return False

        return True

    async def _check_collateral(
        self,
        venue: str,
        symbol: str,
        size_i: int,
        price_i: Optional[int],
        limits: RiskLimits
    ) -> bool:
        """Check collateral requirements."""
        venue_key = venue.lower()
        connector = self._connectors.get(venue_key)
        if not connector:
            return False

        try:
            # Get available collateral
            if hasattr(connector, 'get_account_overview'):
                account = await connector.get_account_overview()
                available = account.get('available_balance', 0.0)
            else:
                # Skip check if can't get collateral info
                return True

            # Estimate required collateral
            size_float = size_i / 1_000_000
            if price_i:
                price_float = price_i / 100  # Assuming 2 decimal places for price
                required_collateral = size_float * price_float * limits.min_collateral_ratio
            else:
                # For market orders, use conservative estimate
                required_collateral = size_float * 50000 * limits.min_collateral_ratio

            if available < required_collateral:
                self.log.warning(
                    f"Insufficient collateral: need {required_collateral}, "
                    f"available {available} for {venue}:{symbol}"
                )
                return False

            return True

        except Exception as e:
            self.log.error(f"Collateral check failed for {venue}:{symbol}: {e}")
            return False

    def _record_error(self, venue: str, error_type: str, message: str) -> None:
        """Record error event for circuit breaker tracking."""
        event = ErrorEvent(
            timestamp=time.time(),
            venue=venue.lower(),
            error_type=error_type,
            message=message
        )
        self._error_events.append(event)

        # Check if circuit breaker should be triggered
        self._check_circuit_breaker(venue)

    def _check_circuit_breaker(self, venue: str) -> None:
        """Check if circuit breaker should be activated."""
        venue_key = venue.lower()
        limits = self._risk_limits.get(venue_key, RiskLimits())

        current_time = time.time()
        window_start = current_time - limits.error_window_seconds

        # Count recent errors for this venue
        recent_errors = [
            e for e in self._error_events
            if e.venue == venue_key and e.timestamp >= window_start
        ]

        # Calculate error rate (errors per minute)
        error_rate = len(recent_errors) / (limits.error_window_seconds / 60)

        if error_rate > limits.max_error_rate:
            self._circuit_breaker_active[venue_key] = True
            self.log.error(
                f"Circuit breaker ACTIVATED for {venue}: "
                f"error rate {error_rate:.2f}/min > {limits.max_error_rate}/min"
            )

    def reset_circuit_breaker(self, venue: str) -> None:
        """Manually reset circuit breaker for venue."""
        venue_key = venue.lower()
        self._circuit_breaker_active[venue_key] = False
        self.log.info(f"Circuit breaker RESET for {venue}")

    def _reset_daily_volumes_if_needed(self) -> None:
        """Reset daily volumes if new day."""
        current_time = time.time()
        if current_time - self._volume_reset_time > 86400:  # 24 hours
            self._daily_volumes.clear()
            self._volume_reset_time = current_time
            self.log.info("Daily volumes reset for new day")

    def get_risk_status(self, venue: Optional[str] = None) -> Dict[str, Any]:
        """Get current risk status."""
        if venue:
            venue_key = venue.lower()
            return {
                'venue': venue,
                'circuit_breaker_active': self._circuit_breaker_active.get(venue_key, False),
                'limits': self._risk_limits.get(venue_key, RiskLimits()),
                'daily_volumes': self._daily_volumes.get(venue_key, {})
            }
        else:
            return {
                'circuit_breakers': self._circuit_breaker_active,
                'limits': self._risk_limits,
                'daily_volumes': self._daily_volumes,
                'total_errors': len(self._error_events)
            }

    def cleanup_old_errors(self, max_age_seconds: int = 3600) -> int:
        """Clean up old error events."""
        current_time = time.time()
        cutoff_time = current_time - max_age_seconds

        initial_count = len(self._error_events)
        self._error_events = [
            e for e in self._error_events if e.timestamp >= cutoff_time
        ]

        removed = initial_count - len(self._error_events)
        if removed > 0:
            self.log.debug(f"Cleaned up {removed} old error events")

        return removed