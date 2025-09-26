import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from mm_bot.execution import TrackingLimitTimeoutError, place_tracking_limit_order
from mm_bot.execution.orders import OrderState, TrackingLimitOrder, TrackingMarketOrder, OrderUpdate
from mm_bot.strategy.strategy_base import StrategyBase


POSITION_TOLERANCE = 1e-8
MAX_FLATTEN_CYCLES = 3


@dataclass
class ConnectorTestConfig:
    symbol: str
    side: str = "buy"
    tracking_timeout_secs: float = 120.0
    tracking_interval_secs: float = 10.0
    settle_timeout_secs: float = 10.0
    market_timeout_secs: float = 30.0
    price_offset_ticks: int = 0
    cancel_wait_secs: float = 2.0


@dataclass
class SmokeTestParams:
    connectors: Dict[str, ConnectorTestConfig] = field(default_factory=dict)
    pause_between_tests_secs: float = 2.0


class ConnectorSmokeTestStrategy(StrategyBase):
    """Exercise connectors with a tracking-limit round trip plus market exit."""

    def __init__(self, connectors: Dict[str, Any], params: SmokeTestParams) -> None:
        self.log = logging.getLogger("mm_bot.strategy.smoke_test")
        self._connectors = connectors
        self.params = params
        self._core: Optional[Any] = None
        self._tasks: Dict[str, asyncio.Task] = {}
        self._started = False
        self._coi_seed = int(time.time() * 1000) % 1_000_000 or 1
        self._results: Dict[str, bool] = {}
        self._errors: Dict[str, str] = {}
        self._monitor_task: Optional[asyncio.Task] = None
        self._active_names: List[str] = []
        self._finalized = False
        self._overall_success: Optional[bool] = None
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------
    @property
    def overall_success(self) -> Optional[bool]:
        return self._overall_success

    @property
    def failure_reason(self) -> Optional[str]:
        return self._last_error

    # ------------------------------------------------------------------
    def start(self, core: Any) -> None:
        self._core = core
        if self._started:
            return
        self._started = True
        loop = asyncio.get_event_loop()
        self._tasks.clear()
        self._results.clear()
        self._errors.clear()
        self._active_names = []
        for name, cfg in self.params.connectors.items():
            connector = self._connectors.get(name)
            if connector is None:
                self.log.error("connector '%s' not available; skipping", name)
                self._errors[name] = "connector unavailable"
                self._results[name] = False
                continue
            self._active_names.append(name)
            task = loop.create_task(self._run_test(name, connector, cfg), name=f"smoke_test[{name}]")
            self._tasks[name] = task
        if not self._active_names:
            loop.create_task(self._finalize(False))
            return
        self._monitor_task = loop.create_task(self._monitor_tests(), name="smoke_test.monitor")

    def stop(self) -> None:
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = None
        self._started = False

    async def on_tick(self, now_ms: float):
        await asyncio.sleep(0)

    # ------------------------------------------------------------------
    async def _run_test(self, name: str, connector: Any, cfg: ConnectorTestConfig) -> bool:
        try:
            return await self._exercise_connector(name, connector, cfg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_failure(name, f"unexpected error: {exc}")
            self.log.exception("smoke test for connector '%s' raised", name)
            return False

    def _record_success(self, name: str) -> None:
        self._results[name] = True
        if name in self._errors:
            self._errors.pop(name, None)
        self.log.info("%s smoke test passed", name)

    def _record_failure(self, name: str, reason: str) -> None:
        existing = self._errors.get(name)
        if existing and reason not in existing:
            self._errors[name] = f"{existing}; {reason}"
        else:
            self._errors[name] = reason
        self._results[name] = False
        self.log.error("%s smoke test failure: %s", name, reason)

    async def _exercise_connector(self, name: str, connector: Any, cfg: ConnectorTestConfig) -> bool:
        base_amount_i, price_dec, size_dec = await self._resolve_order_size(connector, cfg)
        side = cfg.side.lower()
        is_primary_sell = side in {"sell", "short"}

        try:
            limit_tracker = await place_tracking_limit_order(
                connector,
                symbol=cfg.symbol,
                base_amount_i=base_amount_i,
                is_ask=is_primary_sell,
                interval_secs=max(cfg.tracking_interval_secs, 1.0),
                timeout_secs=max(cfg.tracking_timeout_secs, 1.0),
                price_offset_ticks=cfg.price_offset_ticks,
                cancel_wait_secs=max(cfg.cancel_wait_secs, 0.5),
                post_only=False,
                reduce_only=0,
                logger=self.log,
            )
        except TrackingLimitTimeoutError:
            self._record_failure(name, "tracking limit timeout reached")
            return False
        except Exception as exc:
            self._record_failure(name, f"tracking limit error: {exc}")
            return False

        self._log_order_history(name, "limit", limit_tracker)
        final_state = limit_tracker.state
        if final_state not in {OrderState.FILLED, OrderState.PARTIALLY_FILLED}:
            summary = self._summarize_info(limit_tracker.snapshot().info)
            detail = f"limit state={final_state.value}"
            if summary:
                detail = f"{detail} ({summary})"
            self._record_failure(name, detail)
            return False

        filled_i = self._filled_amount_i(limit_tracker, size_dec, fallback=base_amount_i)
        if filled_i <= 0:
            self._record_failure(name, "unable to determine filled quantity")
            return False

        await asyncio.sleep(max(cfg.settle_timeout_secs, 0.0))

        market_tracker = await connector.submit_market_order(
            symbol=cfg.symbol,
            client_order_index=self._next_coi(),
            base_amount=filled_i,
            is_ask=not is_primary_sell,
            reduce_only=1,
        )
        try:
            await market_tracker.wait_final(timeout=max(cfg.market_timeout_secs, 1.0))
        except asyncio.TimeoutError:
            self._record_failure(name, "market close timeout")
            return False

        self._log_order_history(name, "market", market_tracker)
        market_state = market_tracker.state
        if market_state not in {OrderState.FILLED, OrderState.CANCELLED}:
            summary = self._summarize_info(market_tracker.snapshot().info)
            detail = f"market state={market_state.value}"
            if summary:
                detail = f"{detail} ({summary})"
            self._record_failure(name, detail)
            return False

        await self._verify_connector_health(name, connector, cfg.symbol)
        await asyncio.sleep(self.params.pause_between_tests_secs)
        self._record_success(name)
        return True

    async def _monitor_tests(self) -> None:
        if not self._tasks:
            await self._finalize(False)
            return
        names = list(self._tasks.keys())
        results = await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        for name, result in zip(names, results):
            if isinstance(result, Exception):
                self._record_failure(name, f"task crashed: {result}")
        overall = self._compute_overall_success()
        await self._finalize(overall)

    def _compute_overall_success(self) -> bool:
        if not self._active_names:
            return False
        return all(self._results.get(name, False) for name in self._active_names)

    async def _finalize(self, success_hint: bool) -> None:
        if self._finalized:
            return
        self._finalized = True
        cleanup_ok = await self._cleanup_exposures()
        overall = success_hint and cleanup_ok and not self._errors
        if not self._active_names:
            overall = False
        self._overall_success = overall
        if not overall:
            reasons = [f"{name}: {msg}" for name, msg in sorted(self._errors.items())]
            self._last_error = "; ".join(reasons) if reasons else "smoke test failed"
            self.log.error("Smoke test failed: %s", self._last_error)
        else:
            self._last_error = None
            self.log.info("All smoke tests completed successfully")
        if self._core and getattr(self._core, "status", None):
            status = self._core.status()
            if status.get("running"):
                try:
                    await self._core.stop(cancel_orders=True)
                except Exception:
                    self.log.exception("error stopping core from smoke test")

    # ------------------------------------------------------------------
    async def _resolve_order_size(self, connector: Any, cfg: ConnectorTestConfig) -> Tuple[int, int, int]:
        price_dec, size_dec = await connector.get_price_size_decimals(cfg.symbol)
        market = await connector.get_market_info(cfg.symbol)
        min_qty = market.get("min_qty") or market.get("min_order_size") or market.get("minLotSize")
        try:
            min_qty_f = float(min_qty)
        except (TypeError, ValueError):
            min_qty_f = 0.0
        size_scale = 10 ** size_dec
        base_amount_i = max(int(round(min_qty_f * size_scale)) or 1, 1)
        return base_amount_i, price_dec, size_dec

    def _filled_amount_i(self, tracker: TrackingLimitOrder, size_dec: int, fallback: int) -> int:
        scale = 10 ** size_dec
        update = tracker.snapshot()
        if update.filled_base is not None:
            try:
                amount = abs(float(update.filled_base))
                amount_i = int(round(amount * scale))
                if amount_i > 0:
                    return amount_i
            except Exception:
                pass
        inner = getattr(tracker, "_tracker", None)
        if inner is not None:
            for past in reversed(inner.history):
                filled = getattr(past, "filled_base", None)
                if filled is not None:
                    try:
                        amount = abs(float(filled))
                        amount_i = int(round(amount * scale))
                        if amount_i > 0:
                            return amount_i
                    except Exception:
                        continue
        return fallback

    async def _verify_connector_health(self, name: str, connector: Any, symbol: str) -> None:
        open_orders: List[Dict[str, Any]] = []
        rest_fn = getattr(connector, "get_open_orders", None)
        if callable(rest_fn):
            try:
                open_orders = await rest_fn(symbol)
            except TypeError:
                open_orders = await rest_fn()
            except Exception as exc:
                self.log.warning("%s open_orders check failed: %s", name, exc)
        if open_orders:
            self._record_failure(name, f"REST reports {len(open_orders)} open orders")

        positions = await self._fetch_positions(connector)
        exposures = [
            (sym, qty)
            for sym, qty, _ in positions
            if sym and qty is not None and abs(float(qty)) > POSITION_TOLERANCE
        ]
        if exposures:
            self._record_failure(name, f"positions still open: {self._format_positions_summary(positions)}")

        tracker_map = getattr(connector, "_order_trackers_by_client", None)
        if isinstance(tracker_map, dict):
            inflight = [k for k, v in tracker_map.items() if getattr(v, "state", OrderState.CANCELLED) not in {OrderState.FILLED, OrderState.CANCELLED, OrderState.FAILED}]
            if inflight:
                self._record_failure(name, f"tracker reports inflight orders: {inflight}")

    async def _cleanup_exposures(self) -> bool:
        ok = True
        for name in self._active_names:
            connector = self._connectors.get(name)
            if not connector:
                continue
            cfg = self.params.connectors.get(name)
            positions_before = await self._fetch_positions(connector)
            summary_before = self._format_positions_summary(positions_before)
            if summary_before:
                self.log.info("%s positions before cleanup: %s", name, summary_before)
            else:
                self.log.info("%s positions before cleanup: none", name)
            cancel_ok = await self._cancel_all_orders(name, connector)
            flatten_ok = await self._flatten_positions(name, connector, cfg)
            await asyncio.sleep(0.2)
            positions_after = await self._fetch_positions(connector)
            summary_after = self._format_positions_summary(positions_after)
            if summary_after:
                self.log.info("%s positions after cleanup: %s", name, summary_after)
            else:
                self.log.info("%s positions after cleanup: none", name)
            residual = any(
                qty is not None and abs(float(qty)) > POSITION_TOLERANCE for _, qty, _ in positions_after
            )
            if residual:
                self._record_failure(name, "residual exposure after cleanup")
                ok = False
            if not cancel_ok or not flatten_ok:
                ok = False
        return ok

    async def _cancel_all_orders(self, name: str, connector: Any) -> bool:
        cancel_all = getattr(connector, "cancel_all", None)
        if not callable(cancel_all):
            return True
        try:
            result = cancel_all()
            if asyncio.iscoroutine(result):
                await result
            self.log.info("%s cancel_all issued", name)
            return True
        except Exception as exc:
            self._record_failure(name, f"cancel_all error: {exc}")
            return False

    async def _flatten_positions(
        self,
        name: str,
        connector: Any,
        cfg: Optional[ConnectorTestConfig],
    ) -> bool:
        success = True
        timeout = cfg.market_timeout_secs if cfg else 30.0
        for cycle in range(MAX_FLATTEN_CYCLES):
            positions = await self._fetch_positions(connector)
            actionable = [
                (symbol, qty)
                for symbol, qty, _ in positions
                if symbol and qty is not None and abs(float(qty)) > POSITION_TOLERANCE
            ]
            if not actionable:
                return success
            self.log.info("%s flatten cycle %s actionable=%s", name, cycle + 1, len(actionable))
            for symbol, qty in actionable:
                base_amount_i = await self._position_to_base_units(connector, symbol, qty)
                if base_amount_i <= 0:
                    self._record_failure(name, f"unable to determine flatten size for {symbol}")
                    success = False
                    continue
                tracker = await self._execute_market_order(
                    name=name,
                    connector=connector,
                    symbol=symbol,
                    base_amount_i=base_amount_i,
                    is_ask=qty > 0,
                    timeout=timeout,
                )
                if tracker is None:
                    success = False
            await asyncio.sleep(0.5)
        positions = await self._fetch_positions(connector)
        if any(
            symbol and qty is not None and abs(float(qty)) > POSITION_TOLERANCE for symbol, qty, _ in positions
        ):
            success = False
        return success

    async def _execute_market_order(
        self,
        *,
        name: str,
        connector: Any,
        symbol: str,
        base_amount_i: int,
        is_ask: bool,
        timeout: float,
    ) -> Optional[TrackingMarketOrder]:
        tracker = await connector.submit_market_order(
            symbol=symbol,
            client_order_index=self._next_coi(),
            base_amount=base_amount_i,
            is_ask=is_ask,
            reduce_only=1,
        )
        try:
            await tracker.wait_final(timeout=max(timeout, 1.0))
        except asyncio.TimeoutError:
            self._record_failure(name, f"flatten market timeout for {symbol}")
            return None
        self._log_order_history(name, "flatten", tracker)
        return tracker

    async def _fetch_positions(
        self, connector: Any
    ) -> List[Tuple[Optional[str], Optional[float], Dict[str, Any]]]:
        get_positions = getattr(connector, "get_positions", None)
        if not callable(get_positions):
            return []
        try:
            raw = await get_positions()
        except Exception:
            return []
        entries: List[Dict[str, Any]] = []
        if isinstance(raw, dict):
            entries = [v for v in raw.values() if isinstance(v, dict)]
        elif isinstance(raw, list):
            entries = [v for v in raw if isinstance(v, dict)]
        out: List[Tuple[Optional[str], Optional[float], Dict[str, Any]]] = []
        for entry in entries:
            symbol = entry.get("symbol") or entry.get("instrument")
            qty = self._extract_position_size(entry)
            out.append((symbol, qty, entry))
        return out

    def _extract_position_size(self, entry: Dict[str, Any]) -> Optional[float]:
        keys = (
            "position",
            "netPosition",
            "netQuantity",
            "net_quantity",
            "size",
            "position_size",
            "quantity",
        )
        for key in keys:
            if key in entry:
                try:
                    return float(entry[key])
                except (TypeError, ValueError):
                    continue
        return None

    async def _position_to_base_units(self, connector: Any, symbol: str, qty: float) -> int:
        getter = getattr(connector, "get_price_size_decimals", None)
        if not callable(getter):
            return 0
        try:
            _price_dec, size_dec = await getter(symbol)
            scale = 10 ** size_dec
            return max(int(round(abs(float(qty)) * scale)), 1)
        except Exception:
            return 0

    def _format_positions_summary(
        self, positions: List[Tuple[Optional[str], Optional[float], Dict[str, Any]]]
    ) -> str:
        parts: List[str] = []
        for symbol, qty, _ in positions:
            if symbol is None or qty is None:
                continue
            try:
                parts.append(f"{symbol}:{float(qty):.8f}")
            except Exception:
                parts.append(f"{symbol}:{qty}")
        return ", ".join(parts)

    def _log_order_history(
        self,
        name: str,
        order_type: str,
        tracker: TrackingLimitOrder | TrackingMarketOrder,
    ) -> None:
        inner = getattr(tracker, "_tracker", None)
        history = inner.history if inner is not None else []
        if not history:
            self._log_order_update(name, order_type, tracker.snapshot())
            return
        for update in history:
            self._log_order_update(name, order_type, update)

    def _log_order_update(self, name: str, order_type: str, update: OrderUpdate) -> None:
        info_summary = self._summarize_info(update.info)
        self.log.info(
            "%s %s update state=%s filled=%s remaining=%s info=%s",
            name,
            order_type,
            update.state.value,
            self._format_float(update.filled_base),
            self._format_float(update.remaining_base),
            info_summary or "-",
        )

    def _format_float(self, value: Optional[float]) -> str:
        if value is None:
            return "?"
        try:
            return f"{float(value):.8f}"
        except Exception:
            return str(value)

    def _summarize_info(self, info: Optional[Dict[str, Any]]) -> str:
        if not info:
            return ""
        text = self._collect_info_text(info)
        if not text:
            return ""
        return text[:200]

    def _collect_info_text(self, data: Any) -> str:
        parts: List[str] = []

        def _walk(obj: Any):
            if isinstance(obj, dict):
                for value in obj.values():
                    _walk(value)
            elif isinstance(obj, (list, tuple, set)):
                for item in obj:
                    _walk(item)
            elif isinstance(obj, str):
                parts.append(obj)

        _walk(data)
        return " ".join(parts)

    def _next_coi(self) -> int:
        self._coi_seed = (self._coi_seed + 1) % 1_000_000
        return self._coi_seed or 1


__all__ = ["ConnectorSmokeTestStrategy", "SmokeTestParams", "ConnectorTestConfig"]
