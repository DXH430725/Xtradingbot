"""Connector diagnostics - simplified smoke test template for new strategies.

Three core methods: place_limit/market/cancel with enhanced observability.
Direct tracking_limit.place_tracking_limit_order calls for limit orders.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from mm_bot.execution import resolve_filled_amount
from mm_bot.execution.order import Order, OrderState
from mm_bot.execution.router import ExecutionRouter
from mm_bot.execution.tracking_limit import place_tracking_limit_order
from mm_bot.strategy.strategy_base import StrategyBase


@dataclass
class DiagnosticTask:
    """Single diagnostic task configuration."""
    venue: str
    symbol: str
    mode: Literal["limit_once", "tracking_limit", "market"] = "tracking_limit"
    side: Literal["buy", "sell"] = "buy"
    min_multiplier: float = 1.0
    price_offset_ticks: int = 0
    tracking_interval_secs: float = 10.0
    tracking_timeout_secs: float = 120.0
    cancel_wait_secs: float = 2.0
    reduce_only_probe: bool = False


@dataclass
class DiagnosticParams:
    """Diagnostic strategy parameters."""
    tasks: List[DiagnosticTask] = field(default_factory=list)
    pause_between_tests_secs: float = 2.0
    test_mode: bool = False


@dataclass
class PriceTimePoint:
    """Price observation at specific time."""
    timestamp: float
    bid_i: Optional[int]
    ask_i: Optional[int]
    scale: int
    spread_bps: Optional[float] = None


@dataclass
class DiagnosticReport:
    """Enhanced diagnostic report with observability data."""
    venue: str
    symbol: str
    mode: str
    side: str
    success: Optional[bool] = None

    # Order tracking
    attempts: int = 0
    orders: List[Order] = field(default_factory=list)

    # Price timeline
    price_timeline: List[PriceTimePoint] = field(default_factory=list)

    # Timing analysis
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None

    # Diagnostic data
    events: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class ConnectorDiagnostics(StrategyBase):
    """Diagnostic strategy template for connector validation and debugging.

    Designed as blueprint for new strategies with:
    - Short execution paths (3 core methods only)
    - Enhanced observability
    - Timeline tracking for arbitrage window analysis
    """

    def __init__(self, connectors: Dict[str, Any], params: DiagnosticParams) -> None:
        if not connectors:
            raise ValueError("diagnostics requires at least one connector")

        self.log = logging.getLogger("mm_bot.strategy.diagnostics")
        self._connectors = connectors
        self.params = params

        # Use new ExecutionRouter instead of ExecutionLayer
        self.execution = ExecutionRouter(logger=self.log)
        for name, connector in connectors.items():
            self.execution.register_connector(name, connector)

        # State management
        self._started = False
        self._task: Optional[asyncio.Task] = None
        self._active = True

        # Enhanced reporting
        self._reports: List[DiagnosticReport] = []
        self._log_dir = Path("logs/diagnostics")
        self._log_dir.mkdir(parents=True, exist_ok=True)

    def start(self, core: Any) -> None:
        if self._started:
            return
        self._active = True
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._run(), name="diagnostics.run")
        self._started = True

    def stop(self) -> None:
        self._active = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._started = False

    async def on_tick(self, now_ms: float) -> None:
        await asyncio.sleep(0)

    async def _run(self) -> None:
        """Main diagnostic execution loop."""
        try:
            await self._prepare()

            for idx, task in enumerate(self.params.tasks):
                if not self._active:
                    break

                report = DiagnosticReport(
                    venue=task.venue,
                    symbol=task.symbol,
                    mode=task.mode,
                    side=task.side,
                )
                self._reports.append(report)

                await self._run_diagnostic_task(idx, task, report)
                await asyncio.sleep(self.params.pause_between_tests_secs)

        except asyncio.CancelledError:
            raise
        except Exception:
            self.log.exception("Diagnostics crashed")
        finally:
            await self._emergency_cleanup()
            await self._write_final_report()

    async def _prepare(self) -> None:
        """Prepare connectors and symbol mappings."""
        for idx, task in enumerate(self.params.tasks):
            venue = task.venue.lower()
            connector = self._connectors.get(venue)
            if connector is None:
                raise ValueError(f"Connector '{venue}' not supplied")

            # Map canonical symbol to venue-specific format
            venue_symbol = connector.map_symbol(task.symbol)

            # Start WebSocket state if available
            if hasattr(connector, "start_ws_state"):
                try:
                    result = connector.start_ws_state([venue_symbol])
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    self.log.warning("Failed to start WS state for %s: %s", venue, e)

            # Register symbol mapping
            canonical = f"DIAG:{idx}"
            self.execution.register_symbol(canonical, **{venue: venue_symbol})

    async def _run_diagnostic_task(self, idx: int, task: DiagnosticTask, report: DiagnosticReport) -> None:
        """Run single diagnostic task with enhanced observability."""
        venue = task.venue.lower()
        canonical = f"DIAG:{idx}"

        # Start price timeline monitoring
        price_monitor_task = asyncio.create_task(
            self._monitor_prices(venue, canonical, report),
            name=f"price_monitor_{idx}"
        )

        try:
            self.log.info(
                "Starting diagnostic %s: venue=%s symbol=%s mode=%s side=%s",
                idx, venue, task.symbol, task.mode, task.side
            )

            # Determine order size
            size_i = await self._determine_size_i(venue, canonical, task)
            if size_i <= 0:
                report.errors.append(f"Unable to compute size for {venue}:{task.symbol}")
                return

            report.events.append(f"Calculated size_i={size_i}")

            # Execute based on mode
            if task.mode == "limit_once":
                success = await self._run_limit_once(venue, canonical, task, size_i, report)
            elif task.mode == "tracking_limit":
                success = await self._run_tracking_limit(venue, canonical, task, size_i, report)
            elif task.mode == "market":
                success = await self._run_market_roundtrip(venue, canonical, task, size_i, report)
            else:
                raise ValueError(f"Unknown mode: {task.mode}")

            report.success = success
            report.end_time = time.time()

            if success:
                self.log.info("Diagnostic %s completed successfully", idx)
            else:
                self.log.warning("Diagnostic %s finished with errors", idx)

        except Exception as e:
            report.errors.append(f"Task failed: {e}")
            report.success = False
            self.log.error("Diagnostic %s failed: %s", idx, e, exc_info=True)
        finally:
            price_monitor_task.cancel()
            try:
                await price_monitor_task
            except asyncio.CancelledError:
                pass

    async def _run_limit_once(self, venue: str, canonical: str, task: DiagnosticTask, size_i: int, report: DiagnosticReport) -> bool:
        """Run single limit order test - place, wait, cancel if needed."""
        is_ask = task.side.lower() in {"sell", "short"}

        # Get connector and lock
        connector = self._connectors[venue.lower()]
        venue_symbol = connector.map_symbol(task.symbol)

        # ENFORCED: Only use tracking_limit.place_tracking_limit_order
        try:
            report.attempts += 1
            report.events.append(f"Placing limit order: size_i={size_i}, is_ask={is_ask}")

            order = await place_tracking_limit_order(
                connector,
                symbol=venue_symbol,
                base_amount_i=size_i,
                is_ask=is_ask,
                interval_secs=task.tracking_interval_secs,
                timeout_secs=30.0,  # Shorter timeout for limit_once
                price_offset_ticks=task.price_offset_ticks,
                cancel_wait_secs=task.cancel_wait_secs,
                coi_manager=self.execution._order_service.coi_manager,
                lock=await self.execution._order_service._get_lock(venue),
                logger=self.log,
            )

            if order and hasattr(order, '_tracker'):
                # Convert to new Order format for reporting
                new_order = self._convert_tracker_to_order(order._tracker, venue, canonical)
                report.orders.append(new_order)

                final_state = new_order.state
                report.events.append(f"Limit order final state: {final_state.value}")

                return final_state == OrderState.FILLED
            else:
                report.errors.append("Failed to place limit order")
                return False

        except Exception as e:
            report.errors.append(f"Limit order error: {e}")
            return False

    async def _run_tracking_limit(self, venue: str, canonical: str, task: DiagnosticTask, size_i: int, report: DiagnosticReport) -> bool:
        """Run tracking limit order test with exit position."""
        is_ask = task.side.lower() in {"sell", "short"}

        # Entry order via tracking limit
        try:
            report.attempts += 1
            report.events.append(f"Placing tracking limit entry: size_i={size_i}, is_ask={is_ask}")

            # ENFORCED: Use tracking_limit.place_tracking_limit_order
            tracker = await self.execution.limit_order(
                venue,
                canonical,
                base_amount_i=size_i,
                is_ask=is_ask,
                interval_secs=task.tracking_interval_secs,
                timeout_secs=task.tracking_timeout_secs,
                price_offset_ticks=task.price_offset_ticks,
                cancel_wait_secs=task.cancel_wait_secs,
                reduce_only=0,
            )

            if tracker and hasattr(tracker, '_tracker'):
                entry_order = self._convert_tracker_to_order(tracker._tracker, venue, canonical)
                report.orders.append(entry_order)

                if entry_order.state != OrderState.FILLED:
                    report.errors.append(f"Entry not filled: {entry_order.state.value}")
                    return False

                # Calculate exit size
                scale = await self.execution.size_scale(venue, canonical)
                filled_i = resolve_filled_amount(tracker, size_scale=scale, fallback=size_i)

                # Exit order via market
                report.events.append(f"Placing market exit: size_i={filled_i}, is_ask={not is_ask}")
                exit_tracker = await self.execution.market_order(
                    venue,
                    canonical,
                    size_i=filled_i,
                    is_ask=not is_ask,
                    reduce_only=1,
                    wait_timeout=30.0,
                )

                if exit_tracker and hasattr(exit_tracker, '_tracker'):
                    exit_order = self._convert_tracker_to_order(exit_tracker._tracker, venue, canonical)
                    report.orders.append(exit_order)

                    success = exit_order.state == OrderState.FILLED
                    if not success:
                        report.errors.append(f"Exit failed: {exit_order.state.value}")

                    return success
                else:
                    report.errors.append("Exit order failed to create")
                    return False
            else:
                report.errors.append("Entry order failed to create")
                return False

        except Exception as e:
            report.errors.append(f"Tracking limit error: {e}")
            return False

    async def _run_market_roundtrip(self, venue: str, canonical: str, task: DiagnosticTask, size_i: int, report: DiagnosticReport) -> bool:
        """Run market order roundtrip test."""
        is_ask = task.side.lower() in {"sell", "short"}

        try:
            # Entry market order
            report.attempts += 1
            report.events.append(f"Placing market entry: size_i={size_i}, is_ask={is_ask}")

            entry_tracker = await self.execution.market_order(
                venue,
                canonical,
                size_i=size_i,
                is_ask=is_ask,
                reduce_only=0,
                wait_timeout=30.0,
            )

            if entry_tracker and hasattr(entry_tracker, '_tracker'):
                entry_order = self._convert_tracker_to_order(entry_tracker._tracker, venue, canonical)
                report.orders.append(entry_order)

                if entry_order.state != OrderState.FILLED:
                    report.errors.append(f"Market entry failed: {entry_order.state.value}")
                    return False

                # Exit market order
                report.events.append(f"Placing market exit: size_i={size_i}, is_ask={not is_ask}")
                exit_tracker = await self.execution.market_order(
                    venue,
                    canonical,
                    size_i=size_i,
                    is_ask=not is_ask,
                    reduce_only=1,
                    wait_timeout=30.0,
                )

                if exit_tracker and hasattr(exit_tracker, '_tracker'):
                    exit_order = self._convert_tracker_to_order(exit_tracker._tracker, venue, canonical)
                    report.orders.append(exit_order)

                    success = exit_order.state == OrderState.FILLED
                    if not success:
                        report.errors.append(f"Market exit failed: {exit_order.state.value}")

                    return success
                else:
                    report.errors.append("Exit order failed to create")
                    return False
            else:
                report.errors.append("Entry order failed to create")
                return False

        except Exception as e:
            report.errors.append(f"Market roundtrip error: {e}")
            return False

    async def _monitor_prices(self, venue: str, canonical: str, report: DiagnosticReport) -> None:
        """Monitor price timeline during test execution."""
        connector = self._connectors.get(venue.lower())
        if not connector or not hasattr(connector, 'get_top_of_book'):
            return

        venue_symbol = connector.map_symbol(report.symbol)

        try:
            while True:
                try:
                    bid_i, ask_i, scale = await connector.get_top_of_book(venue_symbol)

                    spread_bps = None
                    if bid_i and ask_i and bid_i > 0:
                        spread_bps = ((ask_i - bid_i) / bid_i) * 10000

                    point = PriceTimePoint(
                        timestamp=time.time(),
                        bid_i=bid_i,
                        ask_i=ask_i,
                        scale=scale,
                        spread_bps=spread_bps,
                    )
                    report.price_timeline.append(point)

                    # Keep only last 100 points to avoid memory issues
                    if len(report.price_timeline) > 100:
                        report.price_timeline = report.price_timeline[-100:]

                except Exception as e:
                    # Don't spam logs with price fetch errors
                    if len(report.price_timeline) == 0:  # Log only first error
                        report.warnings.append(f"Price monitoring error: {e}")

                await asyncio.sleep(1.0)  # Sample every second

        except asyncio.CancelledError:
            pass

    async def _determine_size_i(self, venue: str, canonical: str, task: DiagnosticTask) -> int:
        """Determine appropriate order size."""
        if self.params.test_mode:
            # Use minimum size in test mode
            return await self.execution.min_size_i(venue, canonical)

        min_size = await self.execution.min_size_i(venue, canonical)
        multiplier = max(task.min_multiplier, 1.0)
        size = int(round(min_size * multiplier))
        return max(size, min_size)

    def _convert_tracker_to_order(self, tracker: Any, venue: str, canonical: str) -> Order:
        """Convert legacy tracker to new Order format."""
        return Order(
            id=f"{venue}_{tracker.client_order_id}",
            venue=venue,
            symbol=canonical,
            side="sell" if getattr(tracker, 'is_ask', False) else "buy",
            state=tracker.state if hasattr(tracker, 'state') else OrderState.NEW,
            client_order_id=tracker.client_order_id,
            exchange_order_id=getattr(tracker, 'exchange_order_id', None),
        )

    async def _emergency_cleanup(self) -> None:
        """Emergency cleanup of any remaining positions."""
        for report in self._reports:
            canonical = f"DIAG:{self._reports.index(report)}"
            try:
                await self.execution.unwind_all(
                    canonical_symbol=canonical,
                    tolerance=1e-8,
                    venues=[report.venue],
                )
            except Exception as e:
                self.log.error("Emergency cleanup failed for %s: %s", canonical, e)

    async def _write_final_report(self) -> None:
        """Write comprehensive diagnostic report."""
        summary = {
            "test_run_time": time.time(),
            "total_tasks": len(self.params.tasks),
            "successful_tasks": sum(1 for r in self._reports if r.success),
            "failed_tasks": sum(1 for r in self._reports if r.success is False),
            "reports": []
        }

        for report in self._reports:
            report_data = {
                "venue": report.venue,
                "symbol": report.symbol,
                "mode": report.mode,
                "side": report.side,
                "success": report.success,
                "attempts": report.attempts,
                "duration_secs": (report.end_time or time.time()) - report.start_time,
                "events": report.events,
                "errors": report.errors,
                "warnings": report.warnings,
                "order_count": len(report.orders),
                "price_samples": len(report.price_timeline),
            }

            # Add order timeline summaries
            if report.orders:
                report_data["orders"] = [
                    {
                        "id": order.id,
                        "state": order.state.value,
                        "side": order.side,
                        "timeline_summary": order.get_timeline_summary(),
                        "race_conditions": order.detect_race_conditions(),
                    }
                    for order in report.orders
                ]

            # Add price statistics
            if report.price_timeline:
                spreads = [p.spread_bps for p in report.price_timeline if p.spread_bps]
                if spreads:
                    report_data["price_stats"] = {
                        "avg_spread_bps": sum(spreads) / len(spreads),
                        "min_spread_bps": min(spreads),
                        "max_spread_bps": max(spreads),
                    }

            summary["reports"].append(report_data)

        # Write to file
        report_path = self._log_dir / f"diagnostics_{int(time.time())}.json"
        report_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        self.log.info("Diagnostic report written to %s", report_path)


__all__ = [
    "ConnectorDiagnostics",
    "DiagnosticParams",
    "DiagnosticTask",
    "DiagnosticReport",
]