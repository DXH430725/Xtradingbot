import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from mm_bot.strategy.strategy_base import StrategyBase
from mm_bot.execution.orders import OrderState, TrackingLimitOrder, TrackingMarketOrder


@dataclass
class ConnectorTestConfig:
    symbol: str
    side: str = "buy"  # buy then sell, or sell then buy
    limit_timeout_secs: float = 20.0
    settle_timeout_secs: float = 10.0
    price_offset_ticks: int = 0  # positive widens away from mid when placing limit
    max_price_retries: int = 1  # extra attempts after the initial try
    retry_cooldown_secs: float = 0.5


@dataclass
class SmokeTestParams:
    connectors: Dict[str, ConnectorTestConfig] = field(default_factory=dict)
    pause_between_tests_secs: float = 2.0


class ConnectorSmokeTestStrategy(StrategyBase):
    """Lightweight strategy that exercises connectors via one round-trip order.

    For each configured connector the strategy places a small limit order, waits for
    a terminal state, and optionally flattens the position with a market order.
    """

    def __init__(self, connectors: Dict[str, Any], params: SmokeTestParams) -> None:
        self.log = logging.getLogger("mm_bot.strategy.smoke_test")
        self._connectors = connectors
        self.params = params
        self._core: Optional[Any] = None
        self._tasks: Dict[str, asyncio.Task] = {}
        self._started = False
        self._coi_seed = int(time.time() * 1000) % 1_000_000 or 1

    # ------------------------------------------------------------------
    def start(self, core: Any) -> None:
        self._core = core
        if self._started:
            return
        self._started = True
        loop = asyncio.get_event_loop()
        for name, cfg in self.params.connectors.items():
            connector = self._connectors.get(name)
            if connector is None:
                self.log.warning("connector '%s' not available; skipping smoke test", name)
                continue
            task = loop.create_task(self._run_test(name, connector, cfg), name=f"smoke_test[{name}]")
            self._tasks[name] = task

    def stop(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        self._tasks.clear()
        self._started = False

    async def on_tick(self, now_ms: float):
        # strategy work happens in created tasks
        await asyncio.sleep(0)

    # ------------------------------------------------------------------
    async def _run_test(self, name: str, connector: Any, cfg: ConnectorTestConfig) -> None:
        try:
            await self._exercise_connector(name, connector, cfg)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.log.exception("smoke test for connector '%s' failed", name)

    async def _exercise_connector(self, name: str, connector: Any, cfg: ConnectorTestConfig) -> None:
        symbol = cfg.symbol
        side = cfg.side.lower()
        is_primary_sell = side in {"sell", "short"}
        price_dec, size_dec = await connector.get_price_size_decimals(symbol)
        market = await connector.get_market_info(symbol)
        min_qty = market.get("min_qty") or market.get("min_order_size") or market.get("minLotSize")
        try:
            min_qty = float(min_qty)
        except (TypeError, ValueError):
            min_qty = 0.0
        size_scale = 10 ** size_dec
        base_amount_i = max(int(round(min_qty * size_scale)) or 1, 1)

        limit_tracker = await self._place_limit_with_retries(
            name=name,
            connector=connector,
            symbol=symbol,
            base_amount_i=base_amount_i,
            cfg=cfg,
            is_primary_sell=is_primary_sell,
            price_decimals=price_dec,
        )

        if limit_tracker is None:
            return

        if limit_tracker.state not in {OrderState.FILLED, OrderState.PARTIALLY_FILLED}:
            info_summary = self._summarize_info(limit_tracker.snapshot().info)
            if info_summary:
                self.log.info(
                    "%s limit order finished with state=%s (%s)",
                    name,
                    limit_tracker.state.value,
                    info_summary,
                )
            else:
                self.log.info(
                    "%s limit order finished with state=%s",
                    name,
                    limit_tracker.state.value,
                )
            return

        await asyncio.sleep(max(cfg.settle_timeout_secs, 0.0))
        market_tracker: TrackingMarketOrder = await connector.submit_market_order(
            symbol=symbol,
            client_order_index=self._next_coi(),
            base_amount=base_amount_i,
            is_ask=not is_primary_sell,
        )
        try:
            await market_tracker.wait_final(timeout=cfg.limit_timeout_secs)
        except asyncio.TimeoutError:
            self.log.warning("%s market close timeout for symbol=%s", name, symbol)
        await asyncio.sleep(self.params.pause_between_tests_secs)

    # ------------------------------------------------------------------
    async def _safe_top_of_book(self, connector: Any, symbol: str, price_dec: int) -> tuple[Optional[int], Optional[int], int]:
        scale = 10 ** price_dec
        if hasattr(connector, "get_top_of_book"):
            try:
                bid_i, ask_i, scale = await connector.get_top_of_book(symbol)
                return bid_i, ask_i, scale
            except Exception:
                pass
        if hasattr(connector, "get_order_book"):
            try:
                ob = await connector.get_order_book(symbol, depth=5)
                bids = ob.get("bids") if isinstance(ob, dict) else []
                asks = ob.get("asks") if isinstance(ob, dict) else []
                bid_i = int(float(bids[0][0]) * scale) if bids else None
                ask_i = int(float(asks[0][0]) * scale) if asks else None
                return bid_i, ask_i, scale
            except Exception:
                pass
        return None, None, scale

    def _pick_limit_price(
        self,
        bid_i: Optional[int],
        ask_i: Optional[int],
        scale: int,
        offset_ticks: int,
        primary_sell: bool,
        *,
        reference_i: Optional[int] = None,
        force_cross: bool = False,
    ) -> int:
        tick = max(offset_ticks, 1)
        fallback_price = int(25_000 * scale)

        if primary_sell:
            if force_cross:
                if bid_i is not None:
                    return max(bid_i - tick, 1)
                if reference_i is not None:
                    return max(reference_i - tick, 1)
            base = ask_i if ask_i is not None else reference_i
            if base is None:
                base = bid_i
            if base is None:
                return fallback_price
            if reference_i is not None:
                tolerance = max(int(reference_i * 0.01), tick)
                if abs(base - reference_i) > tolerance:
                    base = reference_i
            price_i = base + offset_ticks
            if bid_i is not None:
                price_i = max(price_i, bid_i + 1)
            return max(price_i, 1)
        else:
            if force_cross:
                if ask_i is not None:
                    return ask_i + tick
                if reference_i is not None:
                    return reference_i + tick
            base = bid_i if bid_i is not None else reference_i
            if base is None:
                base = ask_i
            if base is None:
                return fallback_price
            if reference_i is not None:
                tolerance = max(int(reference_i * 0.01), tick)
                if abs(base - reference_i) > tolerance:
                    base = reference_i
            price_i = max(base - offset_ticks, 1)
            if ask_i is not None:
                price_i = min(price_i, max(ask_i - 1, 1))
            return price_i

    async def _reference_price_i(
        self,
        connector: Any,
        symbol: str,
        scale: int,
        bid_i: Optional[int],
        ask_i: Optional[int],
    ) -> Optional[int]:
        get_last_price = getattr(connector, "get_last_price", None)
        if callable(get_last_price):
            try:
                last_price = await get_last_price(symbol)
            except Exception:
                last_price = None
            if last_price is not None:
                try:
                    return int(round(float(last_price) * scale))
                except (TypeError, ValueError):
                    pass
        if bid_i is not None and ask_i is not None:
            return (bid_i + ask_i) // 2
        return bid_i or ask_i

    def _next_coi(self) -> int:
        self._coi_seed = (self._coi_seed + 1) % 1_000_000
        return self._coi_seed

    async def _place_limit_with_retries(
        self,
        *,
        name: str,
        connector: Any,
        symbol: str,
        base_amount_i: int,
        cfg: ConnectorTestConfig,
        is_primary_sell: bool,
        price_decimals: int,
    ) -> Optional[TrackingLimitOrder]:
        attempts = max(1, int(cfg.max_price_retries) + 1)
        timeout = cfg.limit_timeout_secs if cfg.limit_timeout_secs and cfg.limit_timeout_secs > 0 else None
        tracker: Optional[TrackingLimitOrder] = None

        for attempt in range(attempts):
            force_cross = attempt > 0
            bid_i, ask_i, scale = await self._safe_top_of_book(connector, symbol, price_decimals)
            if bid_i is None and ask_i is None:
                raise RuntimeError(f"{name}: unable to determine top of book for {symbol}")
            reference_i = await self._reference_price_i(connector, symbol, scale, bid_i, ask_i)
            price_i = self._pick_limit_price(
                bid_i,
                ask_i,
                scale,
                cfg.price_offset_ticks,
                is_primary_sell,
                reference_i=reference_i,
                force_cross=force_cross,
            )
            client_order_index = self._next_coi()
            price_f = self._format_price(price_i, scale)
            bid_f = self._format_price(bid_i, scale) if bid_i is not None else "?"
            ask_f = self._format_price(ask_i, scale) if ask_i is not None else "?"
            ref_f = self._format_price(reference_i, scale) if reference_i is not None else "?"
            self.log.info(
                "%s placing %s limit test order symbol=%s size_i=%s price_i=%s (%s) bid=%s ask=%s ref=%s attempt=%s/%s",
                name,
                "sell" if is_primary_sell else "buy",
                symbol,
                base_amount_i,
                price_i,
                price_f,
                bid_f,
                ask_f,
                ref_f,
                attempt + 1,
                attempts,
            )
            tracker = await connector.submit_limit_order(
                symbol=symbol,
                client_order_index=client_order_index,
                base_amount=base_amount_i,
                price=price_i,
                is_ask=is_primary_sell,
                post_only=False,
            )
            try:
                await tracker.wait_final(timeout=timeout)
            except asyncio.TimeoutError:
                self.log.warning("%s limit order timeout; cancelling", name)
                cancel_fn = getattr(connector, "cancel_by_client_id", None)
                if callable(cancel_fn):
                    await cancel_fn(symbol, client_order_index)
                cancel_wait = max(cfg.retry_cooldown_secs, 0.5)
                try:
                    await tracker.wait_final(timeout=cancel_wait)
                except asyncio.TimeoutError:
                    self.log.warning("%s cancel request did not settle within %.2fs", name, cancel_wait)
                return tracker

            if tracker.state in {OrderState.FILLED, OrderState.PARTIALLY_FILLED}:
                return tracker

            info = tracker.snapshot().info
            if tracker.state == OrderState.FAILED and force_cross is False and attempt + 1 < attempts:
                if self._should_retry_price(info):
                    reason = self._summarize_info(info)
                    if reason:
                        self.log.warning(
                            "%s limit order rejected (%s); retrying with aggressive price",
                            name,
                            reason,
                        )
                    else:
                        self.log.warning(
                            "%s limit order rejected; retrying with aggressive price",
                            name,
                        )
                    await asyncio.sleep(max(cfg.retry_cooldown_secs, 0.0))
                    continue
            return tracker

        return tracker

    def _should_retry_price(self, info: Optional[Dict[str, Any]]) -> bool:
        if not info:
            return False
        text = self._collect_info_text(info)
        if not text:
            return False
        lowered = text.lower()
        triggers = [
            "price is too far",
            "price out of range",
            "price deviation",
        ]
        return any(trigger in lowered for trigger in triggers)

    def _summarize_info(self, info: Optional[Dict[str, Any]]) -> str:
        if not info:
            return ""
        text = self._collect_info_text(info)
        if not text:
            return ""
        return text[:200]

    def _collect_info_text(self, data: Any) -> str:
        parts: list[str] = []

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

    def _format_price(self, price_i: Optional[int], scale: int) -> str:
        if price_i is None or scale <= 0:
            return "?"
        try:
            return f"{price_i / scale:.6f}"
        except Exception:
            return str(price_i)


__all__ = ["ConnectorSmokeTestStrategy", "SmokeTestParams", "ConnectorTestConfig"]
