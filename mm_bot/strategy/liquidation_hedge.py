import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import aiohttp

from mm_bot.execution import TrackingLimitTimeoutError, place_tracking_limit_order
from mm_bot.execution.orders import OrderState, TrackingLimitOrder
from mm_bot.strategy.strategy_base import StrategyBase

POSITION_EPS = 1e-9
COI_LIMIT = 281_474_976_710_655  # 2**48 - 1
DEFAULT_TELEMETRY_CONFIG = "mm_bot/conf/telemetry.json"


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
    backpack_entry_max_attempts: int = 3
    backpack_retry_delay_secs: float = 0.5
    lighter_max_slippage: float = 0.001
    lighter_hedge_max_attempts: int = 3
    lighter_retry_delay_secs: float = 0.5
    wait_min_secs: float = 2.0
    wait_max_secs: float = 5.0
    confirmation_timeout_secs: float = 2.0
    confirmation_poll_secs: float = 0.2
    rebalance_max_attempts: int = 3
    rebalance_retry_delay_secs: float = 0.5
    wait_min_secs: float = 2.0
    wait_max_secs: float = 5.0
    rebalance_max_attempts: int = 3
    rebalance_retry_delay_secs: float = 0.5
    telemetry_enabled: bool = True
    telemetry_config_path: str = DEFAULT_TELEMETRY_CONFIG
    telemetry_interval_secs: float = 2.0
    telegram_enabled: bool = True
    telegram_keys_path: str = "tg_key.txt"


class CycleState(Enum):
    INIT = "init"
    BP_FILLED = "bp_filled"
    L1_HEDGED = "l1_hedged"
    L2_ENTERED = "l2_entered"
    BP_REBALANCED = "bp_rebalanced"
    MONITORING = "monitoring"
    EXIT = "exit"


@dataclass
class VenueOrderPlan:
    base_amount: float
    size_i: int
    collateral: float


@dataclass
class PositionSnapshot:
    bp: float
    l1: float
    l2: float


@dataclass
class CycleContext:
    cycle_id: str
    bp_entry_size_i: int = 0
    bp_filled_i: int = 0
    bp_entry_base: float = 0.0
    l1_plan: Optional[VenueOrderPlan] = None
    l2_plan: Optional[VenueOrderPlan] = None
    l1_size_i: int = 0
    l2_size_i: int = 0
    l1_position: float = 0.0
    l2_position: float = 0.0
    start_snapshot: Optional[PositionSnapshot] = None
    start_collateral_bp: float = 0.0
    start_collateral_l1: float = 0.0
    start_collateral_l2: float = 0.0


class LiquidationHedgeStrategy(StrategyBase):
    """Run leveraged liquidation hedge between Backpack and two Lighter venues."""

    def __init__(
        self,
        *,
        backpack_connector: Any,
        lighter1_connector: Any,
        lighter2_connector: Any,
        params: Optional[LiquidationHedgeParams] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if backpack_connector is None or lighter1_connector is None or lighter2_connector is None:
            raise ValueError("Backpack and two lighter connectors are required")
        self.backpack = backpack_connector
        self.lighter1 = lighter1_connector
        self.lighter2 = lighter2_connector
        self.params = params or LiquidationHedgeParams()
        self.log = logger or logging.getLogger("mm_bot.strategy.liquidation_hedge")

        self._core: Optional[Any] = None
        self._started: bool = False
        self._task: Optional[asyncio.Task] = None
        self._active: bool = True

        # market metadata
        self._bp_price_scale: int = 1
        self._bp_size_scale: int = 1
        self._bp_min_size_i: int = 1
        self._lg1_price_scale: int = 1
        self._lg1_size_scale: int = 1
        self._lg1_min_size_i: int = 1
        self._lg2_price_scale: int = 1
        self._lg2_size_scale: int = 1
        self._lg2_min_size_i: int = 1

        # runtime state
        self._direction_long: bool = self.params.direction.lower() != "short_backpack"
        self._cycle_index: int = 0
        self._rng = random.Random()

        # order sequencing
        seed = int(time.time() * 1000) % COI_LIMIT or 1
        self._coi_seq: Dict[str, int] = {
            "backpack": seed,
            "lighter1": (seed + 1000) % COI_LIMIT or 1,
            "lighter2": (seed + 2000) % COI_LIMIT or 1,
        }
        self._order_locks: Dict[str, asyncio.Lock] = {
            "backpack": asyncio.Lock(),
            "lighter1": asyncio.Lock(),
            "lighter2": asyncio.Lock(),
        }

        # telemetry / notifications
        self._telemetry_cfg: Optional[Dict[str, Any]] = None
        self._telemetry_task: Optional[asyncio.Task] = None
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._tg_token: Optional[str] = None
        self._tg_chat_id: Optional[str] = None

    # ------------------------------------------------------------------
    def start(self, core: Any) -> None:
        if self._started:
            return
        self._core = core
        self._active = True
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
            await self._load_telemetry_config()
            self._start_telemetry()
            await self._notify_start()

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
                self._direction_long = not self._direction_long

            await self._notify_finished()
            self.log.info("Liquidation hedge strategy finished")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.log.exception("liquidation hedge strategy crashed")
            await self._send_telegram("[LiquidationHedge] 策略异常退出，请检查日志。")
        finally:
            await self._stop_telemetry()
            await self._close_http_session()

    async def _prepare_markets(self) -> None:
        await self.backpack.start_ws_state([self.params.backpack_symbol])
        await self.backpack._ensure_markets()
        bp_p_dec, bp_s_dec = await self.backpack.get_price_size_decimals(self.params.backpack_symbol)
        self._bp_price_scale = 10 ** bp_p_dec
        self._bp_size_scale = 10 ** bp_s_dec
        bp_info = await self.backpack.get_market_info(self.params.backpack_symbol)
        bp_step = float(bp_info.get("step_size") or bp_info.get("min_qty") or 0.0)
        self._bp_min_size_i = max(1, int(round(bp_step * self._bp_size_scale)))

        await self.lighter1.start_ws_state()
        await self.lighter1._ensure_markets()
        lg1_p_dec, lg1_s_dec = await self.lighter1.get_price_size_decimals(self.params.lighter_symbol)
        self._lg1_price_scale = 10 ** lg1_p_dec
        self._lg1_size_scale = 10 ** lg1_s_dec
        self._lg1_min_size_i = max(1, await self.lighter1.get_min_order_size_i(self.params.lighter_symbol))

        await self.lighter2.start_ws_state()
        await self.lighter2._ensure_markets()
        lg2_p_dec, lg2_s_dec = await self.lighter2.get_price_size_decimals(self.params.lighter_symbol)
        self._lg2_price_scale = 10 ** lg2_p_dec
        self._lg2_size_scale = 10 ** lg2_s_dec
        self._lg2_min_size_i = max(1, await self.lighter2.get_min_order_size_i(self.params.lighter_symbol))

    # ------------------------------------------------------------------
    async def _execute_cycle(self) -> bool:
        ctx = CycleContext(cycle_id=f"{int(time.time() * 1000)}-{self._cycle_index}")
        ctx.start_snapshot = await self._snapshot_positions()
        ctx.start_collateral_bp = await self._fetch_backpack_collateral()
        ctx.start_collateral_l1 = await self._fetch_lighter_collateral(self.lighter1)
        ctx.start_collateral_l2 = await self._fetch_lighter_collateral(self.lighter2)

        try:
            ctx.l1_plan = await self._plan_lighter_size(self.lighter1, self._lg1_size_scale, self._lg1_min_size_i)
            ctx.l2_plan = await self._plan_lighter_size(self.lighter2, self._lg2_size_scale, self._lg2_min_size_i)
        except Exception as exc:
            self.log.error("unable to compute order size: %s", exc)
            return False

        if ctx.l1_plan is None or ctx.l2_plan is None:
            self.log.error("order size unavailable; aborting cycle")
            return False

        self.log.info(
            "cycle %s direction=%s l1_base=%.6f l2_base=%.6f",
            ctx.cycle_id,
            "long" if self._direction_long else "short",
            ctx.l1_plan.base_amount,
            ctx.l2_plan.base_amount,
        )

        if not await self._stage_bp_entry(ctx):
            return False

        if not await self._stage_l1_hedge(ctx):
            return False

        await self._stage_wait()

        if not await self._stage_l2_entry(ctx):
            if not await self._rebalance_backpack(-(ctx.l1_position)):
                await self._emergency_exit(ctx, reason="lighter2_entry_failed")
                return False
            return await self._monitor_cycle(ctx, expect_l2=False)

        if not await self._stage_rebalance(ctx):
            await self._emergency_exit(ctx, reason="rebalance_failed")
            return False

        return await self._monitor_cycle(ctx, expect_l2=True)

    # --- stage helpers -------------------------------------------------
    async def _stage_bp_entry(self, ctx: CycleContext) -> bool:
        size_base = ctx.l1_plan.base_amount if ctx.l1_plan else 0.0
        ctx.bp_entry_size_i = max(self._bp_min_size_i, int(round(size_base * self._bp_size_scale)))

        entry = await self._place_backpack_entry(ctx.bp_entry_size_i)
        if entry is None:
            return False
        ctx.bp_filled_i = self._resolve_filled(entry, self._bp_size_scale, ctx.bp_entry_size_i)
        if ctx.bp_filled_i <= 0:
            self.log.error("unable to resolve filled amount; aborting")
            return False
        ctx.bp_entry_base = ctx.bp_filled_i / float(self._bp_size_scale)
        ctx.l1_size_i = max(self._lg1_min_size_i, int(round(ctx.bp_entry_base * self._lg1_size_scale)))
        self.log.info("cycle %s backpack filled base=%.6f size_i=%s", ctx.cycle_id, ctx.bp_entry_base, ctx.bp_filled_i)
        return True

    async def _stage_l1_hedge(self, ctx: CycleContext) -> bool:
        is_ask = self._direction_long
        size_i = max(ctx.l1_size_i, self._lg1_min_size_i)
        success = await self._submit_lighter_market(
            connector=self.lighter1,
            size_i=size_i,
            is_ask=is_ask,
            reduce_only=0,
            max_slippage=self.params.lighter_max_slippage,
            label="lighter1",
        )
        if not success:
            await self._flatten_backpack(ctx.bp_filled_i, emergency=True)
            return False
        pos = await self._confirm_position(
            connector=self.lighter1,
            baseline=ctx.start_snapshot.l1 if ctx.start_snapshot else 0.0,
            expected_sign=-1 if self._direction_long else 1,
            size_i=size_i,
            size_scale=self._lg1_size_scale,
            label="lighter1",
        )
        if pos is None:
            await self._flatten_backpack(ctx.bp_filled_i, emergency=True)
            return False
        ctx.l1_position = pos
        self.log.info("cycle %s lighter1 position=%.6f", ctx.cycle_id, pos)
        return True

    async def _stage_wait(self) -> None:
        wait_min = min(self.params.wait_min_secs, self.params.wait_max_secs)
        wait_max = max(self.params.wait_min_secs, self.params.wait_max_secs)
        if wait_max <= 0:
            return
        delay = self._rng.uniform(max(0.0, wait_min), wait_max)
        self.log.info("waiting %.2fs before lighter2 entry", delay)
        await asyncio.sleep(delay)

    async def _stage_l2_entry(self, ctx: CycleContext) -> bool:
        size_i = max(self._lg2_min_size_i, int(round(ctx.bp_entry_base * self._lg2_size_scale)))
        success = await self._submit_lighter_market(
            connector=self.lighter2,
            size_i=size_i,
            is_ask=not self._direction_long,
            reduce_only=0,
            max_slippage=self.params.lighter_max_slippage,
            label="lighter2",
        )
        if not success:
            return False
        pos = await self._confirm_position(
            connector=self.lighter2,
            baseline=ctx.start_snapshot.l2 if ctx.start_snapshot else 0.0,
            expected_sign=1 if self._direction_long else -1,
            size_i=size_i,
            size_scale=self._lg2_size_scale,
            label="lighter2",
        )
        if pos is None:
            return False
        ctx.l2_position = pos
        self.log.info("cycle %s lighter2 position=%.6f", ctx.cycle_id, pos)
        return True

    async def _stage_rebalance(self, ctx: CycleContext) -> bool:
        l1_pos = await self._get_lighter_position(self.lighter1)
        l2_pos = await self._get_lighter_position(self.lighter2)
        target = -(l1_pos + l2_pos)
        success = await self._rebalance_backpack(target)
        if success:
            ctx.l1_position = l1_pos
            ctx.l2_position = l2_pos
        return success

    async def _monitor_cycle(self, ctx: CycleContext, expect_l2: bool) -> bool:
        timeout = max(float(self.params.timeout_secs), 1.0)
        poll = max(float(self.params.poll_interval_secs), 1.0)
        tolerance = max(float(self.params.liquidation_tolerance), 1e-6)
        start_ts = time.time()
        self.log.debug("cycle %s entering monitor", ctx.cycle_id)
        try:
            while self._active:
                if time.time() - start_ts >= timeout:
                    self.log.warning("cycle %s timeout reached; initiating unwind", ctx.cycle_id)
                    await self._emergency_exit(ctx, reason="timeout")
                    return False

                l1_pos = await self._get_lighter_position(self.lighter1)
                l2_pos = await self._get_lighter_position(self.lighter2)
                if abs(l1_pos) <= POSITION_EPS:
                    await self._emergency_exit(ctx, reason="lighter1_zero")
                    return False
                if expect_l2 and abs(l2_pos) <= POSITION_EPS:
                    await self._emergency_exit(ctx, reason="lighter2_zero")
                    return False

                target = -(l1_pos + (l2_pos if expect_l2 else 0.0))
                bp_pos = await self._get_backpack_position()
                delta = target - bp_pos
                if abs(delta) > tolerance:
                    await self._rebalance_backpack(target)

                await asyncio.sleep(poll)
        finally:
            self.log.debug("cycle %s leaving monitor", ctx.cycle_id)
        return True

    # --- helpers -------------------------------------------------------
    async def _plan_lighter_size(self, connector: Any, size_scale: int, min_size_i: int) -> Optional[VenueOrderPlan]:
        try:
            overview = await connector.get_account_overview()
        except Exception as exc:
            self.log.error("%s account overview failed: %s", getattr(connector, "name", "lighter"), exc)
            return None
        accounts = overview.get("accounts") if isinstance(overview, dict) else None
        if not accounts:
            return None
        account = accounts[0]
        collateral = float(account.get("collateral") or account.get("available_balance") or account.get("equity") or 0.0)
        if collateral <= max(self.params.min_collateral, 0.0):
            self.log.error("lighter collateral %.6f below minimum", collateral)
            return None
        top_bid_i, top_ask_i, scale = await connector.get_top_of_book(self.params.lighter_symbol)
        if top_bid_i is None and top_ask_i is None:
            return None
        price_i = top_ask_i if self._direction_long else top_bid_i
        if price_i is None:
            price_i = top_bid_i or top_ask_i
        price = price_i / float(scale or 1)
        if price <= 0:
            return None
        leverage = max(self.params.leverage, 1.0)
        eff_collateral = collateral * 0.96
        notional = eff_collateral * leverage
        base_amount = notional / price
        size_i = max(min_size_i, int(round(base_amount * size_scale)))
        if size_i <= 0:
            return None
        return VenueOrderPlan(base_amount=base_amount, size_i=size_i, collateral=collateral)

    async def _place_backpack_entry(self, size_i: int) -> Optional[TrackingLimitOrder]:
        attempts = max(int(self.params.backpack_entry_max_attempts or 0), 0) or None
        retry_delay = max(float(self.params.backpack_retry_delay_secs or 0.0), 0.0)
        post_only = bool(self.params.tracking_post_only)
        last_details: Optional[Dict[str, Any]] = None
        attempt = 0
        while True:
            attempt += 1
            try:
                order = await place_tracking_limit_order(
                    self.backpack,
                    symbol=self.params.backpack_symbol,
                    base_amount_i=size_i,
                    is_ask=not self._direction_long,
                    price_offset_ticks=self.params.price_offset_ticks,
                    cancel_wait_secs=self.params.tracking_cancel_wait_secs,
                    post_only=post_only,
                    reduce_only=0,
                    logger=self.log,
                )
            except TrackingLimitTimeoutError as exc:
                self.log.error("backpack tracking order timeout on attempt=%s: %s", attempt, exc)
                order = None
                last_details = {"error": str(exc)}
            except Exception as exc:
                self.log.error("backpack tracking order failed on attempt=%s: %s", attempt, exc)
                order = None
                last_details = {"error": str(exc)}

            if order is None:
                if attempts is not None and attempt >= attempts:
                    break
                if retry_delay:
                    await asyncio.sleep(retry_delay)
                continue

            if order.state == OrderState.FILLED:
                return order

            snapshot = order.snapshot()
            details = snapshot.info or {}
            last_details = details or {"state": order.state.value}
            reason_text = str(details.get("message") or details.get("error") or details.get("code") or "").lower()
            if "immediately match" in reason_text and post_only:
                post_only = False
                self.log.info("disabling post-only after immediate-match rejection")
                if retry_delay:
                    await asyncio.sleep(retry_delay)
                continue

            self.log.warning(
                "backpack tracking order attempt=%s state=%s details=%s",
                attempt,
                order.state.value,
                details,
            )

            if attempts is not None and attempt >= attempts:
                break
            if retry_delay:
                await asyncio.sleep(retry_delay)

        self.log.error("backpack tracking order exhausted attempts=%s last_details=%s", attempts or "unbounded", last_details)
        return None

    async def _submit_lighter_market(
        self,
        *,
        connector: Any,
        size_i: int,
        is_ask: bool,
        reduce_only: int,
        max_slippage: float,
        label: str,
    ) -> bool:
        key = label
        api_key_index = self._api_key_index(connector)
        lock = self._order_locks.setdefault(key, asyncio.Lock())
        max_attempts = max(int(self.params.lighter_hedge_max_attempts or 1), 1)
        retry_delay = max(float(self.params.lighter_retry_delay_secs or 0.0), 0.0)

        for attempt in range(1, max_attempts + 1):
            try:
                async with lock:
                    coi = self._next_coi(key)
                    nonce_state = self._nonce_snapshot(connector)
                    self.log.info(
                        "%s attempt=%s submit coi=%s api_key=%s nonce=%s size_i=%s is_ask=%s reduce_only=%s max_slippage=%.6f",
                        label,
                        attempt,
                        coi,
                        api_key_index,
                        nonce_state,
                        size_i,
                        is_ask,
                        reduce_only,
                        max_slippage,
                    )
                    tracker = await connector.submit_market_order(
                        symbol=self.params.lighter_symbol,
                        client_order_index=coi,
                        base_amount=size_i,
                        is_ask=is_ask,
                        reduce_only=reduce_only,
                        max_slippage=max_slippage if max_slippage > 0 else None,
                    )
                await tracker.wait_final(timeout=30.0)
            except Exception as exc:
                self.log.error("%s market order attempt=%s failed: %s", label, attempt, exc)
                tracker = None

            if tracker and tracker.state == OrderState.FILLED:
                self.log.info("%s attempt=%s filled nonce=%s", label, attempt, self._nonce_snapshot(connector))
                return True

            snapshot = tracker.snapshot() if tracker else None
            info = snapshot.info if snapshot else None
            reason = getattr(tracker, "last_error", None) or getattr(tracker, "reject_reason", None)
            if reason is None and info:
                reason = info.get("error") or info.get("message")
            info_short = self._shorten_info(info)
            nonce_after = self._nonce_snapshot(connector)
            self.log.warning(
                "%s attempt=%s state=%s reason=%s api_key=%s nonce_state=%s info=%s",
                label,
                attempt,
                getattr(tracker.state, "value", tracker.state) if tracker else "error",
                reason,
                api_key_index,
                nonce_after,
                info_short,
            )

            info_code = ""
            if isinstance(info, dict):
                info_code = str(info.get("code") or "")
            reason_text = str(reason or "").lower()
            if "invalid nonce" in reason_text or info_code == "21104":
                await self._refresh_nonce(connector, api_key_index)
                if retry_delay:
                    await asyncio.sleep(retry_delay * max(1, attempt))
                continue

            if attempt < max_attempts and retry_delay:
                await asyncio.sleep(retry_delay)
        return False

    async def _confirm_position(
        self,
        connector: Any,
        baseline: float,
        expected_sign: int,
        *,
        size_i: int,
        size_scale: int,
        label: str,
    ) -> Optional[float]:
        timeout = max(float(self.params.confirmation_timeout_secs or 0.0), 1.0)
        poll = max(float(self.params.confirmation_poll_secs or 0.0), 0.1)
        deadline = time.time() + timeout
        base_size = abs(size_i) / float(size_scale or 1)
        min_change = max(base_size * 0.2, 1e-6)
        self.log.info(
            "%s confirm baseline=%.6f expected_sign=%s min_change=%.8f timeout=%.2fs",
            label,
            baseline,
            expected_sign,
            min_change,
            timeout,
        )
        while time.time() <= deadline:
            pos = await self._get_lighter_position(connector)
            delta = pos - baseline
            if expected_sign > 0 and delta >= min_change:
                self.log.info("%s confirm success pos=%.6f delta=%.6f", label, pos, delta)
                return pos
            if expected_sign < 0 and delta <= -min_change:
                self.log.info("%s confirm success pos=%.6f delta=%.6f", label, pos, delta)
                return pos
            await asyncio.sleep(poll)
        self.log.warning(
            "%s confirm timeout baseline=%.6f last_pos=%.6f min_change=%.6f",
            label,
            baseline,
            await self._get_lighter_position(connector),
            min_change,
        )
        return None

    async def _rebalance_backpack(self, target_base: float) -> bool:
        attempts = max(int(self.params.rebalance_max_attempts or 1), 1)
        retry_delay = max(float(self.params.rebalance_retry_delay_secs or 0.0), 0.0)
        lock = self._order_locks.setdefault("backpack", asyncio.Lock())
        for attempt in range(1, attempts + 1):
            current = await self._get_backpack_position()
            delta = target_base - current
            if abs(delta) <= POSITION_EPS:
                return True
            is_ask = delta < 0
            size_i = max(self._bp_min_size_i, int(round(abs(delta) * self._bp_size_scale)))
            try:
                async with lock:
                    coi = self._next_coi("backpack")
                    tracker = await self.backpack.submit_market_order(
                        symbol=self.params.backpack_symbol,
                        client_order_index=coi,
                        base_amount=size_i,
                        is_ask=is_ask,
                        reduce_only=0,
                    )
                await tracker.wait_final(timeout=30.0)
            except Exception as exc:
                self.log.error("backpack rebalance attempt=%s failed: %s", attempt, exc)
                tracker = None
            if tracker and tracker.state == OrderState.FILLED:
                continue
            if attempt < attempts and retry_delay:
                await asyncio.sleep(retry_delay)
        self.log.error("backpack rebalance unable to reach target=%.8f", target_base)
        return False

    async def _emergency_exit(self, ctx: CycleContext, *, reason: str) -> None:
        self.log.warning("cycle %s emergency exit reason=%s", ctx.cycle_id, reason)
        bp_pos = await self._get_backpack_position()
        if abs(bp_pos) > POSITION_EPS:
            await self._flatten_backpack(int(round(abs(bp_pos) * self._bp_size_scale)), emergency=True)

        for label, connector in (("lighter1", self.lighter1), ("lighter2", self.lighter2)):
            pos = await self._get_lighter_position(connector)
            if abs(pos) <= POSITION_EPS:
                continue
            is_ask = pos > 0
            size_scale = self._lg1_size_scale if connector is self.lighter1 else self._lg2_size_scale
            size_i = int(round(abs(pos) * size_scale))
            await self._submit_lighter_market(
                connector=connector,
                size_i=size_i,
                is_ask=is_ask,
                reduce_only=1,
                max_slippage=self.params.lighter_max_slippage,
                label=label,
            )

        end_bp = await self._fetch_backpack_collateral()
        end_l1 = await self._fetch_lighter_collateral(self.lighter1)
        end_l2 = await self._fetch_lighter_collateral(self.lighter2)

        pnl_bp = end_bp - ctx.start_collateral_bp
        pnl_l1 = end_l1 - ctx.start_collateral_l1
        pnl_l2 = end_l2 - ctx.start_collateral_l2

        msg = (
            f"[LiquidationHedge] 紧急退出 cycle={ctx.cycle_id} reason={reason}\n"
            f"PnL BP={pnl_bp:.4f} L1={pnl_l1:.4f} L2={pnl_l2:.4f}\n"
            f"Positions: bp={await self._get_backpack_position():.6f} "
            f"l1={await self._get_lighter_position(self.lighter1):.6f} "
            f"l2={await self._get_lighter_position(self.lighter2):.6f}"
        )
        await self._send_telegram(msg)

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

    def _resolve_filled(self, tracker: TrackingLimitOrder, size_scale: int, fallback: int) -> int:
        snap = tracker.snapshot()
        if snap.filled_base is not None:
            try:
                amount = abs(float(snap.filled_base))
                amount_i = int(round(amount * size_scale))
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
                        amount_i = int(round(amount * size_scale))
                        if amount_i > 0:
                            return amount_i
                    except Exception:
                        continue
        return fallback

    # --- telemetry & notifications ------------------------------------
    async def _notify_start(self) -> None:
        await self._send_telegram("[LiquidationHedge] 策略启动，开始建立三账户对冲。")

    async def _notify_finished(self) -> None:
        await self._send_telegram("[LiquidationHedge] 策略已结束。")

    async def _load_telemetry_config(self) -> None:
        if not self.params.telemetry_enabled:
            return
        path = Path(self.params.telemetry_config_path or DEFAULT_TELEMETRY_CONFIG)
        if not path.exists():
            self.log.warning("telemetry config %s not found; heartbeat disabled", path)
            return
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            base_url = str(cfg.get("base_url") or "").rstrip('/')
            if not base_url:
                self.log.warning("telemetry config missing base_url; heartbeat disabled")
                return
            bots = cfg.get("bots") or {}
            self._telemetry_cfg = {
                "base_url": base_url,
                "token": cfg.get("token") or '',
                "group": cfg.get("group") or "triad",
                "bots": {
                    "BP": bots.get("BP") or "hedge-bp",
                    "L1": bots.get("L1") or "hedge-l1",
                    "L2": bots.get("L2") or "hedge-l2",
                },
            }
        except Exception as exc:
            self.log.error("failed to load telemetry config %s: %s", path, exc)
            self._telemetry_cfg = None

    def _start_telemetry(self) -> None:
        if not self.params.telemetry_enabled or not self._telemetry_cfg:
            return
        interval = max(float(self.params.telemetry_interval_secs or 0.0), 1.0)
        loop = asyncio.get_event_loop()
        self._telemetry_task = loop.create_task(self._telemetry_loop(interval), name="liquidation_hedge.telemetry")

    async def _stop_telemetry(self) -> None:
        if self._telemetry_task is not None:
            self._telemetry_task.cancel()
            try:
                await self._telemetry_task
            except asyncio.CancelledError:
                pass
            self._telemetry_task = None

    async def _telemetry_loop(self, interval: float) -> None:
        while self._active and self._telemetry_cfg:
            try:
                await self._send_telemetry()
            except Exception as exc:
                self.log.debug("telemetry send failed: %s", exc)
            await asyncio.sleep(interval)

    async def _send_telemetry(self) -> None:
        if not self._telemetry_cfg:
            return
        session = await self._ensure_http_session()
        base_url = self._telemetry_cfg["base_url"]
        token = self._telemetry_cfg.get("token")
        group = self._telemetry_cfg.get("group")
        bots = self._telemetry_cfg.get("bots", {})
        interval = max(float(self.params.telemetry_interval_secs or 0.0), 1.0)

        payloads = await self._collect_telemetry_payloads(group, interval)
        for role, payload in payloads.items():
            bot_name = bots.get(role, f"triad-{role.lower()}")
            url = f"{base_url}/ingest/{bot_name}"
            headers = {"Content-Type": "application/json"}
            if token:
                headers["x-auth-token"] = token
            try:
                async with session.post(url, json=payload, headers=headers, timeout=5) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        self.log.debug("telemetry post %s failed status=%s body=%s", role, resp.status, text[:200])
            except Exception as exc:
                self.log.debug("telemetry post %s error: %s", role, exc)

    async def _collect_telemetry_payloads(self, group: Optional[str], interval: float) -> Dict[str, Dict[str, Any]]:
        timestamp = time.time()
        payloads: Dict[str, Dict[str, Any]] = {}

        bp_pos = await self._get_backpack_position()
        l1_pos = await self._get_lighter_position(self.lighter1)
        l2_pos = await self._get_lighter_position(self.lighter2)

        bp_overview = await self._safe_account_overview(self.backpack)
        l1_overview = await self._safe_account_overview(self.lighter1)
        l2_overview = await self._safe_account_overview(self.lighter2)

        payloads["BP"] = {
            "timestamp": timestamp,
            "telemetry_interval_secs": interval,
            "group": group,
            "role": "BP",
            "symbol": self.params.backpack_symbol,
            "direction": "long" if self._direction_long else "short",
            "position_base": bp_pos,
            "max_position_base": None,
            "pnl_unrealized": None,
            "balance_total": self._extract_balance(bp_overview),
        }
        payloads["L1"] = {
            "timestamp": timestamp,
            "telemetry_interval_secs": interval,
            "group": group,
            "role": "L1",
            "symbol": self.params.lighter_symbol,
            "direction": "short" if self._direction_long else "long",
            "position_base": l1_pos,
            "max_position_base": None,
            "pnl_unrealized": None,
            "balance_total": self._extract_balance(l1_overview),
        }
        payloads["L2"] = {
            "timestamp": timestamp,
            "telemetry_interval_secs": interval,
            "group": group,
            "role": "L2",
            "symbol": self.params.lighter_symbol,
            "direction": "long" if self._direction_long else "short",
            "position_base": l2_pos,
            "max_position_base": None,
            "pnl_unrealized": None,
            "balance_total": self._extract_balance(l2_overview),
        }
        return payloads

    async def _ensure_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def _close_http_session(self) -> None:
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

    async def _send_telegram(self, text: str) -> None:
        if not self.params.telegram_enabled or not text:
            return
        await self._load_telegram_keys()
        if not self._tg_token or not self._tg_chat_id:
            return
        session = await self._ensure_http_session()
        url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
        payload = {"chat_id": self._tg_chat_id, "text": text}
        try:
            async with session.post(url, json=payload, timeout=10) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    self.log.debug("telegram notify failed status=%s body=%s", resp.status, body[:200])
        except Exception as exc:
            self.log.debug("telegram notify error: %s", exc)

    async def _load_telegram_keys(self) -> None:
        if self._tg_token and self._tg_chat_id:
            return
        path = Path(self.params.telegram_keys_path or "tg_key.txt")
        if not path.exists():
            self.log.debug("telegram key file %s missing", path)
            return
        try:
            data: Dict[str, str] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                data[key.strip()] = value.strip().strip('"')
            self._tg_token = data.get("bot_token")
            self._tg_chat_id = data.get("chat_id")
        except Exception as exc:
            self.log.debug("unable to load telegram keys: %s", exc)

    # --- utility methods ----------------------------------------------
    def _next_coi(self, key: str) -> int:
        seq = self._coi_seq.get(key, 1)
        seq += 1
        if seq > COI_LIMIT:
            seq = 1
        self._coi_seq[key] = seq
        return seq

    def _api_key_index(self, connector: Any) -> Optional[int]:
        signer = getattr(connector, "signer", None)
        for attr in ("api_key_index", "apiKeyIndex", "api_key_id"):
            if getattr(signer, attr, None) is not None:
                try:
                    return int(getattr(signer, attr))
                except Exception:
                    return None
        value = getattr(connector, "_api_key_index", None)
        if value is not None:
            try:
                return int(value)
            except Exception:
                return None
        return None

    def _nonce_snapshot(self, connector: Any) -> Optional[str]:
        signer = getattr(connector, "signer", None)
        manager = getattr(signer, "nonce_manager", None)
        mapping = getattr(manager, "nonce", None)
        if isinstance(mapping, dict) and mapping:
            try:
                return ",".join(f"{k}:{mapping[k]}" for k in sorted(mapping))
            except Exception:
                return str(mapping)
        return None

    async def _refresh_nonce(self, connector: Any, api_key_index: Optional[int]) -> None:
        signer = getattr(connector, "signer", None)
        manager = getattr(signer, "nonce_manager", None)
        if manager is None or api_key_index is None:
            return
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, manager.hard_refresh_nonce, api_key_index)
            self.log.debug(
                "%s refreshed nonce api_key=%s nonce_state=%s",
                getattr(connector, "name", "lighter"),
                api_key_index,
                self._nonce_snapshot(connector),
            )
        except Exception as exc:
            self.log.warning("%s nonce refresh failed api_key=%s error=%s", getattr(connector, "name", "lighter"), api_key_index, exc)

    async def _snapshot_positions(self) -> PositionSnapshot:
        bp = await self._get_backpack_position()
        l1 = await self._get_lighter_position(self.lighter1)
        l2 = await self._get_lighter_position(self.lighter2)
        return PositionSnapshot(bp=bp, l1=l1, l2=l2)

    async def _get_backpack_position(self) -> float:
        try:
            positions = await self.backpack.get_positions()
        except Exception:
            return 0.0
        key = self._symbol_key(self.params.backpack_symbol)
        for entry in positions:
            sym = self._symbol_key(entry.get("symbol"))
            if sym == key:
                try:
                    return float(entry.get("position") or entry.get("size") or 0.0)
                except Exception:
                    return 0.0
        return 0.0

    async def _get_lighter_position(self, connector: Any) -> float:
        try:
            positions = await connector.get_positions()
        except Exception:
            return 0.0
        key = self._symbol_key(self.params.lighter_symbol)
        for entry in positions:
            sym = self._symbol_key(entry.get("symbol"))
            if sym == key:
                try:
                    size = float(entry.get("position") or entry.get("netQuantity") or 0.0)
                    sign = entry.get("sign")
                    if sign is not None:
                        size *= float(sign)
                    return size
                except Exception:
                    return 0.0
        return 0.0

    async def _fetch_backpack_collateral(self) -> float:
        try:
            data = await self.backpack.get_collateral()
            if isinstance(data, dict):
                for key in ("collateral", "totalCollateral", "effectiveCollateral", "equity"):
                    if data.get(key) is not None:
                        return float(data[key])
        except Exception:
            pass
        return 0.0

    async def _fetch_lighter_collateral(self, connector: Any) -> float:
        try:
            overview = await connector.get_account_overview()
        except Exception:
            return 0.0
        accounts = overview.get("accounts") if isinstance(overview, dict) else None
        if not accounts:
            return 0.0
        account = accounts[0]
        for key in ("collateral", "available_balance", "balance", "equity"):
            if account.get(key) is not None:
                try:
                    return float(account.get(key))
                except Exception:
                    continue
        return 0.0

    async def _safe_account_overview(self, connector: Any) -> Optional[Dict[str, Any]]:
        try:
            data = await connector.get_account_overview()
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _extract_balance(self, overview: Optional[Dict[str, Any]]) -> Optional[float]:
        if not overview:
            return None
        accounts = overview.get("accounts")
        if isinstance(accounts, list) and accounts:
            acc = accounts[0]
            for key in ("equity", "collateral", "available_balance", "balance"):
                if acc.get(key) is not None:
                    try:
                        return float(acc.get(key))
                    except Exception:
                        continue
        return None

    @staticmethod
    def _symbol_key(symbol: Any) -> str:
        text = str(symbol or "").upper()
        return "".join(ch for ch in text if ch.isalnum())

    def _shorten_info(self, info: Optional[Dict[str, Any]]) -> Optional[str]:
        if not info:
            return None
        try:
            return str(info)[:200]
        except Exception:
            return None

__all__ = ["LiquidationHedgeStrategy", "LiquidationHedgeParams"]
