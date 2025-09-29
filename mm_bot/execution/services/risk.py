"""Risk service - pre/post order risk checks and exposure management."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .position import PositionService
from .symbol import SymbolService


class RiskService:
    """Service for risk management - exposure limits, collateral checks, position limits.

    Provides pre-order risk validation and post-order risk monitoring.
    """

    def __init__(self, *, logger: Optional[logging.Logger] = None) -> None:
        self.log = logger or logging.getLogger("mm_bot.execution.services.risk")

        # Dependencies
        self._symbol_service = SymbolService(logger=self.log)
        self._position_service = PositionService(logger=self.log)

        # Connectors
        self._connectors: Dict[str, Any] = {}

        # Risk configuration
        self._max_position_ratio = 0.8  # Max position as ratio of collateral
        self._min_collateral_buffer = 0.1  # Keep 10% collateral buffer
        self._max_venue_concentration = 0.6  # Max 60% of total position on single venue
        self._max_order_size_ratio = 0.2  # Max single order as ratio of current position

    def register_connector(self, venue: str, connector: Any) -> None:
        """Register connector for risk calculations."""
        key = venue.lower()
        self._connectors[key] = connector
        self._symbol_service.register_connector(key, connector)
        self._position_service.register_connector(key, connector)

    async def check_pre_order_risk(
        self,
        venue: str,
        canonical_symbol: str,
        size_i: int,
        is_ask: bool,
    ) -> None:
        """Pre-order risk validation.

        Raises:
            ValueError: If order violates risk limits
        """
        try:
            await self._check_minimum_size(venue, canonical_symbol, size_i)
            await self._check_collateral_adequacy(venue, canonical_symbol, size_i, is_ask)
            await self._check_position_limits(venue, canonical_symbol, size_i, is_ask)
            await self._check_concentration_limits(venue, canonical_symbol, size_i, is_ask)

        except Exception as e:
            self.log.error(
                "Pre-order risk check failed for %s:%s size=%d is_ask=%s - %s",
                venue, canonical_symbol, size_i, is_ask, e
            )
            raise

    async def check_post_order_risk(
        self,
        venue: str,
        canonical_symbol: str,
        filled_size_i: int,
        is_ask: bool,
    ) -> Dict[str, Any]:
        """Post-order risk assessment.

        Returns:
            Risk metrics and warnings
        """
        try:
            # Calculate new position after fill
            current_position = await self._position_service.get_position(venue, canonical_symbol)
            size_scale = await self._symbol_service.get_size_scale(venue, canonical_symbol)
            filled_size = filled_size_i / size_scale

            new_position = current_position + (filled_size if not is_ask else -filled_size)

            # Get cross-venue exposure
            net_positions = await self._position_service.get_net_position(canonical_symbol)
            new_net_position = net_positions['net'] + (filled_size if not is_ask else -filled_size)

            # Calculate risk metrics
            collaterals = await self._position_service.get_total_collateral()
            total_collateral = collaterals['total']

            risk_metrics = {
                "venue_position": new_position,
                "net_position": new_net_position,
                "total_collateral": total_collateral,
                "position_ratio": abs(new_net_position) / max(total_collateral, 1),
                "venue_concentration": abs(new_position) / max(abs(new_net_position), 1),
                "warnings": [],
            }

            # Generate warnings
            if risk_metrics["position_ratio"] > self._max_position_ratio:
                risk_metrics["warnings"].append(
                    f"High position ratio: {risk_metrics['position_ratio']:.2%} > {self._max_position_ratio:.2%}"
                )

            if risk_metrics["venue_concentration"] > self._max_venue_concentration:
                risk_metrics["warnings"].append(
                    f"High venue concentration: {risk_metrics['venue_concentration']:.2%} > {self._max_venue_concentration:.2%}"
                )

            return risk_metrics

        except Exception as e:
            self.log.error(
                "Post-order risk check failed for %s:%s - %s",
                venue, canonical_symbol, e
            )
            return {"error": str(e)}

    async def get_max_order_size(
        self,
        venue: str,
        canonical_symbol: str,
        is_ask: bool,
    ) -> int:
        """Calculate maximum safe order size for venue/symbol."""
        try:
            # Get current state
            collateral = await self._position_service.get_collateral(venue)
            current_position = await self._position_service.get_position(venue, canonical_symbol)
            size_scale = await self._symbol_service.get_size_scale(venue, canonical_symbol)
            min_size = await self._symbol_service.get_min_size_i(venue, canonical_symbol)

            # Calculate based on collateral limits
            max_position_value = collateral * self._max_position_ratio
            current_position_value = abs(current_position)

            if is_ask:
                # Selling - limited by current long position
                max_sell_size = max(0, current_position)
            else:
                # Buying - limited by remaining collateral capacity
                remaining_capacity = max_position_value - current_position_value
                max_buy_size = remaining_capacity

            max_size_float = max_sell_size if is_ask else max_buy_size
            max_size_i = int(max_size_float * size_scale)

            # Apply minimum size constraint
            if max_size_i < min_size:
                return 0

            return max_size_i

        except Exception as e:
            self.log.error(
                "Failed to calculate max order size for %s:%s - %s",
                venue, canonical_symbol, e
            )
            return 0

    async def _check_minimum_size(self, venue: str, canonical_symbol: str, size_i: int) -> None:
        """Verify order meets minimum size requirements."""
        min_size = await self._symbol_service.get_min_size_i(venue, canonical_symbol)
        if size_i < min_size:
            raise ValueError(
                f"Order size {size_i} below minimum {min_size} for {venue}:{canonical_symbol}"
            )

    async def _check_collateral_adequacy(
        self,
        venue: str,
        canonical_symbol: str,
        size_i: int,
        is_ask: bool,
    ) -> None:
        """Verify sufficient collateral for order."""
        if is_ask:
            return  # Selling doesn't require additional collateral

        collateral = await self._position_service.get_collateral(venue)
        size_scale = await self._symbol_service.get_size_scale(venue, canonical_symbol)
        order_size = size_i / size_scale

        # Rough estimate: assume 1:1 collateral requirement (conservative)
        required_collateral = order_size
        available_collateral = collateral * (1 - self._min_collateral_buffer)

        if required_collateral > available_collateral:
            raise ValueError(
                f"Insufficient collateral: need {required_collateral:.2f}, "
                f"available {available_collateral:.2f} for {venue}:{canonical_symbol}"
            )

    async def _check_position_limits(
        self,
        venue: str,
        canonical_symbol: str,
        size_i: int,
        is_ask: bool,
    ) -> None:
        """Verify order doesn't exceed position limits."""
        current_position = await self._position_service.get_position(venue, canonical_symbol)
        size_scale = await self._symbol_service.get_size_scale(venue, canonical_symbol)
        order_size = size_i / size_scale

        # Calculate new position
        new_position = current_position + (order_size if not is_ask else -order_size)

        # Check against collateral-based limit
        collateral = await self._position_service.get_collateral(venue)
        max_position = collateral * self._max_position_ratio

        if abs(new_position) > max_position:
            raise ValueError(
                f"Position limit exceeded: new position {new_position:.2f} > limit {max_position:.2f} "
                f"for {venue}:{canonical_symbol}"
            )

    async def _check_concentration_limits(
        self,
        venue: str,
        canonical_symbol: str,
        size_i: int,
        is_ask: bool,
    ) -> None:
        """Verify order doesn't create excessive venue concentration."""
        # Get current net position across venues
        net_positions = await self._position_service.get_net_position(canonical_symbol)
        current_venue_position = net_positions.get(venue, 0)
        current_net_position = net_positions.get('net', 0)

        # Calculate new positions
        size_scale = await self._symbol_service.get_size_scale(venue, canonical_symbol)
        order_size = size_i / size_scale
        new_venue_position = current_venue_position + (order_size if not is_ask else -order_size)
        new_net_position = current_net_position + (order_size if not is_ask else -order_size)

        # Check concentration ratio
        if abs(new_net_position) > 0:
            venue_concentration = abs(new_venue_position) / abs(new_net_position)
            if venue_concentration > self._max_venue_concentration:
                raise ValueError(
                    f"Venue concentration limit exceeded: {venue_concentration:.2%} > "
                    f"{self._max_venue_concentration:.2%} for {venue}:{canonical_symbol}"
                )

    def configure_limits(
        self,
        *,
        max_position_ratio: Optional[float] = None,
        min_collateral_buffer: Optional[float] = None,
        max_venue_concentration: Optional[float] = None,
        max_order_size_ratio: Optional[float] = None,
    ) -> None:
        """Configure risk limits."""
        if max_position_ratio is not None:
            self._max_position_ratio = max_position_ratio
        if min_collateral_buffer is not None:
            self._min_collateral_buffer = min_collateral_buffer
        if max_venue_concentration is not None:
            self._max_venue_concentration = max_venue_concentration
        if max_order_size_ratio is not None:
            self._max_order_size_ratio = max_order_size_ratio

        self.log.info(
            "Risk limits configured: position_ratio=%.2%%, collateral_buffer=%.2%%, "
            "venue_concentration=%.2%%, order_size_ratio=%.2%%",
            self._max_position_ratio, self._min_collateral_buffer,
            self._max_venue_concentration, self._max_order_size_ratio
        )


__all__ = ["RiskService"]