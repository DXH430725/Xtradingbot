from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from mm_bot.execution import ExecutionLayer, resolve_filled_amount, wait_random
from mm_bot.execution.orders import OrderState, TrackingLimitOrder
from mm_bot.strategy.strategy_base import StrategyBase

MARGIN_POLL_SECS = 60.0
DEFAULT_CANCEL_WAIT_SECS = 2.0


@dataclass
class SimpleHedgeParams:
    backpack_symbol: str = "ETH_USDC_PERP"
    lighter_symbol: str = "ETH"
    direction: str = "long_backpack"  # or "short_backpack"
    leverage: float = 10.0
    tracking_interval_secs: float = 10.0
    tracking_timeout_secs: float = 180.0
    price_offset_ticks: int = 0
    hold_min_secs: float = 3600.0
    hold_max_secs: float = 7200.0
    cooldown_min_secs: float = 600.0
    cooldown_max_secs: float = 1200.0
    margin_threshold: float = 30.0
    telegram_enabled: bool = True
    telegram_keys_path: str = "tg_key.txt"
    test_mode: bool = False


@dataclass
class CycleContext:
    cycle_id: str
    entry_size_i: int = 0
    entry_base: float = 0.0
    lighter_fill_i: int = 0
    direction_long: bool = True


class SimpleBackpackLighterHedgeStrategy(StrategyBase):
    """Simple Backpack/Lighter hedge loop with optional test mode."""

    def __init__(
        self,
        *,
        backpack_connector: Any,
        lighter_connector: Any,
        params: Optional[SimpleHedgeParams] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if backpack_connector is None or lighter_connector is None:
            raise ValueError("Backpack and Lighter connectors are required")
        self.backpack = backpack_connector
        self.lighter = lighter_connector
        self.params = params or SimpleHedgeParams()
        self.log = logger or logging.getLogger("mm_bot.strategy.simple_hedge")

        self.execution = ExecutionLayer(logger=self.log)
        self.execution.register_connector("backpack", self.backpack)
        self.execution.register_connector("lighter", self.lighter)
        self.execution.register_symbol(
            "HEDGE",
            backpack=self.params.backpack_symbol,
            lighter=self.params.lighter_symbol,
        )

        self._rng = random.Random()
        self._started = False
        self._task: Optional[asyncio.Task] = None
        self._active = True

        self._bp_size_scale: int = 1
        self._bp_min_size_i: int = 1
        self._lighter_size_scale: int = 1
        self._lighter_min_size_i: int = 1
        self._test_mode_sizes: Dict[str, int] = {}

        self._tg_token: Optional[str] = None
        self._tg_chat_id: Optional[str] = None
        self._http_session: Optional[Any] = None

    # ------------------------------------------------------------------
    def start(self, core: Any) -> None:
        if self._started:
            return
        self._active = True
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._run(), name="simple_hedge.run")
        self._started = True

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
            await self._prepare()
            cycle_index = 0
            while self._active:
                ctx = CycleContext(
                    cycle_id=f"{int(time.time())}-{cycle_index}",
                    direction_long=self.params.direction.lower() != "short_backpack",
                )
                success = await self._execute_cycle(ctx)
                if not success:
                    break
                cycle_index += 1
                if not self._active:
                    break
                await self._cooldown_wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.log.exception("simple hedge strategy crashed")
            await self._send_telegram("[SimpleHedge] 策略异常退出，请检查日志。")
        finally:
            await self._close_http_session()

    async def _prepare(self) -> None:
        await self.backpack.start_ws_state([self.params.backpack_symbol])
        await self.lighter.start_ws_state()

        self._bp_size_scale = await self.execution.size_scale("backpack", "HEDGE")
        self._bp_min_size_i = await self.execution.min_size_i("backpack", "HEDGE")
        self._lighter_size_scale = await self.execution.size_scale("lighter", "HEDGE")
        self._lighter_min_size_i = await self.execution.min_size_i("lighter", "HEDGE")

        if self.params.test_mode:
            base_min = max(self._bp_min_size_i, self._lighter_min_size_i)

            def align(origin: int) -> int:
                return max(origin, ((base_min + origin - 1) // origin) * origin)

            self._test_mode_sizes = {
                "backpack": align(self._bp_min_size_i),
                "lighter": align(self._lighter_min_size_i),
            }
        else:
            self._test_mode_sizes = {}

    async def _execute_cycle(self, ctx: CycleContext) -> bool:
        if not await self._enter_positions(ctx):
            return False
        if not await self._hold_positions(ctx):
            return False
        return await self._exit_positions(ctx)

    async def _enter_positions(self, ctx: CycleContext) -> bool:
        plan = await self._plan_entry_size()
        if plan is None:
            return False
        ctx.entry_size_i = plan
        is_ask = not ctx.direction_long

        self.log.info(
            "cycle %s entering backpack size_i=%s side=%s", ctx.cycle_id, ctx.entry_size_i, "sell" if is_ask else "buy"
        )
        try:
            limit_tracker = await self.execution.limit_order(
                "backpack",
                "HEDGE",
                base_amount_i=ctx.entry_size_i,
                is_ask=is_ask,
                interval_secs=max(self.params.tracking_interval_secs, 1.0),
                timeout_secs=max(self.params.tracking_timeout_secs, 10.0),
                price_offset_ticks=self.params.price_offset_ticks,
                cancel_wait_secs=DEFAULT_CANCEL_WAIT_SECS,
                reduce_only=0,
            )
        except Exception as exc:
            self.log.error("cycle %s backpack limit failed: %s", ctx.cycle_id, exc)
            return False

        if not await self._ensure_limit_success(limit_tracker, ctx):
            return False

        self.log.info("cycle %s hedge lighter market size_i=%s", ctx.cycle_id, ctx.entry_size_i)
        tracker = await self.execution.market_order(
            "lighter",
            "HEDGE",
            size_i=ctx.entry_size_i,
            is_ask=ctx.direction_long,
            reduce_only=0,
            max_slippage=None,
            wait_timeout=30.0,
        )
        if tracker is None or tracker.state != OrderState.FILLED:
            self.log.error("cycle %s lighter hedge failed state=%s", ctx.cycle_id, tracker.state if tracker else None)
            await self.execution.unwind_all(canonical_symbol="HEDGE")
            return False
        ctx.lighter_fill_i = ctx.entry_size_i
        await self._send_telegram(f"[SimpleHedge] 周期 {ctx.cycle_id} 开仓完成 size={ctx.entry_size_i}")
        return True

    async def _hold_positions(self, ctx: CycleContext) -> bool:
        hold_secs = self._rng.uniform(self.params.hold_min_secs, self.params.hold_max_secs)
        end_ts = time.time() + hold_secs
        self.log.info("cycle %s holding for %.0fs", ctx.cycle_id, hold_secs)
        while self._active and time.time() < end_ts:
            if await self._check_margin(ctx):
                return False
            await asyncio.sleep(min(MARGIN_POLL_SECS, max(0.0, end_ts - time.time())))
        return self._active

    async def _exit_positions(self, ctx: CycleContext) -> bool:
        is_ask_exit = ctx.direction_long
        self.log.info("cycle %s exiting backpack via limit", ctx.cycle_id)
        try:
            limit_tracker = await self.execution.limit_order(
                "backpack",
                "HEDGE",
                base_amount_i=ctx.entry_size_i,
                is_ask=is_ask_exit,
                interval_secs=max(self.params.tracking_interval_secs, 1.0),
                timeout_secs=max(self.params.tracking_timeout_secs, 10.0),
                price_offset_ticks=self.params.price_offset_ticks,
                cancel_wait_secs=DEFAULT_CANCEL_WAIT_SECS,
                reduce_only=1,
            )
        except Exception as exc:
            self.log.error("cycle %s exit limit failed: %s", ctx.cycle_id, exc)
            await self.execution.unwind_all(canonical_symbol="HEDGE")
            return False

        if limit_tracker.state != OrderState.FILLED:
            await self.execution.unwind_all(canonical_symbol="HEDGE")
            return False

        filled_i = resolve_filled_amount(limit_tracker, size_scale=self._bp_size_scale, fallback=ctx.entry_size_i)
        self.log.info("cycle %s exit lighter market size_i=%s", ctx.cycle_id, filled_i)
        tracker = await self.execution.market_order(
            "lighter",
            "HEDGE",
            size_i=filled_i,
            is_ask=not ctx.direction_long,
            reduce_only=1,
            wait_timeout=30.0,
        )
        if tracker is None or tracker.state != OrderState.FILLED:
            await self.execution.unwind_all(canonical_symbol="HEDGE")
            return False
        await self._send_telegram(f"[SimpleHedge] 周期 {ctx.cycle_id} 平仓完成")
        return True

    async def _cooldown_wait(self) -> None:
        wait = await wait_random(self.params.cooldown_min_secs, self.params.cooldown_max_secs, rng=self._rng)
        self.log.info("cooldown for %.0fs before next cycle", wait)

    async def _plan_entry_size(self) -> Optional[int]:
        if self.params.test_mode:
            return self._test_mode_sizes.get("backpack", self._bp_min_size_i)
        plan = await self.execution.plan_order_size(
            "lighter",
            "HEDGE",
            leverage=self.params.leverage,
            min_collateral=0.0,
            collateral_buffer=0.96,
        )
        if not plan:
            self.log.error("unable to compute order size from execution layer")
            return None
        base_amount = float(plan["base_amount"])
        size_i = max(self._bp_min_size_i, int(round(base_amount * self._bp_size_scale)))
        return size_i

    async def _ensure_limit_success(self, tracker: TrackingLimitOrder, ctx: CycleContext) -> bool:
        if tracker.state != OrderState.FILLED:
            self.log.error("cycle %s limit state=%s", ctx.cycle_id, tracker.state.value)
            return False
        ctx.entry_size_i = resolve_filled_amount(tracker, size_scale=self._bp_size_scale, fallback=ctx.entry_size_i)
        ctx.entry_base = ctx.entry_size_i / float(self._bp_size_scale or 1)
        return ctx.entry_size_i > 0

    async def _check_margin(self, ctx: CycleContext) -> bool:
        bp_collateral = await self.execution.collateral("backpack")
        lighter_collateral = await self.execution.collateral("lighter")
        self.log.debug(
            "cycle %s collateral backpack=%.3f lighter=%.3f", ctx.cycle_id, bp_collateral, lighter_collateral
        )
        if bp_collateral <= self.params.margin_threshold or lighter_collateral <= self.params.margin_threshold:
            await self._send_telegram(
                f"[SimpleHedge] 保证金低于阈值，触发紧急退出 bp={bp_collateral:.2f} lighter={lighter_collateral:.2f}"
            )
            await self.execution.unwind_all(canonical_symbol="HEDGE")
            return True
        return False

    async def _send_telegram(self, message: str) -> None:
        if not self.params.telegram_enabled:
            return
        await self._ensure_telegram()
        if not self._tg_token or not self._tg_chat_id:
            return
        session = await self._ensure_http_session()
        url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
        try:
            async with session.post(url, json={"chat_id": self._tg_chat_id, "text": message}, timeout=5) as resp:
                if resp.status >= 400:
                    self.log.debug("telegram send failed status=%s", resp.status)
        except Exception as exc:
            self.log.debug("telegram send error: %s", exc)

    async def _ensure_http_session(self) -> Any:
        if self._http_session is None:
            import aiohttp  # local import to avoid hard dependency if unused

            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def _close_http_session(self) -> None:
        if self._http_session:
            await self._http_session.close()
            self._http_session = None

    async def _ensure_telegram(self) -> None:
        if self._tg_token and self._tg_chat_id:
            return
        path = self.params.telegram_keys_path
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or ":" not in line:
                        continue
                    key, value = line.split(":", 1)
                    key = key.strip().lower()
                    value = value.strip().strip('"')
                    if key in {"bot_token", "token"}:
                        self._tg_token = value
                    elif key in {"chat_id", "channel"}:
                        self._tg_chat_id = value
        except FileNotFoundError:
            self.log.warning("telegram key file %s missing", path)
        except Exception as exc:
            self.log.warning("telegram key load error: %s", exc)


__all__ = ["SimpleBackpackLighterHedgeStrategy", "SimpleHedgeParams"]
