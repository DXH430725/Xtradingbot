import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, Optional

from mm_bot.strategy.strategy_base import StrategyBase


@dataclass
class HedgeLadderParams:
    backpack_symbol: str = "BTC_USDC_PERP"
    lighter_symbol: str = "BTC"
    quantity_base: float = 0.0002
    take_profit_abs: float = 5.0
    max_concurrent_positions: int = 10
    entry_interval_secs: float = 60.0
    poll_interval_ms: int = 500
    hedge_trigger_ratio: float = 0.99
    hedge_rate_limit_per_sec: int = 10
    hedge_retry: int = 3
    hedge_retry_delay: float = 0.2
    lighter_reduce_only_close: bool = True
    debug: bool = False


class HedgeLadderStrategy(StrategyBase):
    def __init__(
        self,
        backpack_connector: Any,
        lighter_connector: Any,
        params: Optional[HedgeLadderParams] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.backpack = backpack_connector
        self.lighter = lighter_connector
        self.params = params or HedgeLadderParams()
        self.log = logger or logging.getLogger("mm_bot.strategy.hedge_ladder")

        self._core: Optional[Any] = None
        self._started: bool = False

        # symbol/scales
        self._bp_symbol: str = self.params.backpack_symbol
        self._lg_symbol: str = self.params.lighter_symbol
        self._bp_price_scale: int = 1
        self._bp_size_scale: int = 1
        self._lg_price_scale: int = 1
        self._lg_size_scale: int = 1
        self._bp_min_size_i: int = 1
        self._bp_order_size_i: int = 1

        # order/position tracking
        self._entry_orders: Dict[int, Dict[str, Any]] = {}
        self._tp_orders: Dict[int, Dict[str, Any]] = {}
        self._positions: Dict[int, Dict[str, Any]] = {}
        self._hedge_records: Dict[int, Dict[str, Any]] = {}
        self._last_entry_ts: float = 0.0
        self._last_poll_ms: float = 0.0
        self._hedge_rate_window: deque[float] = deque(maxlen=self.params.hedge_rate_limit_per_sec)

        self._coi_counter: int = int(time.time() * 1000) % 1_000_000 or 1

    # ------------------------------------------------------------------
    def start(self, core: Any) -> None:
        self._core = core
        self._started = True
        self.backpack.set_event_handlers(
            on_order_filled=self._on_backpack_filled,
            on_order_cancelled=self._on_backpack_cancelled,
            on_position_update=self._on_backpack_position,
        )
        self.lighter.set_event_handlers(
            on_order_filled=self._on_lighter_event,
            on_order_cancelled=self._on_lighter_event,
            on_position_update=self._on_lighter_position,
        )

    def stop(self) -> None:
        self._started = False
        try:
            asyncio.create_task(self.shutdown())
        except RuntimeError:
            pass

    # ------------------------------------------------------------------
    async def _ensure_ready(self) -> None:
        if self._bp_size_scale != 1 and self._lg_size_scale != 1:
            return
        await self.backpack.start_ws_state([self._bp_symbol])
        await self.backpack._ensure_markets()
        p_dec_bp, s_dec_bp = await self.backpack.get_price_size_decimals(self._bp_symbol)
        self._bp_price_scale = 10 ** p_dec_bp
        self._bp_size_scale = 10 ** s_dec_bp
        info = self.backpack._market_info.get(self._bp_symbol, {})
        try:
            min_size = float((info.get("filters", {}) or {}).get("quantity", {}).get("minQuantity", "0.0001"))
        except Exception:
            min_size = 0.0001
        self._bp_min_size_i = max(1, int(round(min_size * self._bp_size_scale)))
        want_size_i = int(round(self.params.quantity_base * self._bp_size_scale))
        self._bp_order_size_i = max(self._bp_min_size_i, want_size_i)

        await self.lighter.start_ws_state()
        await self.lighter._ensure_markets()
        _p_dec_lg, s_dec_lg = await self.lighter.get_price_size_decimals(self._lg_symbol)
        self._lg_price_scale = 10 ** _p_dec_lg
        self._lg_size_scale = 10 ** s_dec_lg
        self.log.info(
            "HedgeLadder ready: bp_sym=%s size_i=%s min_i=%s lg_sym=%s",
            self._bp_symbol,
            self._bp_order_size_i,
            self._bp_min_size_i,
            self._lg_symbol,
        )

    # ------------------------------------------------------------------
    def _next_coi(self) -> int:
        self._coi_counter = (self._coi_counter + 1) % 1_000_000
        if self._coi_counter == 0:
            self._coi_counter = 1
        return self._coi_counter

    def _on_backpack_position(self, payload: Dict[str, Any]) -> None:
        try:
            sym = str(payload.get("symbol") or payload.get("s") or payload.get("market") or "").upper()
        except Exception:
            sym = ""
        if sym != self._bp_symbol.upper():
            return
        self._positions[int(payload.get("clientOrderId", 0) or 0)] = payload

    def _on_lighter_position(self, payload: Dict[str, Any]) -> None:
        pass  # placeholder for future metrics

    def _on_lighter_event(self, payload: Dict[str, Any]) -> None:
        if not self.params.debug:
            return
        try:
            status = payload.get("status") or payload.get("X")
            self.log.debug(f"lighter_event status={status} payload={payload}")
        except Exception:
            pass

    def _on_backpack_filled(self, payload: Dict[str, Any]) -> None:
        try:
            coi = int(payload.get("clientId") or payload.get("clientID") or payload.get("client_order_index") or -1)
        except Exception:
            return
        if coi in self._entry_orders:
            meta = self._entry_orders.pop(coi)
            entry_price_i = int(float(payload.get("price") or meta.get("price_i")))
            entry_price = entry_price_i / float(self._bp_price_scale or 1)
            size_i = int(meta.get("size_i", self._bp_order_size_i))
            hedge_price = entry_price * float(self.params.hedge_trigger_ratio)
            self._hedge_records[coi] = {
                "entry_price": entry_price,
                "hedge_price": hedge_price,
                "size_i": size_i,
                "hedged": False,
                "hedge_size_i": 0,
                "tp_coi": None,
            }
            asyncio.create_task(self._place_take_profit(coi, entry_price_i, size_i))
        elif coi in self._tp_orders:
            meta = self._tp_orders.pop(coi)
            entry_coi = meta.get("entry_coi")
            if entry_coi in self._hedge_records:
                record = self._hedge_records.pop(entry_coi)
                if record.get("hedged"):
                    asyncio.create_task(self._ensure_hedge_closed(entry_coi, record))

    def _on_backpack_cancelled(self, payload: Dict[str, Any]) -> None:
        try:
            coi = int(payload.get("clientId") or payload.get("clientID") or payload.get("client_order_index") or -1)
        except Exception:
            return
        self._entry_orders.pop(coi, None)
        meta = self._tp_orders.pop(coi, None)
        if meta:
            entry_coi = meta.get("entry_coi")
            record = self._hedge_records.get(entry_coi)
            if record:
                record["tp_coi"] = None

    # ------------------------------------------------------------------
    async def _place_take_profit(self, entry_coi: int, entry_price_i: int, size_i: int) -> None:
        price_offset_i = int(round(self.params.take_profit_abs * self._bp_price_scale))
        tp_price_i = entry_price_i + price_offset_i
        cio = self._next_coi()
        _, ret, err = await self.backpack.place_limit(
            self._bp_symbol,
            cio,
            size_i,
            price=tp_price_i,
            is_ask=True,
            post_only=True,
            reduce_only=1,
        )
        if err:
            self.log.warning(
                "place_tp_failed entry_coi=%s coi=%s price_i=%s err=%s",
                entry_coi,
                cio,
                tp_price_i,
                err,
            )
            return
        self._tp_orders[cio] = {"entry_coi": entry_coi, "price_i": tp_price_i}
        record = self._hedge_records.get(entry_coi)
        if record:
            record["tp_coi"] = cio
        self.log.info(
            "tp_placed entry_coi=%s tp_coi=%s price=%.4f",
            entry_coi,
            cio,
            tp_price_i / float(self._bp_price_scale),
        )

    # ------------------------------------------------------------------
    async def _maybe_place_entry(self, now_ms: float) -> None:
        if len(self._hedge_records) >= int(self.params.max_concurrent_positions):
            return
        if (time.time() - self._last_entry_ts) < float(self.params.entry_interval_secs):
            return
        bid_i, ask_i, scale = await self.backpack.get_top_of_book(self._bp_symbol)
        if bid_i is None or ask_i is None:
            return
        price_i = bid_i  # post at best bid
        cio = self._next_coi()
        size_i = self._bp_order_size_i
        _, ret, err = await self.backpack.place_limit(
            self._bp_symbol,
            cio,
            size_i,
            price=price_i,
            is_ask=False,
            post_only=True,
        )
        if err:
            self.log.warning("entry_place_failed coi=%s err=%s", cio, err)
            return
        self._entry_orders[cio] = {
            "price_i": price_i,
            "size_i": size_i,
            "created": time.time(),
        }
        self._last_entry_ts = time.time()
        self.log.info("entry_placed coi=%s price=%.4f size=%.6f", cio, price_i / scale, size_i / float(self._bp_size_scale))

    # ------------------------------------------------------------------
    async def _ensure_hedges(self, last_price: float) -> None:
        for entry_coi, record in list(self._hedge_records.items()):
            hedge_price = float(record.get("hedge_price"))
            hedged = bool(record.get("hedged"))
            if not hedged and last_price <= hedge_price:
                ok = await self._open_hedge(entry_coi, record)
                if ok:
                    record["hedged"] = True
            elif hedged and last_price > hedge_price:
                await self._ensure_hedge_closed(entry_coi, record)

    async def _open_hedge(self, entry_coi: int, record: Dict[str, Any]) -> bool:
        size_i = int(record.get("size_i", self._bp_order_size_i))
        if size_i <= 0:
            return False
        result = await self._execute_lighter_market_order(size_i, is_ask=True, reduce_only=0)
        if result:
            record["hedged"] = True
            record["hedge_size_i"] = size_i
            self.log.info("hedge_opened entry=%s size_i=%s", entry_coi, size_i)
        else:
            self.log.warning("hedge_open_failed entry=%s", entry_coi)
        return result

    async def _ensure_hedge_closed(self, entry_coi: int, record: Dict[str, Any]) -> None:
        if not record.get("hedged"):
            return
        size_i = int(record.get("hedge_size_i", 0))
        if size_i <= 0:
            record["hedged"] = False
            return
        result = await self._execute_lighter_market_order(size_i, is_ask=False, reduce_only=1 if self.params.lighter_reduce_only_close else 0)
        if result:
            record["hedged"] = False
            record["hedge_size_i"] = 0
            self.log.info("hedge_closed entry=%s", entry_coi)
        else:
            self.log.warning("hedge_close_failed entry=%s", entry_coi)

    async def _execute_lighter_market_order(self, size_i: int, *, is_ask: bool, reduce_only: int) -> bool:
        for attempt in range(max(1, int(self.params.hedge_retry))):
            await self._respect_hedge_rate_limit()
            cio = self._next_coi()
            _tx, _ret, err = await self.lighter.place_market(
                self._lg_symbol,
                cio,
                size_i,
                is_ask=is_ask,
                reduce_only=reduce_only,
            )
            if not err:
                return True
            self.log.warning(
                "lighter_market_fail coi=%s attempt=%s err=%s",
                cio,
                attempt + 1,
                err,
            )
            await asyncio.sleep(float(self.params.hedge_retry_delay) * (attempt + 1))
        return False

    async def _respect_hedge_rate_limit(self) -> None:
        limit = int(self.params.hedge_rate_limit_per_sec)
        if limit <= 0:
            return
        now = time.time()
        if len(self._hedge_rate_window) < limit:
            self._hedge_rate_window.append(now)
            return
        earliest = self._hedge_rate_window[0]
        wait = 1.0 - (now - earliest)
        if wait > 0:
            await asyncio.sleep(wait)
        now = time.time()
        if len(self._hedge_rate_window) == limit:
            self._hedge_rate_window.popleft()
        self._hedge_rate_window.append(now)

    # ------------------------------------------------------------------
    async def on_tick(self, now_ms: float) -> None:
        await self._ensure_ready()
        if (now_ms - self._last_poll_ms) < int(self.params.poll_interval_ms):
            return
        self._last_poll_ms = now_ms
        bid_i, ask_i, scale = await self.backpack.get_top_of_book(self._bp_symbol)
        if bid_i is None or ask_i is None:
            return
        mid_price = (bid_i + ask_i) / (2 * float(scale))
        await self._maybe_place_entry(now_ms)
        await self._ensure_hedges(mid_price)

    # ------------------------------------------------------------------
    async def shutdown(self) -> None:
        # cancel open Backpack orders
        try:
            await self.backpack.cancel_all(self._bp_symbol)
        except Exception as exc:
            self.log.warning("backpack_cancel_all_failed err=%s", exc)
        # attempt to close hedges
        for entry_coi, record in list(self._hedge_records.items()):
            try:
                await self._ensure_hedge_closed(entry_coi, record)
            except Exception as exc:
                self.log.warning("hedge_close_on_shutdown_failed entry=%s err=%s", entry_coi, exc)
        self._hedge_records.clear()
