from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from mm_bot.execution import ExecutionLayer, resolve_filled_amount
from mm_bot.execution.orders import OrderState
from mm_bot.strategy.strategy_base import StrategyBase


@dataclass
class ConnectorTestTask:
    venue: str
    symbol: str
    order_type: str = "market"  # "market" or "tracking_limit"
    side: str = "buy"
    min_multiplier: float = 1.0
    price_offset_ticks: int = 0
    tracking_interval_secs: float = 10.0
    tracking_timeout_secs: float = 120.0
    cancel_wait_secs: float = 2.0


@dataclass
class ConnectorTestParams:
    tasks: List[ConnectorTestTask] = field(default_factory=list)
    pause_between_tests_secs: float = 2.0
    test_mode: bool = False


class ConnectorTestStrategy(StrategyBase):
    """Generic connector exercise that opens then closes a minimal exposure."""

    def __init__(self, connectors: Dict[str, Any], params: ConnectorTestParams) -> None:
        if not connectors:
            raise ValueError("connector_test requires at least one connector; check strategy tasks and config")
        self.log = logging.getLogger("mm_bot.strategy.connector_test")
        self._connectors = connectors
        self.params = params
        self.execution = ExecutionLayer(logger=self.log)
        for name, connector in connectors.items():
            self.execution.register_connector(name, connector)
        self._started = False
        self._task: Optional[asyncio.Task] = None
        self._active = True
        self._test_mode_sizes: Dict[str, int] = {}
        self._canonicals: List[str] = []
        self._canonical_venues: Dict[str, List[str]] = {}
        self._report: Dict[int, Dict[str, Any]] = {}
        self._log_dir = Path("logs/connector_test")
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._report: Dict[int, Dict[str, Any]] = {}
        self._log_dir = Path("logs/connector_test")
        self._log_dir.mkdir(parents=True, exist_ok=True)

    def start(self, core: Any) -> None:
        if self._started:
            return
        self._active = True
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._run(), name="connector_test.run")
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
        try:
            await self._prepare()
            for idx, task in enumerate(self.params.tasks):
                if not self._active:
                    break
                await self._run_task(idx, task)
                await asyncio.sleep(self.params.pause_between_tests_secs)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.log.exception("connector test crashed")
        finally:
            for canonical in self._canonicals:
                await self.execution.unwind_all(
                    canonical_symbol=canonical,
                    tolerance=1e-8,
                    venues=self._canonical_venues.get(canonical),
                )

    async def _prepare(self) -> None:
        symbols_by_task: Dict[str, Dict[str, str]] = {}
        for idx, task in enumerate(self.params.tasks):
            venue = task.venue.lower()
            connector = self._connectors.get(venue)
            if connector is None:
                raise ValueError(f"connector '{venue}' not supplied")
            if hasattr(connector, "start_ws_state"):
                start_ws = connector.start_ws_state
                try:
                    result = start_ws([task.symbol])
                except TypeError:
                    result = start_ws()
                if asyncio.iscoroutine(result):
                    await result
            canonical = f"TEST:{idx}"
            symbols_by_task[canonical] = {venue: task.symbol}
            self._canonicals.append(canonical)
            self._canonical_venues[canonical] = [venue]
            self._report[idx] = {
                "venue": venue,
                "symbol": task.symbol,
                "order_type": task.order_type,
                "side": task.side,
                "success": None,
                "events": [],
                "errors": [],
                "order_events": [],
            }
        for canonical, mapping in symbols_by_task.items():
            self.execution.register_symbol(canonical, **mapping)
        if self.params.test_mode:
            await self._compute_test_mode_sizes()

    async def _compute_test_mode_sizes(self) -> None:
        for canonical, venues in self._canonical_venues.items():
            for venue in venues:
                min_size = await self.execution.min_size_i(venue, canonical)
                current = self._test_mode_sizes.get(venue, 0)
                self._test_mode_sizes[venue] = max(current, min_size)

    async def _run_task(self, idx: int, task: ConnectorTestTask) -> None:
        venue = task.venue.lower()
        canonical = f"TEST:{idx}"
        symbol = task.symbol
        size_i = await self._determine_size_i(venue, canonical, task)
        if size_i <= 0:
            self.log.error("task %s unable to compute size", venue)
            return
        self.log.info(
            "connector_test task=%s venue=%s symbol=%s order=%s size_i=%s",
            idx,
            venue,
            symbol,
            task.order_type,
            size_i,
        )
        if task.order_type.lower() == "tracking_limit":
            success = await self._run_tracking_limit(idx, venue, canonical, task, size_i)
        else:
            success = await self._run_market_roundtrip(idx, venue, canonical, task, size_i)
        if success:
            self.log.info("task %s completed successfully", idx)
        else:
            self.log.warning("task %s finished with errors", idx)
        self._report[idx]["success"] = success
        await self._write_task_report(idx)

    async def _run_tracking_limit(self, idx: int, venue: str, canonical: str, task: ConnectorTestTask, size_i: int) -> bool:
        is_ask = task.side.lower() in {"sell", "short"}
        tracker = await self.execution.limit_order(
            venue,
            canonical,
            base_amount_i=size_i,
            is_ask=is_ask,
            interval_secs=max(task.tracking_interval_secs, 1.0),
            timeout_secs=max(task.tracking_timeout_secs, 10.0),
            price_offset_ticks=task.price_offset_ticks,
            cancel_wait_secs=max(task.cancel_wait_secs, 0.5),
            reduce_only=0,
        )
        self._record_tracker_history(idx, canonical, venue, "limit_entry", tracker)
        if tracker.state != OrderState.FILLED:
            self._record_error(idx, f"{venue} limit not filled state={tracker.state.value}")
            await self.execution.unwind_all(canonical_symbol=canonical, tolerance=1e-8, venues=self._canonical_venues.get(canonical))
            return False
        scale = await self.execution.size_scale(venue, canonical)
        filled_i = resolve_filled_amount(tracker, size_scale=scale, fallback=size_i)
        market_tracker = await self.execution.market_order(
            venue,
            canonical,
            size_i=filled_i,
            is_ask=not is_ask,
            reduce_only=1,
            wait_timeout=30.0,
        )
        if market_tracker:
            self._record_tracker_history(idx, canonical, venue, "market_exit", market_tracker)
        success = bool(market_tracker and market_tracker.state == OrderState.FILLED)
        if not success:
            self._record_error(idx, f"{venue} market exit failed state={market_tracker.state if market_tracker else 'none'}")
            await self.execution.unwind_all(canonical_symbol=canonical, tolerance=1e-8, venues=self._canonical_venues.get(canonical))
            return False
        if task.reduce_only_probe:
            await self._run_reduce_only_probe(idx, venue, canonical, filled_i, is_ask)
        await self._rest_ws_consistency_check(idx, venue, canonical)
        return True

    async def _run_market_roundtrip(self, idx: int, venue: str, canonical: str, task: ConnectorTestTask, size_i: int) -> bool:
        is_ask = task.side.lower() in {"sell", "short"}
        tracker = await self.execution.market_order(
            venue,
            canonical,
            size_i=size_i,
            is_ask=is_ask,
            reduce_only=0,
            wait_timeout=30.0,
        )
        if tracker:
            self._record_tracker_history(idx, canonical, venue, "market_entry", tracker)
        if tracker is None or tracker.state != OrderState.FILLED:
            self._record_error(idx, f"{venue} market entry failed state={tracker.state if tracker else 'none'}")
            await self.execution.unwind_all(canonical_symbol=canonical, tolerance=1e-8, venues=self._canonical_venues.get(canonical))
            return False
        tracker_exit = await self.execution.market_order(
            venue,
            canonical,
            size_i=size_i,
            is_ask=not is_ask,
            reduce_only=1,
            wait_timeout=30.0,
        )
        if tracker_exit:
            self._record_tracker_history(idx, canonical, venue, "market_exit", tracker_exit)
        success = bool(tracker_exit and tracker_exit.state == OrderState.FILLED)
        if not success:
            self._record_error(idx, f"{venue} market exit failed state={tracker_exit.state if tracker_exit else 'none'}")
            await self.execution.unwind_all(canonical_symbol=canonical, tolerance=1e-8, venues=self._canonical_venues.get(canonical))
            return False
        if task.reduce_only_probe:
            await self._run_reduce_only_probe(idx, venue, canonical, size_i, is_ask)
        await self._rest_ws_consistency_check(idx, venue, canonical)
        return True

    async def _determine_size_i(self, venue: str, canonical: str, task: ConnectorTestTask) -> int:
        if self.params.test_mode:
            base = self._test_mode_sizes.get(venue)
            if base:
                return int(base)
        min_size = await self.execution.min_size_i(venue, canonical)
        multiplier = max(task.min_multiplier, 1.0)
        size = int(round(min_size * multiplier))
        return max(size, min_size)

    async def _run_reduce_only_probe(self, idx: int, venue: str, canonical: str, size_i: int, entry_is_ask: bool) -> None:
        tracker = await self.execution.market_order(
            venue,
            canonical,
            size_i=size_i,
            is_ask=entry_is_ask,
            reduce_only=1,
            wait_timeout=30.0,
        )
        if tracker:
            self._record_tracker_history(idx, canonical, venue, "reduce_only_probe", tracker)
        if tracker and tracker.state == OrderState.FILLED:
            self._record_error(idx, f"{venue} reduce-only probe unexpectedly filled")
            await self.execution.unwind_all(canonical_symbol=canonical, tolerance=1e-8, venues=self._canonical_venues.get(canonical))
        else:
            state = tracker.state.value if tracker else "none"
            self._record_info(idx, f"{venue} reduce-only probe state={state}")

    async def _rest_ws_consistency_check(self, idx: int, venue: str, canonical: str) -> None:
        connector = self._connectors.get(venue)
        if connector is None:
            return
        try:
            rest_positions = await connector.get_positions()
        except Exception as exc:
            self._record_error(idx, f"{venue} REST get_positions error: {exc}")
            return
        exec_pos = await self.execution.position(venue, canonical)
        snapshot = {
            "rest": rest_positions,
            "execution": exec_pos,
        }
        self._record_info(idx, f"{venue} position snapshot {snapshot}")

    def _record_tracker_history(self, idx: int, canonical: str, venue: str, label: str, tracker) -> None:
        history = []
        inner = getattr(tracker, "_tracker", None)
        if inner is not None:
            for update in inner.history:
                history.append(
                    {
                        "state": update.state.value,
                        "filled_base": update.filled_base,
                        "info": update.info,
                    }
                )
        self._report[idx].setdefault("order_events", []).append(
            {
                "canonical": canonical,
                "venue": venue,
                "label": label,
                "final_state": tracker.state.value if tracker else "unknown",
                "history": history,
            }
        )

    def _record_error(self, idx: int, message: str) -> None:
        self._report.setdefault(idx, {}).setdefault("errors", []).append(message)
        self.log.error(message)

    def _record_info(self, idx: int, message: str) -> None:
        self._report.setdefault(idx, {}).setdefault("events", []).append(message)
        self.log.info(message)

    async def _write_task_report(self, idx: int) -> None:
        data = self._report.get(idx)
        if not data:
            return
        path = self._log_dir / f"connector_test_task_{idx}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


__all__ = [
    "ConnectorTestStrategy",
    "ConnectorTestParams",
    "ConnectorTestTask",
]
