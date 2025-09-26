import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from mm_bot.execution import place_tracking_limit_order
from mm_bot.execution.orders import OrderState, TrackingLimitOrder
from mm_bot.strategy.strategy_base import StrategyBase


POSITION_EPS = 1e-9


@dataclass
class LiquidationHedgeParams:
    backpack_symbol: str = "ETH_USDC_PERP"
    lighter_symbol: str = "ETH"
    leverage: float = 50.0
    direction: str = "long_backpack"  # or "short_backpack"
    price_offset_ticks: int = 1
    tracking_cancel_wait_secs: float = 2.0
    tracking_post_only: bool = True
    timeout_secs: float = 24 * 3600.0
    poll_interval_secs: float = 5.0
    reduce_only_buffer_ticks: int = 5
    reverse_on_timeout: bool = True
    max_cycles: int = 1
    liquidation_tolerance: float = 1e-4
    min_collateral: float = 0.0


class LiquidationHedgeStrategy(StrategyBase):
    """Run leveraged liquidation hedge between Backpack and Lighter."""

    def __init__(
        self,
        *,
        backpack_connector: Any,
        lighter_connector: Any,
        params: Optional[LiquidationHedgeParams] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if backpack_connector is None or lighter_connector is None:
            raise ValueError("Both backpack and lighter connectors are required")
        self.backpack = backpack_connector
        self.lighter = lighter_connector
        self.params = params or LiquidationHedgeParams()
        self.log = logger or logging.getLogger("mm_bot.strategy.liquidation_hedge")

        self._core: Optional[Any] = None
        self._started: bool = False
        self._task: Optional[asyncio.Task] = None

        # market metadata
        self._bp_price_scale: int = 1
        self._bp_size_scale: int = 1
        self._bp_min_size_i: int = 1
        self._lg_price_scale: int = 1
        self._lg_size_scale: int = 1
        self._lg_min_size_i: int = 1

        # runtime state
        self._direction_long: bool = self.params.direction.lower() != "short_backpack"
        self._cycle_index: int = 0
        self._close_order: Optional[Dict[str, Any]] = None
        self._active = True

    # ------------------------------------------------------------------
    def start(self, core: Any) -> None:
        if self._started:
            return
        self._core = core
        self._started = True
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._run(), name="liquidation_hedge.run")

    def stop(self) -> None:
        self._active = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._started = False

    async def on_tick(self, now_ms: float) -> None:
        await asyncio.sleep(0)

    # ------------------------------------------------------------------
    async def _run(self) -> None:
        try:
            await self._prepare_markets()
            cycles = max(1, int(self.params.max_cycles))
            for idx in range(cycles):
                if not self._active:
                    break
                self._cycle_index = idx
                success = await self._execute_cycle()
                if not success:
                    break
                if not self.params.reverse_on_timeout:
                    break
                # flip direction for subsequent cycle if requested
                self._direction_long = not self._direction_long
            self.log.info("Liquidation hedge strategy finished")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.log.exception("liquidation hedge strategy crashed")

    async def _prepare_markets(self) -> None:
        await self.backpack.start_ws_state([self.params.backpack_symbol])
        await self.backpack._ensure_markets()
        bp_p_dec, bp_s_dec = await self.backpack.get_price_size_decimals(self.params.backpack_symbol)
        self._bp_price_scale = 10 ** bp_p_dec
        self._bp_size_scale = 10 ** bp_s_dec
        bp_info = await self.backpack.get_market_info(self.params.backpack_symbol)
        bp_step = float(bp_info.get("step_size") or bp_info.get("min_qty") or 0.0)
        self._bp_min_size_i = max(1, int(round(bp_step * self._bp_size_scale)))

        await self.lighter.start_ws_state()
        await self.lighter._ensure_markets()
        lg_p_dec, lg_s_dec = await self.lighter.get_price_size_decimals(self.params.lighter_symbol)
        self._lg_price_scale = 10 ** lg_p_dec
        self._lg_size_scale = 10 ** lg_s_dec
        self._lg_min_size_i = max(1, await self.lighter.get_min_order_size_i(self.params.lighter_symbol))

    # ------------------------------------------------------------------
    async def _execute_cycle(self) -> bool:
        try:
            order_size = await self._compute_order_size()
        except Exception as exc:
            self.log.error("unable to compute order size: %s", exc)
            return False
        if order_size is None:
            self.log.error("order size unavailable; aborting cycle")
            return False
        bp_size_i, lg_size_i, base_amount, collateral = order_size
        self.log.info(
            "cycle %s direction=%s base=%.6f backpack_size_i=%s lighter_size_i=%s",
            self._cycle_index,
            "long" if self._direction_long else "short",
            base_amount,
            bp_size_i,
            lg_size_i,
        )
        entry = await self._place_backpack_entry(bp_size_i)
        if entry is None:
            return False
        filled_i = self._resolve_filled(entry, self._bp_size_scale, bp_size_i)
        if filled_i <= 0:
            self.log.error("unable to resolve filled amount; aborting")
            return False
        lighter_size_i = max(self._lg_min_size_i, int(round((filled_i / self._bp_size_scale) * self._lg_size_scale)))
        hedge_ok = await self._place_lighter_hedge(lighter_size_i)
        if not hedge_ok:
            await self._flatten_backpack(filled_i, emergency=True)
            return False
        monitor_state = await self._monitor_liquidation(filled_i, lighter_size_i)
        if monitor_state == "liquidated":
            await self._request_topup("lighter", collateral, "post_liquidation")
        self.log.info("cycle %s completed with state=%s", self._cycle_index, monitor_state)
        return monitor_state in {"liquidated", "timeout_reversed"}

    async def _compute_order_size(self) -> Optional[Tuple[int, int, float, float]]:
        overview = await self.lighter.get_account_overview()
        accounts = overview.get("accounts") if isinstance(overview, dict) else None
        if not accounts:
            return None
        account = accounts[0]
        collateral = float(account.get("collateral") or account.get("available_balance") or 0.0)
        if collateral <= max(self.params.min_collateral, 0.0):
            self.log.error("lighter collateral %.6f below minimum", collateral)
            return None
        top_bid_i, top_ask_i, scale = await self.lighter.get_top_of_book(self.params.lighter_symbol)
        if top_bid_i is None and top_ask_i is None:
            return None
        price_i = top_ask_i if self._direction_long else top_bid_i
        if price_i is None:
            price_i = top_bid_i or top_ask_i
        price = price_i / float(scale or 1)
        if price <= 0:
            return None
        notional = collateral * max(self.params.leverage, 1.0)
        base_amount = notional / price
        bp_size_i = max(self._bp_min_size_i, int(round(base_amount * self._bp_size_scale)))
        lg_size_i = max(self._lg_min_size_i, int(round(base_amount * self._lg_size_scale)))
        if bp_size_i <= 0 or lg_size_i <= 0:
            return None
        return bp_size_i, lg_size_i, base_amount, collateral

    async def _place_backpack_entry(self, size_i: int) -> Optional[TrackingLimitOrder]:
        try:
            order = await place_tracking_limit_order(
                self.backpack,
                symbol=self.params.backpack_symbol,
                base_amount_i=size_i,
                is_ask=not self._direction_long,
                price_offset_ticks=self.params.price_offset_ticks,
                cancel_wait_secs=self.params.tracking_cancel_wait_secs,
                post_only=self.params.tracking_post_only,
                reduce_only=0,
                logger=self.log,
            )
            if order.state != OrderState.FILLED:
                self.log.error("backpack tracking order ended state=%s", order.state.value)
                return None
            return order
        except Exception as exc:
            self.log.error("backpack tracking order failed: %s", exc)
            return None

    async def _place_lighter_hedge(self, size_i: int) -> bool:
        try:
            tracker = await self.lighter.submit_market_order(
                symbol=self.params.lighter_symbol,
                client_order_index=int(time.time() * 1000) % 1_000_000,
                base_amount=size_i,
                is_ask=self._direction_long,
                reduce_only=0,
            )
            await tracker.wait_final(timeout=30.0)
            if tracker.state not in {OrderState.FILLED, OrderState.CANCELLED}:
                self.log.error("lighter hedge unexpected state=%s", tracker.state.value)
                return False
            return True
        except Exception as exc:
            self.log.error("lighter hedge placement failed: %s", exc)
            return False

    async def _monitor_liquidation(self, bp_filled_i: int, lg_size_i: int) -> str:
        start_ts = time.time()
        monitor_state = "timeout"
        try:
            while self._active:
                elapsed = time.time() - start_ts
                if elapsed >= self.params.timeout_secs:
                    self.log.warning("hedge timeout reached; initiating unwind")
                    await self._timeout_unwind(bp_filled_i, lg_size_i)
                    monitor_state = "timeout_reversed"
                    break
                liq_price, position_size = await self._lighter_position_state()
                if position_size <= POSITION_EPS:
                    self.log.info("lighter position closed (likely liquidated)")
                    await self._ensure_backpack_flat()
                    monitor_state = "liquidated"
                    break
                await self._ensure_reduce_only_order(liq_price, bp_filled_i)
                await asyncio.sleep(self.params.poll_interval_secs)
        finally:
            await self._cancel_reduce_only()
        return monitor_state

    async def _lighter_position_state(self) -> Tuple[float, float]:
        try:
            positions = await self.lighter.get_positions()
        except Exception as exc:
            self.log.warning("lighter get_positions failed: %s", exc)
            return 0.0, 0.0
        for entry in positions:
            sym = str(entry.get("symbol") or "").upper()
            if sym != self.params.lighter_symbol.upper():
                continue
            try:
                qty = float(entry.get("position") or entry.get("netQuantity") or 0.0)
            except Exception:
                qty = 0.0
            try:
                liq_price = float(entry.get("liquidation_price") or entry.get("liquidationPrice") or 0.0)
            except Exception:
                liq_price = 0.0
            return liq_price, abs(qty)
        return 0.0, 0.0

    async def _ensure_reduce_only_order(self, liq_price: float, bp_filled_i: int) -> None:
        if liq_price <= 0:
            return
        price_i = int(round(liq_price * self._bp_price_scale))
        if price_i <= 0:
            return
        buffer_ticks = max(int(self.params.reduce_only_buffer_ticks), 0)
        if self._direction_long:
            price_i = max(1, price_i - buffer_ticks)
        else:
            price_i = max(1, price_i + buffer_ticks)
        reorder = False
        if self._close_order is None:
            reorder = True
        else:
            if abs(self._close_order.get("price_i", 0) - price_i) >= max(1, buffer_ticks):
                reorder = True
        if not reorder:
            return
        await self._cancel_reduce_only()
        coi = int(time.time() * 1000) % 1_000_000
        try:
            await self.backpack.place_limit(
                self.params.backpack_symbol,
                client_order_index=coi,
                base_amount=bp_filled_i,
                price=price_i,
                is_ask=self._direction_long,
                post_only=False,
                reduce_only=1,
            )
            self._close_order = {"coi": coi, "price_i": price_i, "size_i": bp_filled_i}
            self.log.info("placed reduce-only order coi=%s price_i=%s", coi, price_i)
        except Exception as exc:
            self.log.error("failed to place reduce-only order: %s", exc)
            self._close_order = None

    async def _cancel_reduce_only(self) -> None:
        if not self._close_order:
            return
        coi = self._close_order.get("coi")
        try:
            if coi is not None:
                await self.backpack.cancel_by_client_id(self.params.backpack_symbol, int(coi))
        except Exception:
            pass
        self._close_order = None

    async def _ensure_backpack_flat(self) -> None:
        try:
            positions = await self.backpack.get_positions()
        except Exception:
            positions = []
        for entry in positions:
            sym = str(entry.get("symbol") or "").upper()
            if sym != self.params.backpack_symbol.upper():
                continue
            qty = float(entry.get("position") or entry.get("size") or 0.0)
            if abs(qty) <= POSITION_EPS:
                continue
            self.log.info("residual backpack position %.8f; flattening", qty)
            await self._flatten_backpack(int(round(abs(qty) * self._bp_size_scale)), emergency=True)

    async def _flatten_backpack(self, size_i: int, emergency: bool = False) -> None:
        if size_i <= 0:
            return
        try:
            tracker = await place_tracking_limit_order(
                self.backpack,
                symbol=self.params.backpack_symbol,
                base_amount_i=size_i,
                is_ask=self._direction_long,
                price_offset_ticks=self.params.price_offset_ticks,
                cancel_wait_secs=self.params.tracking_cancel_wait_secs,
                post_only=False,
                reduce_only=1,
                logger=self.log if emergency else None,
            )
            if tracker.state != OrderState.FILLED:
                await tracker.wait_final(timeout=30.0)
        except Exception as exc:
            self.log.error("flatten backpack failed: %s", exc)
        finally:
            await self._cancel_reduce_only()

    async def _timeout_unwind(self, bp_size_i: int, lg_size_i: int) -> None:
        await self._flatten_backpack(bp_size_i, emergency=True)
        try:
            tracker = await self.lighter.submit_market_order(
                symbol=self.params.lighter_symbol,
                client_order_index=int(time.time() * 1000) % 1_000_000,
                base_amount=lg_size_i,
                is_ask=not self._direction_long,
                reduce_only=1,
            )
            await tracker.wait_final(timeout=30.0)
        except Exception as exc:
            self.log.error("timeout hedge close failed: %s", exc)

    def _resolve_filled(self, tracker: TrackingLimitOrder, size_scale: int, fallback: int) -> int:
        scale = size_scale
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

    async def _request_topup(self, venue: str, amount: float, reason: str) -> None:
        self.log.info("top-up hook placeholder venue=%s amount=%.6f reason=%s", venue, amount, reason)

__all__ = ["LiquidationHedgeStrategy", "LiquidationHedgeParams"]
