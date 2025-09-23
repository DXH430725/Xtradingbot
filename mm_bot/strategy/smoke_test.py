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
        bid_i, ask_i, scale = await self._safe_top_of_book(connector, symbol, price_dec)
        if bid_i is None and ask_i is None:
            raise RuntimeError(f"{name}: unable to determine top of book for {symbol}")
        price_i = self._pick_limit_price(bid_i, ask_i, scale, cfg.price_offset_ticks, is_primary_sell)
        client_order_index = self._next_coi()
        self.log.info(
            "%s placing %s limit test order symbol=%s size_i=%s price_i=%s",
            name,
            "sell" if is_primary_sell else "buy",
            symbol,
            base_amount_i,
            price_i,
        )
        limit_tracker: TrackingLimitOrder = await connector.submit_limit_order(
            symbol=symbol,
            client_order_index=client_order_index,
            base_amount=base_amount_i,
            price=price_i,
            is_ask=is_primary_sell,
            post_only=False,
        )
        try:
            await limit_tracker.wait_final(timeout=cfg.limit_timeout_secs)
        except asyncio.TimeoutError:
            self.log.warning("%s limit order timeout; cancelling", name)
            cancel_fn = getattr(connector, "cancel_by_client_id", None)
            if callable(cancel_fn):
                await cancel_fn(symbol, client_order_index)
            return

        if limit_tracker.state not in {OrderState.FILLED, OrderState.PARTIALLY_FILLED}:
            self.log.info("%s limit order finished with state=%s", name, limit_tracker.state.value)
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
    ) -> int:
        if primary_sell:
            if ask_i is not None:
                return ask_i + offset_ticks
            if bid_i is not None:
                return bid_i + max(offset_ticks, 1)
        else:
            if bid_i is not None:
                return bid_i - offset_ticks
            if ask_i is not None:
                return ask_i - max(offset_ticks, 1)
        fallback_price = int(25_000 * scale)
        return max(fallback_price, 1)

    def _next_coi(self) -> int:
        self._coi_seed = (self._coi_seed + 1) % 1_000_000
        return self._coi_seed


__all__ = ["ConnectorSmokeTestStrategy", "SmokeTestParams", "ConnectorTestConfig"]
