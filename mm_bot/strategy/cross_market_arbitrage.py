import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .strategy_base import StrategyBase
from .arb.order_exec import (
    LegExecutionResult,
    LegOrder,
    OrderTracker,
    TrackingLimitExecutor,
    parse_backpack_event,
    parse_lighter_event,
)
from .arb.pairing import discover_pairs, pick_common_sizes


@dataclass
class CrossArbParams:
    # entry settings
    entry_threshold_pct: float = 0.20  # percent
    max_concurrent_positions: int = 3
    cooldown_secs: float = 3.0
    # exit settings
    tp_ratio: float = 1.0  # r_tp; exit when spread contracts to (1-r_tp)*entry
    sl_ratio: float = 1.0  # r_sl; stop when spread expands to (1+r_sl)*entry
    max_hold_secs: float = 600.0  # force-close timeout
    # risk and ops
    maintenance_hour_utc: Optional[int] = None  # if set, block new entries during [h,h+1)
    maintenance_local_hour: Optional[int] = 23  # local maintenance window (default 23:00-24:00)
    pre_maint_close_minutes: int = 10
    # polling
    poll_interval_ms: int = 250
    latency_circuit_ms: float = 1500.0
    # delta guard
    delta_tolerance: float = 0.01
    max_delta_failures: int = 3
    # tracking limit controls
    tracking_wait_secs: float = 10.0
    tracking_max_retries: int = 3
    allow_market_fallback: bool = True
    debug_run_once: bool = False
    lighter_market_execution: bool = True


class CrossMarketArbitrageStrategy(StrategyBase):
    def __init__(
        self,
        lighter_connector: Any,
        backpack_connector: Any,
        params: Optional[CrossArbParams] = None,
        symbol_filters: Optional[List[str]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.lighter = lighter_connector
        self.backpack = backpack_connector
        self.params = params or CrossArbParams()
        self.symbol_filters = [s.upper() for s in (symbol_filters or [])]
        self.log = logger or logging.getLogger("mm_bot.strategy.cross_arb")
        self.core = None

        self._pairs: List[Tuple[str, str]] = []  # (bp_sym, lg_sym)
        # state: key by (bp_sym, lg_sym)
        self._open: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._last_trade_at: float = 0.0

        # internal pacing
        self._last_poll_ms: float = 0.0
        seed = int(time.time() * 1000) % 1_000_000
        self._cio_counter: int = seed if seed != 0 else 1
        self._pair_stats: Dict[str, int] = {"lighter": 0, "backpack": 0, "overlap": 0}
        self._last_pair_digest: Optional[Tuple[int, int, int]] = None
        self._delta_failure_count: int = 0
        self._suspend_trading: bool = False
        self._size_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._bp_tracker: Optional[OrderTracker] = None
        self._lg_tracker: Optional[OrderTracker] = None
        self._executors: Dict[str, TrackingLimitExecutor] = {}
        self._pair_subscriptions_started: bool = False
        self._positions_cache: Dict[str, Dict[str, Any]] = {"backpack": {}, "lighter": {}}
        self._debug_run_once: bool = bool(getattr(self.params, "debug_run_once", False))
        self._debug_shutdown_triggered: bool = False

    def start(self, core: Any) -> None:
        self.core = core
        # connectors should already be started by core
        self._bp_tracker = OrderTracker("backpack", parse_backpack_event, logger=self.log.getChild("bp_tracker"))
        self._lg_tracker = OrderTracker("lighter", parse_lighter_event, logger=self.log.getChild("lg_tracker"))

        self.backpack.set_event_handlers(
            on_order_filled=self._on_backpack_order_event,
            on_order_cancelled=self._on_backpack_order_event,
            on_trade=self._on_backpack_trade,
            on_position_update=self._on_backpack_position,
        )
        self.lighter.set_event_handlers(
            on_order_filled=self._on_lighter_order_event,
            on_order_cancelled=self._on_lighter_order_event,
            on_trade=self._on_lighter_trade,
            on_position_update=self._on_lighter_position,
        )

        self._executors["backpack"] = TrackingLimitExecutor(
            venue="backpack",
            connector=self.backpack,
            order_tracker=self._bp_tracker,
            next_client_order_index=self._next_client_order_index,
            top_of_book_fetcher=self.backpack.get_top_of_book,
            cancel_by_coi=self._cancel_backpack_order,
            logger=self.log.getChild("exec.backpack"),
        )
        self._executors["lighter"] = TrackingLimitExecutor(
            venue="lighter",
            connector=self.lighter,
            order_tracker=self._lg_tracker,
            next_client_order_index=self._next_client_order_index,
            top_of_book_fetcher=self.lighter.get_top_of_book,
            cancel_by_coi=self._cancel_lighter_order,
            logger=self.log.getChild("exec.lighter"),
        )

        self.log.info("CrossArb strategy started")

    def stop(self) -> None:
        self.log.info("CrossArb strategy stopped")
        if self._bp_tracker:
            self._bp_tracker.clear()
        if self._lg_tracker:
            self._lg_tracker.clear()
        try:
            asyncio.create_task(self._flatten_all_positions())
        except RuntimeError:
            # not in event loop; best effort synchronous schedule
            pass

    async def _cancel_backpack_order(self, client_order_index: int, symbol: str):
        try:
            return await self.backpack.cancel_by_client_id(symbol, client_order_index)
        except Exception as exc:
            self.log.debug(f"backpack cancel_by_client_id failed coi={client_order_index}: {exc}")
            return None, None, str(exc)

    async def _cancel_lighter_order(self, client_order_index: int, symbol: str):
        try:
            return await self.lighter.cancel_by_client_order_index(client_order_index, symbol=symbol)
        except Exception as exc:
            self.log.debug(f"lighter cancel_by_client_order_index failed coi={client_order_index}: {exc}")
            return None, None, str(exc)

    def _on_backpack_order_event(self, payload: Dict[str, Any]) -> None:
        if self._bp_tracker:
            self._bp_tracker.handle_event(payload)

    def _on_lighter_order_event(self, payload: Dict[str, Any]) -> None:
        if self._lg_tracker:
            self._lg_tracker.handle_event(payload)

    def _on_backpack_trade(self, payload: Dict[str, Any]) -> None:
        # cache recent trade info for optional diagnostics
        try:
            sym = payload.get("symbol")
            if sym:
                self._positions_cache.setdefault("backpack", {})[f"trade:{sym}"] = payload
        except Exception:
            pass

    def _on_lighter_trade(self, payload: Dict[str, Any]) -> None:
        try:
            mid = payload.get("market_index") or payload.get("marketIndex")
            if mid is not None:
                self._positions_cache.setdefault("lighter", {})[f"trade:{mid}"] = payload
        except Exception:
            pass

    def _on_backpack_position(self, payload: Dict[str, Any]) -> None:
        try:
            sym = payload.get("symbol") or payload.get("market")
            if sym:
                self._positions_cache.setdefault("backpack", {})[sym] = payload
        except Exception:
            pass

    def _on_lighter_position(self, payload: Dict[str, Any]) -> None:
        try:
            mid = str(payload.get("market_id") or payload.get("marketIndex") or payload.get("market"))
            if mid:
                self._positions_cache.setdefault("lighter", {})[mid] = payload
        except Exception:
            pass

    async def _ensure_pair_subscriptions(self) -> None:
        if self._pair_subscriptions_started:
            return
        if not self._pairs:
            return
        symbols = sorted({bp for (bp, _lg) in self._pairs})
        if not symbols:
            return
        try:
            await self.backpack.start_ws_state(symbols)
        except Exception as exc:
            self.log.debug(f"backpack ws subscribe failed: {exc}")
        else:
            self._pair_subscriptions_started = True

    async def _stop_core_after_debug(self) -> None:
        if not self.core:
            return
        try:
            await self.core.shutdown()
        except Exception as exc:
            self.log.error(f"debug_stop_failed shutdown: {exc}")

    def _maybe_finish_debug_cycle(self) -> None:
        if not self._debug_run_once or self._debug_shutdown_triggered:
            return
        self._debug_shutdown_triggered = True
        try:
            asyncio.create_task(self._stop_core_after_debug())
        except RuntimeError:
            pass

    async def _ensure_pairs(self):
        pairs, lg_syms, bp_syms, misses = await discover_pairs(
            self.lighter,
            self.backpack,
            symbol_filters=self.symbol_filters,
        )
        self._pair_stats = {
            "lighter": len(lg_syms),
            "backpack": len(bp_syms),
            "overlap": len(pairs),
        }
        if not self._pairs:
            self._pairs = pairs
        else:
            self._pairs[:] = pairs
        digest = (
            self._pair_stats.get("lighter", 0),
            self._pair_stats.get("backpack", 0),
            self._pair_stats.get("overlap", 0),
        )
        if digest != self._last_pair_digest:
            self._last_pair_digest = digest
            if digest[2] == 0:
                sample_misses = misses[:6]
                self.log.warning(
                    f"pair_discovery_overlap_zero lighter={digest[0]} backpack={digest[1]} misses={sample_misses}"
                )
            else:
                self.log.info(
                    f"pair_discovery lighter={digest[0]} backpack={digest[1]} overlap={digest[2]} sample={pairs[:6]}"
                )
        await self._ensure_pair_subscriptions()

    def _within_maintenance(self) -> bool:
        # local time window
        if self.params.maintenance_local_hour is not None:
            lt = time.localtime()
            if lt.tm_hour == int(self.params.maintenance_local_hour):
                return True
        if self.params.maintenance_hour_utc is not None:
            ut = time.gmtime()
            if ut.tm_hour == int(self.params.maintenance_hour_utc):
                return True
        return False

    def _pre_maint_window(self) -> bool:
        h = self.params.maintenance_local_hour
        if h is None:
            return False
        lt = time.localtime()
        if lt.tm_hour == ((h - 1) % 24) and lt.tm_min >= (60 - int(self.params.pre_maint_close_minutes)):
            return True
        return False

    async def _maybe_circuit_break(self) -> bool:
        try:
            la = await self.lighter.best_effort_latency_ms()
            lb = await self.backpack.best_effort_latency_ms()
            if la > self.params.latency_circuit_ms or lb > self.params.latency_circuit_ms:
                self.log.warning(f"latency high: lighter={la:.0f}ms backpack={lb:.0f}ms; skipping tick")
                return True
        except Exception:
            return False
        return False

    def _next_client_order_index(self) -> int:
        self._cio_counter = (self._cio_counter + 1) % 1_000_000
        if self._cio_counter == 0:
            self._cio_counter = 1
        return self._cio_counter

    async def _register_delta_failure(self, reason: str) -> None:
        self._delta_failure_count += 1
        threshold = getattr(self.params, "max_delta_failures", 3) or 3
        if self._delta_failure_count >= int(threshold):
            if not self._suspend_trading:
                self._suspend_trading = True
                self.log.error(
                    f"delta_guard_triggered reason={reason} count={self._delta_failure_count}; flattening and suspending entries"
                )
            await self._flatten_all_positions()

    async def _flatten_all_positions(self) -> None:
        for key in list(self._open.keys()):
            st = self._open.get(key)
            if not st:
                continue
            await self._close_position(key, st)

    async def _close_position(self, key: Tuple[str, str], st: Dict[str, Any]) -> None:
        bp_sym, lg_sym = key
        size_bp_i = int(st.get("size_bp_i", 0) or 0)
        size_lg_i = int(st.get("size_lg_i", 0) or 0)
        if size_bp_i <= 0 or size_lg_i <= 0:
            self._open.pop(key, None)
            return

        if st.get("dir") == "long_lighter_short_backpack":
            legs = [
                LegOrder(name="lighter", connector=self.lighter, symbol=lg_sym, size_i=size_lg_i, is_ask=True, reduce_only=1),
                LegOrder(name="backpack", connector=self.backpack, symbol=bp_sym, size_i=size_bp_i, is_ask=False, reduce_only=1),
            ]
        else:
            legs = [
                LegOrder(name="lighter", connector=self.lighter, symbol=lg_sym, size_i=size_lg_i, is_ask=False, reduce_only=1),
                LegOrder(name="backpack", connector=self.backpack, symbol=bp_sym, size_i=size_bp_i, is_ask=True, reduce_only=1),
            ]

        errs: List[str] = []
        for leg in legs:
            ok = await self._place_market_leg(leg, size_i=int(leg.size_i), invert=False, reduce_only=1)
            if not ok:
                errs.append(f"{leg.name}:{leg.symbol}:market_fail")
        self._open.pop(key, None)
        if not errs:
            self._maybe_finish_debug_cycle()
        if errs:
            self.log.error(f"close_position_errors key={key} errs={errs}")

    def _build_entry_legs(
        self,
        direction: str,
        bp_sym: str,
        lg_sym: str,
        size_bp_i: int,
        size_lg_i: int,
    ) -> List[LegOrder]:
        if direction == "long_lighter_short_backpack":
            return [
                LegOrder(name="lighter", connector=self.lighter, symbol=lg_sym, size_i=size_lg_i, is_ask=False),
                LegOrder(name="backpack", connector=self.backpack, symbol=bp_sym, size_i=size_bp_i, is_ask=True),
            ]
        return [
            LegOrder(name="lighter", connector=self.lighter, symbol=lg_sym, size_i=size_lg_i, is_ask=True),
            LegOrder(name="backpack", connector=self.backpack, symbol=bp_sym, size_i=size_bp_i, is_ask=False),
        ]
    async def _place_market_leg(self, leg: LegOrder, size_i: int, *, invert: bool = False, reduce_only: int = 1) -> bool:
        try:
            cio = self._next_client_order_index()
            _tx, _ret, err = await leg.connector.place_market(
                leg.symbol,
                cio,
                int(size_i),
                is_ask=(not leg.is_ask) if invert else leg.is_ask,
                reduce_only=int(reduce_only),
            )
            if err:
                self.log.error(f"market_leg_fail venue={leg.name} sym={leg.symbol} err={err}")
                return False
            return True
        except Exception as exc:
            self.log.error(f"market_leg_exception venue={leg.name} sym={leg.symbol} err={exc}")
            return False

    async def _rebalance_after_entry_failure(
        self,
        legs: List[LegOrder],
        results: List[LegExecutionResult],
        reason: str,
    ) -> None:
        for leg, result in zip(legs, results):
            if result.success:
                await self._place_market_leg(leg, result.filled_size_i, invert=True, reduce_only=1)
        await self._register_delta_failure(reason)

    async def _compute_size_info(self, bp_sym: str, lg_sym: str) -> Optional[Dict[str, Any]]:
        key = (bp_sym, lg_sym)
        info = self._size_cache.get(key)
        if info is None:
            info = await pick_common_sizes(self.backpack, self.lighter, bp_sym, lg_sym)
            if info:
                self._size_cache[key] = info
        return info

    async def _gather_opportunities(self) -> List[Dict[str, Any]]:
        th = float(self.params.entry_threshold_pct) / 100.0
        entries: List[Dict[str, Any]] = []
        for bp_sym, lg_sym in self._pairs:
            if (bp_sym, lg_sym) in self._open:
                continue
            try:
                bid_lg_i, ask_lg_i, sc_lg = await self.lighter.get_top_of_book(lg_sym)
                bid_bp_i, ask_bp_i, sc_bp = await self.backpack.get_top_of_book(bp_sym)
            except Exception:
                continue
            if not bid_lg_i or not ask_lg_i or not bid_bp_i or not ask_bp_i:
                continue
            try:
                bid_lg = bid_lg_i / float(sc_lg)
                ask_lg = ask_lg_i / float(sc_lg)
                bid_bp = bid_bp_i / float(sc_bp)
                ask_bp = ask_bp_i / float(sc_bp)
            except Exception:
                continue
            try:
                r_long_lg = (bid_lg / max(1, ask_bp)) - 1.0
                r_short_lg = (bid_bp / max(1, ask_lg)) - 1.0
            except Exception:
                continue
            direction: Optional[str] = None
            entry_ratio = 0.0
            if r_long_lg > th:
                direction = "long_lighter_short_backpack"
                entry_ratio = r_long_lg
            elif r_short_lg > th:
                direction = "short_lighter_long_backpack"
                entry_ratio = r_short_lg
            if not direction:
                continue
            size_info = await self._compute_size_info(bp_sym, lg_sym)
            if not size_info:
                continue
            base_bp = float(size_info.get("base_bp", 0.0))
            base_lg = float(size_info.get("base_lg", 0.0))
            diff = abs(base_bp - base_lg)
            denom = max(base_bp, base_lg, 1e-12)
            tol = float(getattr(self.params, "delta_tolerance", 0.01) or 0.01)
            if denom == 0 or (diff / denom) > tol:
                self.log.warning(
                    f"skip_pair_delta bp={base_bp} lg={base_lg} sym_bp={bp_sym} sym_lg={lg_sym}"
                )
                await self._register_delta_failure("delta_tolerance")
                continue
            entries.append(
                {
                    "bp_sym": bp_sym,
                    "lg_sym": lg_sym,
                    "direction": direction,
                    "entry_ratio": entry_ratio,
                    "size_info": size_info,
                    "top": {
                        "bid_lg": bid_lg,
                        "ask_lg": ask_lg,
                        "bid_bp": bid_bp,
                        "ask_bp": ask_bp,
                    },
                }
            )
        entries.sort(key=lambda x: x["entry_ratio"], reverse=True)
        return entries

    async def _execute_entry(self, opportunity: Dict[str, Any], now_ms: float) -> bool:
        bp_sym = opportunity["bp_sym"]
        lg_sym = opportunity["lg_sym"]
        direction = opportunity["direction"]
        size_info = opportunity["size_info"]

        legs = self._build_entry_legs(
            direction,
            bp_sym,
            lg_sym,
            int(size_info["size_bp_i"]),
            int(size_info["size_lg_i"]),
        )

        wait_secs = float(getattr(self.params, "tracking_wait_secs", 10.0) or 10.0)
        max_retries = int(getattr(self.params, "tracking_max_retries", 3) or 3)
        allow_market = bool(getattr(self.params, "allow_market_fallback", True))

        use_lighter_market = bool(getattr(self.params, "lighter_market_execution", False))
        results: List[LegExecutionResult] = []
        for leg in legs:
            if leg.name == "lighter" and use_lighter_market:
                res = await self._execute_lighter_market_leg(leg)
                results.append(res)
                continue
            executor = self._executors.get(leg.name)
            if executor is None:
                return False
            res = await executor.run(
                leg,
                wait_seconds=wait_secs,
                max_retries=max_retries,
                allow_market_fallback=allow_market,
            )
            results.append(res)
        if not all(res.success for res in results):
            reason = "+".join(res.reason or res.status for res in results if not res.success)
            await self._rebalance_after_entry_failure(legs, results, reason or "entry_failure")
            return False

        base_bp = float(size_info.get("base_bp", 0.0))
        base_lg = float(size_info.get("base_lg", 0.0))
        self._open[(bp_sym, lg_sym)] = {
            "dir": direction,
            "size_bp_i": int(size_info["size_bp_i"]),
            "size_lg_i": int(size_info["size_lg_i"]),
            "base_bp": base_bp,
            "base_lg": base_lg,
            "entry_at": now_ms,
            "entry_ratio": float(opportunity["entry_ratio"]),
            "fills": [(res.order.name, res.status) for res in results],
        }
        self._delta_failure_count = 0
        self._last_trade_at = now_ms
        fill_modes = ",".join(res.status for res in results)
        self.log.info(
            f"entered {bp_sym}~{lg_sym} dir={direction} size={base_bp:.6f} ratio={opportunity['entry_ratio']*100:.3f}% fills={fill_modes}"
        )
        return True

    async def _execute_lighter_market_leg(self, leg: LegOrder) -> LegExecutionResult:
        ok = await self._place_market_leg(leg, size_i=int(leg.size_i), invert=False, reduce_only=0)
        status = "market_filled" if ok else "market_failed"
        reason = None if ok else "market_failed"
        return LegExecutionResult(
            order=leg,
            success=ok,
            filled_size_i=int(leg.size_i if ok else 0),
            attempts=1,
            status=status,
            reason=reason,
            history=[],
        )

    async def _enter_if_opportunity(self, now_ms: float):
        if self._suspend_trading:
            return
        if len(self._open) >= int(self.params.max_concurrent_positions):
            return
        if (now_ms - self._last_trade_at) < (self.params.cooldown_secs * 1000.0):
            return
        for opportunity in await self._gather_opportunities():
            if len(self._open) >= int(self.params.max_concurrent_positions):
                break
            success = await self._execute_entry(opportunity, now_ms)
            if success and len(self._open) >= int(self.params.max_concurrent_positions):
                break

    async def _ensure_balanced_positions(self) -> None:
        if not self._open:
            return
        try:
            bp_positions = await self.backpack.get_positions()
        except Exception:
            bp_positions = []
        try:
            lg_positions = await self.lighter.get_positions()
        except Exception:
            lg_positions = []

        def _extract_value(entry: Dict[str, Any]) -> float:
            for key in ("position", "netSize", "size", "amount", "base_position"):
                val = entry.get(key)
                if val is not None:
                    try:
                        return float(val)
                    except Exception:
                        continue
            return 0.0

        bp_map: Dict[str, float] = {}
        for pos in bp_positions:
            sym = str(pos.get("symbol") or pos.get("market") or "").upper()
            if sym:
                bp_map[sym] = _extract_value(pos)

        lg_map: Dict[str, float] = {}
        for pos in lg_positions:
            sym = pos.get("symbol")
            if not sym:
                try:
                    mid = int(pos.get("market_index") or pos.get("marketIndex") or 0)
                    sym = self.lighter._market_to_symbol.get(mid) if hasattr(self.lighter, "_market_to_symbol") else None
                except Exception:
                    sym = None
            if sym:
                lg_map[str(sym).upper()] = _extract_value(pos)

        tol = float(getattr(self.params, "delta_tolerance", 0.01) or 0.01)
        to_close: List[Tuple[str, str]] = []
        for key, st in list(self._open.items()):
            bp_sym, lg_sym = key
            base_target = float(max(st.get("base_bp", 0.0), st.get("base_lg", 0.0)))
            if base_target <= 0:
                continue
            bp_pos = abs(bp_map.get(bp_sym.upper(), 0.0))
            lg_pos = abs(lg_map.get(lg_sym.upper(), 0.0))
            imbalance = abs(bp_pos - lg_pos)
            if base_target == 0:
                continue
            if (imbalance / base_target) > max(tol, 0.01):
                self.log.warning(
                    f"position_imbalance bp={bp_sym}({bp_pos:.6f}) lg={lg_sym}({lg_pos:.6f}) target={base_target:.6f}"
                )
                to_close.append(key)

        for key in to_close:
            st = self._open.get(key)
            if st:
                await self._close_position(key, st)
                await self._register_delta_failure("position_imbalance")

    async def _maybe_exit_positions(self, now_ms: float):
        to_close: List[Tuple[str, str]] = []
        for key, st in self._open.items():
            bp_sym, lg_sym = key
            try:
                bid_lg, ask_lg, _ = await self.lighter.get_top_of_book(lg_sym)
                bid_bp, ask_bp, _ = await self.backpack.get_top_of_book(bp_sym)
            except Exception:
                continue
            if not bid_lg or not ask_lg or not bid_bp or not ask_bp:
                continue
            # compute current ratio in entry direction
            if st["dir"] == "long_lighter_short_backpack":
                cur = (bid_lg / max(1, ask_bp)) - 1.0
            else:
                cur = (bid_bp / max(1, ask_lg)) - 1.0
            entry = float(st.get("entry_ratio", 0.0))
            # exit rules
            hit_tp = cur <= (1.0 - float(self.params.tp_ratio)) * entry
            hit_sl = cur >= (1.0 + float(self.params.sl_ratio)) * entry
            hit_tl = (now_ms - float(st.get("entry_at", now_ms))) >= (float(self.params.max_hold_secs) * 1000.0)
            if hit_tp or hit_sl or hit_tl or self._pre_maint_window():
                to_close.append(key)
        # close marked
        for key in to_close:
            st = self._open.get(key)
            if not st:
                continue
            await self._close_position(key, st)
            self.log.info(f"closed {key[0]}~{key[1]}")

    async def on_tick(self, now_ms: float):
        # pace
        if (now_ms - self._last_poll_ms) < int(self.params.poll_interval_ms):
            return
        self._last_poll_ms = now_ms

        # ensure symbols discovered
        await self._ensure_pairs()
        if not self._pairs:
            return

        # circuit breaker
        if await self._maybe_circuit_break():
            return

        # avoid new entries during maintenance window
        if self._within_maintenance():
            await self._maybe_exit_positions(now_ms)
            return

        # exit checks
        await self._maybe_exit_positions(now_ms)

        # balance check to avoid residual exposure
        await self._ensure_balanced_positions()

        # entry checks
        await self._enter_if_opportunity(now_ms)
