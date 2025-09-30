"""Smoke test strategy - short circuit diagnostic tool.

Replaces complex connector_diagnostics with simple, direct testing.
Strategy → Router(OrderService) → tracking_limit.py (ONLY implementation)
Target: < 400 lines.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from ..execution import ExecutionRouter, place_tracking_limit_order, TrackingLimitTimeoutError
from ..execution.order_model import OrderState


class StrategyBase(Protocol):
    """Base protocol for all trading strategies."""
    def start(self, core: Any) -> None: ...
    def stop(self) -> None: ...
    async def on_tick(self, now_ms: float) -> None: ...


@dataclass
class SmokeTestConfig:
    """Configuration for smoke test strategy."""
    venue: str
    symbol: str
    mode: str = "tracking_limit"  # "tracking_limit" | "limit_once" | "market"
    side: str = "buy"  # "buy" | "sell"
    size_multiplier: float = 1.0  # Multiplier for min size
    price_offset_ticks: int = 2  # Offset from top of book
    interval_secs: float = 10.0  # Tracking interval
    timeout_secs: float = 120.0  # Total timeout
    max_attempts: int = 3  # Maximum attempts
    debug: bool = True


@dataclass
class SmokeTestResult:
    """Result from smoke test execution."""
    success: bool
    attempts: int
    duration_secs: float
    events: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    price_samples: int = 0
    price_stats: Optional[Dict[str, Any]] = None
    order_count: int = 0
    timeline_summary: Optional[Dict[str, Any]] = None


class SmokeTestStrategy:
    """Short-circuit smoke test strategy.

    Directly tests Strategy → Router → OrderService → tracking_limit.py
    """

    def __init__(
        self,
        config: SmokeTestConfig,
        router: ExecutionRouter,
        *,
        logger: Optional[logging.Logger] = None
    ):
        self.config = config
        self.router = router
        self.log = logger or logging.getLogger("xbot.strategy.smoke_test")

        # Strategy state
        self.core: Optional[Any] = None
        self.running = False
        self.test_complete = False

        # Test results
        self.result = SmokeTestResult(
            success=False,
            attempts=0,
            duration_secs=0.0
        )

        # Price tracking
        self.price_samples: List[Dict[str, Any]] = []

    def start(self, core: Any) -> None:
        """Start strategy with core reference."""
        self.core = core
        self.running = True
        self.log.info(f"Smoke test started: {self.config.venue}:{self.config.symbol}")

    def stop(self) -> None:
        """Stop strategy."""
        self.running = False
        self.log.info("Smoke test stopped")

    async def on_tick(self, now_ms: float) -> None:
        """Execute smoke test on first tick."""
        if not self.running or self.test_complete:
            return

        self.test_complete = True
        self.log.info("Starting smoke test execution")

        start_time = time.time()

        try:
            if self.config.mode == "tracking_limit":
                await self._test_tracking_limit()
            elif self.config.mode == "limit_once":
                await self._test_limit_once()
            elif self.config.mode == "market":
                await self._test_market()
            else:
                raise ValueError(f"Unknown test mode: {self.config.mode}")

            self.result.success = True
            self.result.events.append("Test completed successfully")

        except Exception as e:
            self.result.success = False
            self.result.errors.append(str(e))
            self.log.error(f"Smoke test failed: {e}")

        finally:
            self.result.duration_secs = time.time() - start_time
            await self._generate_report()

            # Stop the core to end the test
            if self.core:
                asyncio.create_task(self.core.stop())

    async def _test_tracking_limit(self) -> None:
        """Test tracking limit order functionality."""
        self.result.events.append(f"Starting tracking limit test: {self.config.venue}:{self.config.symbol}")

        # Get connector through router
        connector = self.router.get_connector(self.config.venue)
        if not connector:
            raise RuntimeError(f"No connector for venue: {self.config.venue}")

        # Get symbol metadata
        try:
            size_scale = await self.router.size_scale(self.config.venue, self.config.symbol)
            min_size_i = await self.router.min_size_i(self.config.venue, self.config.symbol)
            price_dec, _ = await connector.get_price_size_decimals(self.config.symbol)

            self.result.events.append(f"Symbol metadata: size_scale={size_scale}, min_size_i={min_size_i}")

        except Exception as e:
            self.result.warnings.append(f"Failed to get symbol metadata: {e}")
            size_scale = 1000000  # Default 6 decimals
            min_size_i = 1000
            price_dec = 2

        # Calculate order size
        size_i = max(int(min_size_i * self.config.size_multiplier), 1)
        self.result.events.append(f"Calculated size_i={size_i}")

        # Sample price before placing order
        await self._sample_price()

        # Use tracking limit - THE ONLY IMPLEMENTATION
        try:
            tracking_result = await place_tracking_limit_order(
                connector=connector,
                symbol=self.config.symbol,
                size_i=size_i,
                price_i=0,  # Will be adjusted by price_offset_ticks
                is_ask=self.config.side == "sell",
                interval_secs=self.config.interval_secs,
                timeout_secs=self.config.timeout_secs,
                price_offset_ticks=self.config.price_offset_ticks,
                max_attempts=self.config.max_attempts,
                logger=self.log
            )

            self.result.attempts = tracking_result.attempts
            self.result.order_count = 1

            if tracking_result.order.is_filled():
                self.result.events.append(f"Order filled: {tracking_result.filled_base_i}/{size_i}")
            elif tracking_result.order.is_cancelled():
                self.result.events.append("Order cancelled")
            else:
                self.result.warnings.append(f"Order ended in state: {tracking_result.state}")

            # Get timeline summary
            self.result.timeline_summary = tracking_result.order.get_timeline_summary()

            # Check for race conditions
            race_issues = tracking_result.order.detect_race_conditions()
            if race_issues:
                self.result.warnings.extend(race_issues)

        except TrackingLimitTimeoutError as e:
            self.result.errors.append(f"Tracking limit timeout: {e}")
        except Exception as e:
            self.result.errors.append(f"Tracking limit error: {e}")

    async def _test_limit_once(self) -> None:
        """Test single limit order placement and cancellation."""
        self.result.events.append(f"Starting limit once test: {self.config.venue}:{self.config.symbol}")

        # Get minimum size
        min_size_i = await self.router.min_size_i(self.config.venue, self.config.symbol)
        size_i = max(int(min_size_i * self.config.size_multiplier), 1)
        self.result.events.append(f"Calculated size_i={size_i}")

        # Sample price
        await self._sample_price()

        # Get current price for limit order
        connector = self.router.get_connector(self.config.venue)
        bid_i, ask_i, scale = await connector.get_top_of_book(self.config.symbol)

        if self.config.side == "sell":
            price_i = (ask_i or 0) + self.config.price_offset_ticks
        else:
            price_i = max((bid_i or 0) - self.config.price_offset_ticks, 1)

        self.result.events.append(f"Placing limit order: size_i={size_i}, price_i={price_i}, is_ask={self.config.side == 'sell'}")

        try:
            # Place limit order through router
            order = await self.router.limit_order(
                venue=self.config.venue,
                symbol=self.config.symbol,
                size_i=size_i,
                price_i=price_i,
                is_ask=self.config.side == "sell",
                tracking=False  # Single order, not tracking
            )

            self.result.order_count = 1
            self.result.attempts = 1

            # Wait a bit to see if it fills
            await asyncio.sleep(2.0)

            # Cancel the order
            await self.router.cancel(self.config.venue, self.config.symbol, order['coi'])
            self.result.events.append(f"Cancelled order: coi={order['coi']}")

        except Exception as e:
            self.result.errors.append(f"Limit order error: {e}")

    async def _test_market(self) -> None:
        """Test market order execution."""
        self.result.events.append(f"Starting market test: {self.config.venue}:{self.config.symbol}")

        # Get minimum size
        min_size_i = await self.router.min_size_i(self.config.venue, self.config.symbol)
        size_i = max(int(min_size_i * self.config.size_multiplier), 1)
        self.result.events.append(f"Calculated size_i={size_i}")

        # Sample price
        await self._sample_price()

        self.result.events.append(f"Placing market order: size_i={size_i}, is_ask={self.config.side == 'sell'}")

        try:
            # Place market order through router
            order = await self.router.market_order(
                venue=self.config.venue,
                symbol=self.config.symbol,
                size_i=size_i,
                is_ask=self.config.side == "sell"
            )

            self.result.order_count = 1
            self.result.attempts = 1

            # Market orders should fill quickly
            await asyncio.sleep(0.5)

            if hasattr(order, 'state') and order.state == "FILLED":
                self.result.events.append("Market order filled")
            else:
                self.result.warnings.append(f"Market order state: {getattr(order, 'state', 'unknown')}")

        except Exception as e:
            self.result.errors.append(f"Market order error: {e}")

    async def _sample_price(self) -> None:
        """Sample current price for analysis."""
        try:
            connector = self.router.get_connector(self.config.venue)
            bid_i, ask_i, scale = await connector.get_top_of_book(self.config.symbol)

            if bid_i is not None and ask_i is not None:
                spread_i = ask_i - bid_i
                spread_bps = (spread_i / bid_i) * 10000 if bid_i > 0 else 0

                sample = {
                    "timestamp": time.time(),
                    "bid_i": bid_i,
                    "ask_i": ask_i,
                    "spread_i": spread_i,
                    "spread_bps": spread_bps,
                    "scale": scale
                }

                self.price_samples.append(sample)
                self.result.price_samples += 1

                self.log.debug(f"Price sample: bid={bid_i/scale:.6f}, ask={ask_i/scale:.6f}, spread={spread_bps:.2f}bps")

        except Exception as e:
            self.result.warnings.append(f"Price sampling failed: {e}")

    async def _generate_report(self) -> None:
        """Generate final test report."""
        # Calculate price statistics
        if self.price_samples:
            spreads = [s["spread_bps"] for s in self.price_samples]
            self.result.price_stats = {
                "samples": len(spreads),
                "avg_spread_bps": sum(spreads) / len(spreads),
                "min_spread_bps": min(spreads),
                "max_spread_bps": max(spreads)
            }

        # Log comprehensive results
        self.log.info("=== SMOKE TEST RESULTS ===")
        self.log.info(f"Venue: {self.config.venue}")
        self.log.info(f"Symbol: {self.config.symbol}")
        self.log.info(f"Mode: {self.config.mode}")
        self.log.info(f"Success: {self.result.success}")
        self.log.info(f"Duration: {self.result.duration_secs:.3f}s")
        self.log.info(f"Attempts: {self.result.attempts}")
        self.log.info(f"Orders: {self.result.order_count}")

        if self.result.events:
            self.log.info("Events:")
            for event in self.result.events:
                self.log.info(f"  - {event}")

        if self.result.warnings:
            self.log.warning("Warnings:")
            for warning in self.result.warnings:
                self.log.warning(f"  - {warning}")

        if self.result.errors:
            self.log.error("Errors:")
            for error in self.result.errors:
                self.log.error(f"  - {error}")

        if self.result.price_stats:
            stats = self.result.price_stats
            self.log.info(f"Price Stats: samples={stats['samples']}, "
                         f"avg_spread={stats['avg_spread_bps']:.4f}bps, "
                         f"range=[{stats['min_spread_bps']:.4f}, {stats['max_spread_bps']:.4f}]bps")

        if self.result.timeline_summary:
            timeline = self.result.timeline_summary
            self.log.info(f"Timeline: events={timeline['total_events']}, "
                         f"duration={timeline['duration_secs']:.3f}s")

        self.log.info("=== END SMOKE TEST ===")

    @property
    def overall_success(self) -> bool:
        """Get overall test success status."""
        return self.result.success

    @property
    def failure_reason(self) -> Optional[str]:
        """Get failure reason if test failed."""
        if self.result.success:
            return None
        return "; ".join(self.result.errors) if self.result.errors else "Unknown failure"