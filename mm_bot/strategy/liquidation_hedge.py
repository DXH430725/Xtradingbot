import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp

from mm_bot.execution import ExecutionLayer, resolve_filled_amount, wait_random
from mm_bot.execution.orders import OrderState, TrackingMarketOrder
from mm_bot.strategy.strategy_base import StrategyBase

POSITION_EPS = 1e-9
COI_LIMIT_LIGHTER = 281_474_976_710_655  # 2**48 - 1
COI_LIMIT_BACKPACK = 4_294_967_295       # 2**32 - 1
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
    test_mode: bool = False
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
        self._bp_size_scale: int = 1
        self._bp_min_size_i: int = 1
        self._lg1_size_scale: int = 1
        self._lg1_min_size_i: int = 1
        self._lg2_size_scale: int = 1
        self._lg2_min_size_i: int = 1

        # runtime state
        self._direction_long: bool = self.params.direction.lower() != "short_backpack"
        self._cycle_index: int = 0
        self._rng = random.Random()

        # execution layer abstraction
        self._canonical_symbol = "HEDGE"
        self.execution = ExecutionLayer(logger=self.log)
        self.execution.register_connector("backpack", self.backpack, coi_limit=COI_LIMIT_BACKPACK)
        self.execution.register_connector("lighter1", self.lighter1, coi_limit=COI_LIMIT_LIGHTER)
        self.execution.register_connector("lighter2", self.lighter2, coi_limit=COI_LIMIT_LIGHTER)
        self.execution.register_symbol(
            self._canonical_symbol,
            backpack=self.params.backpack_symbol,
            lighter1=self.params.lighter_symbol,
            lighter2=self.params.lighter_symbol,
        )
        self._test_mode_sizes: Dict[str, int] = {}

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
        await self.lighter1.start_ws_state()
        await self.lighter2.start_ws_state()

        self._bp_size_scale = await self.execution.size_scale("backpack", self._canonical_symbol)
        self._bp_min_size_i = await self.execution.min_size_i("backpack", self._canonical_symbol)
        self._lg1_size_scale = await self.execution.size_scale("lighter1", self._canonical_symbol)
        self._lg1_min_size_i = await self.execution.min_size_i("lighter1", self._canonical_symbol)
        self._lg2_size_scale = await self.execution.size_scale("lighter2", self._canonical_symbol)
        self._lg2_min_size_i = await self.execution.min_size_i("lighter2", self._canonical_symbol)

        if self.params.test_mode:
            base_min = max(self._bp_min_size_i, self._lg1_min_size_i, self._lg2_min_size_i)
            def align(min_size: int) -> int:
                return max(min_size, ((base_min + min_size - 1) // min_size) * min_size)

            self._test_mode_sizes = {
                "backpack": align(self._bp_min_size_i),
                "lighter1": align(self._lg1_min_size_i),
                "lighter2": align(self._lg2_min_size_i),
            }
        else:
            self._test_mode_sizes = {}

    # ------------------------------------------------------------------
    async def _execute_cycle(self) -> bool:
        ctx = CycleContext(cycle_id=f"{int(time.time() * 1000)}-{self._cycle_index}")
        ctx.start_snapshot = await self._snapshot_positions()
        ctx.start_collateral_bp = await self._fetch_backpack_collateral()
        ctx.start_collateral_l1 = await self._fetch_lighter_collateral("lighter1")
        ctx.start_collateral_l2 = await self._fetch_lighter_collateral("lighter2")

        try:
            override_l1 = self._test_mode_sizes.get("lighter1") if self.params.test_mode else None
            override_l2 = self._test_mode_sizes.get("lighter2") if self.params.test_mode else None
            ctx.l1_plan = await self._plan_lighter_size("lighter1", override_size_i=override_l1)
            ctx.l2_plan = await self._plan_lighter_size("lighter2", override_size_i=override_l2)
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
        if self.params.test_mode:
            ctx.bp_entry_size_i = self._test_mode_sizes.get("backpack", self._bp_min_size_i)
        else:
            size_base = ctx.l1_plan.base_amount if ctx.l1_plan else 0.0
            ctx.bp_entry_size_i = max(self._bp_min_size_i, int(round(size_base * self._bp_size_scale)))

        tracker = await self._submit_backpack_market(
            size_i=ctx.bp_entry_size_i,
            is_ask=not self._direction_long,
            reduce_only=0,
            label="bp_entry",
        )
        if tracker is None:
            return False
        ctx.bp_filled_i = resolve_filled_amount(tracker, size_scale=self._bp_size_scale, fallback=ctx.bp_entry_size_i)
        if ctx.bp_filled_i <= 0:
            self.log.error("unable to resolve filled amount; aborting")
            return False
        ctx.bp_entry_base = ctx.bp_filled_i / float(self._bp_size_scale)
        if self.params.test_mode:
            ctx.l1_size_i = self._test_mode_sizes.get("lighter1", self._lg1_min_size_i)
        else:
            ctx.l1_size_i = max(self._lg1_min_size_i, int(round(ctx.bp_entry_base * self._lg1_size_scale)))
        self.log.info("cycle %s backpack filled base=%.6f size_i=%s", ctx.cycle_id, ctx.bp_entry_base, ctx.bp_filled_i)
        return True

    async def _stage_l1_hedge(self, ctx: CycleContext) -> bool:
        is_ask = self._direction_long
        size_i = max(ctx.l1_size_i, self._lg1_min_size_i)
        success = await self._submit_lighter_market(
            venue="lighter1",
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
            venue="lighter1",
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
        delay = await wait_random(max(0.0, wait_min), wait_max, rng=self._rng)
        self.log.info("waiting %.2fs before lighter2 entry", delay)

    async def _stage_l2_entry(self, ctx: CycleContext) -> bool:
        if self.params.test_mode:
            size_i = self._test_mode_sizes.get("lighter2", self._lg2_min_size_i)
        else:
            size_i = max(self._lg2_min_size_i, int(round(ctx.bp_entry_base * self._lg2_size_scale)))
        success = await self._submit_lighter_market(
            venue="lighter2",
            size_i=size_i,
            is_ask=not self._direction_long,
            reduce_only=0,
            max_slippage=self.params.lighter_max_slippage,
            label="lighter2",
        )
        if not success:
            return False
        pos = await self._confirm_position(
            venue="lighter2",
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
        l1_pos = await self._get_lighter_position("lighter1")
        l2_pos = await self._get_lighter_position("lighter2")
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

                l1_pos = await self._get_lighter_position("lighter1")
                l2_pos = await self._get_lighter_position("lighter2")
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
    async def _plan_lighter_size(self, venue: str, *, override_size_i: Optional[int] = None) -> Optional[VenueOrderPlan]:
        if self.params.test_mode and override_size_i is not None:
            collateral = await self._fetch_lighter_collateral(venue)
            size_scale = self._lg1_size_scale if venue == "lighter1" else self._lg2_size_scale
            base_amount = override_size_i / float(size_scale or 1)
            return VenueOrderPlan(base_amount=base_amount, size_i=override_size_i, collateral=collateral)

        plan = await self.execution.plan_order_size(
            venue,
            self._canonical_symbol,
            leverage=self.params.leverage,
            min_collateral=self.params.min_collateral,
            collateral_buffer=0.96,
        )
        if not plan:
            self.log.error("%s collateral/market data unavailable", venue)
            return None
        return VenueOrderPlan(
            base_amount=plan["base_amount"],
            size_i=plan["size_i"],
            collateral=plan["collateral"],
        )

    async def _submit_lighter_market(
        self,
        *,
        venue: str,
        size_i: int,
        is_ask: bool,
        reduce_only: int,
        max_slippage: float,
        label: str,
    ) -> bool:
        tracker = await self.execution.market_order(
            venue,
            self._canonical_symbol,
            size_i=size_i,
            is_ask=is_ask,
            reduce_only=reduce_only,
            max_slippage=max_slippage if max_slippage > 0 else None,
            attempts=int(self.params.lighter_hedge_max_attempts or 1),
            retry_delay=float(self.params.lighter_retry_delay_secs or 0.0),
            wait_timeout=30.0,
            label=label,
        )
        return bool(tracker and tracker.state == OrderState.FILLED)

    async def _submit_backpack_market(
        self,
        *,
        size_i: int,
        is_ask: bool,
        reduce_only: int,
        label: str,
        attempts: Optional[int] = None,
        retry_delay: Optional[float] = None,
        log_errors: bool = True,
    ) -> Optional[TrackingMarketOrder]:
        if size_i <= 0:
            return None
        attempts_val = int(attempts or self.params.backpack_entry_max_attempts or 1)
        retry = float(retry_delay if retry_delay is not None else self.params.backpack_retry_delay_secs or 0.0)
        tracker = await self.execution.market_order(
            "backpack",
            self._canonical_symbol,
            size_i=size_i,
            is_ask=is_ask,
            reduce_only=reduce_only,
            attempts=max(attempts_val, 1),
            retry_delay=retry,
            wait_timeout=30.0,
            label=label,
        )
        if tracker and tracker.state == OrderState.FILLED:
            if log_errors:
                self.log.info("%s filled", label)
            return tracker
        if log_errors and tracker is not None:
            info = tracker.snapshot().info if tracker else None
            self.log.warning(
                "%s ended state=%s info=%s",
                label,
                tracker.state.value,
                self._shorten_info(info),
            )
        return tracker

    async def _confirm_position(
        self,
        venue: str,
        baseline: float,
        expected_sign: int,
        *,
        size_i: int,
        size_scale: int,
        label: str,
    ) -> Optional[float]:
        timeout = max(float(self.params.confirmation_timeout_secs or 0.0), 1.0)
        poll = max(float(self.params.confirmation_poll_secs or 0.0), 0.1)
        base_size = abs(size_i) / float(size_scale or 1)
        tolerance = max(base_size * 0.2, 1e-6)
        target = baseline + (expected_sign * base_size)
        self.log.info(
            "%s confirm target=%.6f baseline=%.6f tolerance=%.8f",
            label,
            target,
            baseline,
            tolerance,
        )
        result = await self.execution.confirm_position(
            venue,
            self._canonical_symbol,
            target=target,
            tolerance=tolerance,
            timeout=timeout,
            poll_interval=poll,
        )
        if result is not None:
            self.log.info("%s confirm success pos=%.6f", label, result)
            return result
        last_pos = await self._get_lighter_position(venue)
        self.log.warning(
            "%s confirm timeout baseline=%.6f last_pos=%.6f tolerance=%.6f",
            label,
            baseline,
            last_pos,
            tolerance,
        )
        return None

    async def _rebalance_backpack(self, target_base: float) -> bool:
        return await self.execution.rebalance(
            "backpack",
            self._canonical_symbol,
            target=target_base,
            tolerance=POSITION_EPS,
            attempts=self.params.rebalance_max_attempts or 1,
            retry_delay=self.params.rebalance_retry_delay_secs or 0.0,
        )

    async def _emergency_exit(self, ctx: CycleContext, *, reason: str) -> None:
        self.log.warning("cycle %s emergency exit reason=%s", ctx.cycle_id, reason)
        await self.execution.unwind_all(canonical_symbol=self._canonical_symbol, tolerance=POSITION_EPS)

        end_bp = await self._fetch_backpack_collateral()
        end_l1 = await self._fetch_lighter_collateral("lighter1")
        end_l2 = await self._fetch_lighter_collateral("lighter2")

        pnl_bp = end_bp - ctx.start_collateral_bp
        pnl_l1 = end_l1 - ctx.start_collateral_l1
        pnl_l2 = end_l2 - ctx.start_collateral_l2

        msg = (
            f"[LiquidationHedge] 紧急退出 cycle={ctx.cycle_id} reason={reason}\n"
            f"PnL BP={pnl_bp:.4f} L1={pnl_l1:.4f} L2={pnl_l2:.4f}\n"
            f"Positions: bp={await self._get_backpack_position():.6f} "
            f"l1={await self._get_lighter_position('lighter1'):.6f} "
            f"l2={await self._get_lighter_position('lighter2'):.6f}"
        )
        await self._send_telegram(msg)

    async def _flatten_backpack(self, size_i: int, emergency: bool = False) -> None:
        if size_i <= 0:
            return
        tracker = await self._submit_backpack_market(
            size_i=size_i,
            is_ask=self._direction_long,
            reduce_only=1,
            label="bp_flatten",
            attempts=self.params.backpack_entry_max_attempts,
            retry_delay=self.params.backpack_retry_delay_secs,
            log_errors=emergency,
        )
        if tracker is None:
            self.log.error("flatten backpack failed size_i=%s", size_i)

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
        l1_pos = await self._get_lighter_position("lighter1")
        l2_pos = await self._get_lighter_position("lighter2")

        bp_overview = await self._safe_account_overview("backpack")
        l1_overview = await self._safe_account_overview("lighter1")
        l2_overview = await self._safe_account_overview("lighter2")

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
    async def _snapshot_positions(self) -> PositionSnapshot:
        bp = await self._get_backpack_position()
        l1 = await self._get_lighter_position("lighter1")
        l2 = await self._get_lighter_position("lighter2")
        return PositionSnapshot(bp=bp, l1=l1, l2=l2)

    async def _get_backpack_position(self) -> float:
        return await self.execution.position("backpack", self._canonical_symbol)

    async def _get_lighter_position(self, venue: str) -> float:
        return await self.execution.position(venue, self._canonical_symbol)

    async def _fetch_backpack_collateral(self) -> float:
        return await self.execution.collateral("backpack")

    async def _fetch_lighter_collateral(self, venue: str) -> float:
        return await self.execution.collateral(venue)

    async def _safe_account_overview(self, venue: str) -> Optional[Dict[str, Any]]:
        connector = self.execution.connectors.get(venue.lower())
        if connector is None:
            return None
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
