import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple
from collections import deque

from mm_bot.strategy.strategy_base import StrategyBase
from mm_bot.connector.backpack import BackpackConnector


@dataclass
class TrendLadderParams:
    quantity_base: float = 0.0002           # per-order base size (e.g., 0.8 ETH)
    take_profit_abs: float = 5         # absolute $ take-profit per order
    max_orders: int = 40                 # max concurrent entry orders
    base_wait: float = 450.0             # base wait seconds between entries
    min_wait: float = 60.0               # min wait cap
    max_wait: float = 900.0              # max wait cap
    # user-defined trading direction (disable trend auto-switching)
    fixed_direction: str = "long"        # "long" or "short"
    # (legacy) trend detection params (ignored when fixed_direction is set)
    ema_len: int = 50                    # EMA length on 1m closes
    atr_len: int = 14                    # ATR length
    slope_up: float = 0.2                # switch to long if slope > +0.2
    slope_down: float = -0.2             # switch to short if slope < -0.2
    warmup_minutes: int = 50             # minutes to warm up (ensure EMA50)
    flush_ticks: int = 1                 # ticks outside best for flush TP
    imb_threshold_mult: int = 3          # imbalance threshold in multiples of min_size
    imb_max_corr_in_10m: int = 3         # max corrections allowed within 10 minutes
    partial_tp_enabled: bool = True      # enable incremental TP on partial fills
    requote_ticks: int = 5               # re-quote threshold in integer ticks
    max_requotes_per_tick: int = 2       # max number of entry re-quotes per tick
    requote_abs: float = 0.0             # absolute $ move to trigger re-quote (optional)
    requote_wait_confirm_secs: float = 5.0   # wait WS confirm removal up to N seconds
    requote_skip_backoff_secs: float = 15.0  # initial backoff after skip/error
    requote_skip_backoff_max_secs: float = 120.0  # max backoff cap
    # if cancel keeps reporting open abnormally, force treat-as-removed after retries/seconds
    requote_force_forget_after_retries: int = 3
    requote_force_forget_after_secs: float = 600.0
    # requote mode: default cancel_first; place_first to place new order first then cancel old to reduce gap
    requote_mode: str = "cancel_first"   # "cancel_first" | "place_first"
    # when using place_first mode, limit how many duplicate entries we create per tick
    max_requote_dupes: int = 1
    pace_ignore_imbalance_tp: bool = False # when pacing entries, ignore imbalance-cover TPs
    # telemetry
    telemetry_enabled: bool = True
    telemetry_url: Optional[str] = None
    telemetry_interval_secs: int = 10


class TrendAdaptiveLadderStrategy(StrategyBase):
    def __init__(
        self,
        connector: BackpackConnector,
        symbol: Optional[str] = None,
        params: Optional[TrendLadderParams] = None,
    ):
        self.log = logging.getLogger("mm_bot.strategy.trend_ladder")
        self.connector = connector
        self.symbol = symbol
        self.params = params or TrendLadderParams()
        self.preserve_orders_on_stop: bool = True

        self._core: Any = None
        self._started = False

        # market scales
        self._market_id: Optional[int] = None
        self._size_i: Optional[int] = None
        self._price_scale: int = 1
        self._size_scale: int = 1
        self._min_size_i: int = 1
        self._price_tick_i: int = 1
        self._size_step_i: int = 1

        # bar aggregator (1m)
        self._cur_minute: Optional[int] = None
        self._bar_ohlc: Optional[Tuple[int, int, int, int]] = None  # (o,h,l,c) in integer price
        self._bars: Deque[Tuple[int, int, int, int]] = deque(maxlen=max(self.params.warmup_minutes + 10, 100))

        # trend state
        # honor fixed user direction; default long
        self._direction: str = (str(self.params.fixed_direction).strip().lower() if getattr(self.params, "fixed_direction", None) else "long")
        if self._direction not in ("long", "short"):
            self._direction = "long"
        self._last_slope: float = 0.0
        self._flush_mode: bool = False
        self._pending_direction: Optional[str] = None

        # order tracking
        # maps coi -> info dict
        self._orders: Dict[int, Dict[str, Any]] = {}
        self._entry_cois_long: set[int] = set()
        self._entry_cois_short: set[int] = set()
        self._tp_cois: set[int] = set()
        # (no external counters; rely on local tracking)

        # timing
        self._last_entry_time: float = 0.0
        self._immediate_entry: bool = False
        self._hydrated_open_orders: bool = False
        self._last_close_orders: int = 0
        self._entry_filled_i_long: int = 0
        self._entry_filled_i_short: int = 0
        self._trade_lock: asyncio.Lock = asyncio.Lock()
        # imbalance correction rate-limit
        self._imb_corrections: Deque[float] = deque(maxlen=100)
        # telemetry task
        self._telemetry_task: Optional[asyncio.Task] = None
        # requote backoff for stubborn orders (by order_index)
        self._requote_skip_until: Dict[str, float] = {}
        self._requote_skip_count: Dict[str, int] = {}
        # first-seen timestamp for stuck oi during requote
        self._requote_first_seen_ts: Dict[str, float] = {}

    async def _place_limit_with_retry(
        self,
        base_amount_i: int,
        price_i: int,
        is_ask: bool,
        post_only: bool = True,
        reduce_only: int = 0,
        retries: int = 3,
        retry_delay: float = 0.2,
    ) -> Tuple[Optional[int], Optional[Any], Optional[str]]:
        base_amount_i = int(base_amount_i)
        if base_amount_i <= 0:
            return None, None, "size_below_min"
        cio = int(time.time() * 1000) % 1_000_000
        last_err = None
        for attempt in range(max(1, retries)):
            try:
                self.log.debug(
                    "place_limit attempt=%s/%s symbol=%s price_i=%s price=%.8f size_i=%s size=%.8f is_ask=%s post_only=%s reduce_only=%s coi=%s",
                    attempt + 1,
                    retries,
                    self.symbol,
                    price_i,
                    price_i / float(self._price_scale or 1),
                    base_amount_i,
                    base_amount_i / float(self._size_scale or 1),
                    is_ask,
                    post_only,
                    reduce_only,
                    cio,
                )
                _tx, ret, err = await self.connector.place_limit(self.symbol, cio, base_amount_i, price=price_i, is_ask=is_ask, post_only=post_only, reduce_only=reduce_only)
                if err and ("invalid nonce" in str(err).lower() or "nonce" in str(err).lower()):
                    last_err = err
                    await asyncio.sleep(retry_delay * (attempt + 1))
                    continue
                return cio, ret, err
            except Exception as e:
                last_err = str(e)
                await asyncio.sleep(retry_delay * (attempt + 1))
        # all attempts failed
        return None, None, last_err

    def start(self, core: Any) -> None:
        self._core = core
        self._started = True
        self.connector.set_event_handlers(
            on_order_filled=self._on_filled,
            on_order_cancelled=self._on_cancelled,
            on_trade=lambda t: asyncio.create_task(self._handle_trade(t)),
        )
        # start telemetry if configured
        if getattr(self.params, "telemetry_enabled", False) and getattr(self.params, "telemetry_url", None):
            try:
                self._telemetry_task = asyncio.create_task(self._telemetry_loop())
            except Exception:
                self._telemetry_task = None

    def stop(self) -> None:
        self._started = False
        if self._telemetry_task is not None:
            try:
                self._telemetry_task.cancel()
            except Exception:
                pass
            self._telemetry_task = None

    # --------------- events ---------------
    def _on_filled(self, info: Dict[str, Any]) -> None:
        try:
            coi = self._client_order_index_of(info)
        except Exception:
            return
        if coi is None:
            return
        meta = self._orders.get(coi)
        if not meta:
            return
        typ = meta.get("type")
        side = meta.get("side")
        entry_i = int(meta.get("entry_price_i", 0))
        if typ == "entry":
            # remove from entry tracking
            (self._entry_cois_long if side == "long" else self._entry_cois_short).discard(coi)
            # place TP immediately
            now = time.time()
            self._last_entry_time = now
            asyncio.create_task(self._place_tp_for_entry(entry_i, side))
        elif typ == "tp":
            # TP done -> schedule an immediate new entry if direction matches
            self._tp_cois.discard(coi)
            self._immediate_entry = True

    def _on_cancelled(self, info: Dict[str, Any]) -> None:
        try:
            coi = self._client_order_index_of(info)
        except Exception:
            return
        if coi is None:
            return
        meta = self._orders.pop(coi, None)
        if not meta:
            return
        if meta.get("type") == "entry":
            (self._entry_cois_long if meta.get("side") == "long" else self._entry_cois_short).discard(coi)
            self._immediate_entry = True
        elif meta.get("type") == "tp":
            self._tp_cois.discard(coi)

    # --------------- setup helpers ---------------
    async def _ensure_ready(self) -> None:
        if self._market_id is not None and self.symbol is not None and self._size_i is not None:
            return
        # Backpack WS requires explicit symbols when available to reduce noise
        try:
            if self.symbol:
                await self.connector.start_ws_state([self.symbol])
            else:
                await self.connector.start_ws_state()
        except TypeError:
            await self.connector.start_ws_state()
        await self.connector._ensure_markets()
        if self.symbol is None:
            sym = next((s for s in self.connector._symbol_to_market.keys() if s.upper().startswith("BTC")), None)
            if sym is None:
                raise RuntimeError("No BTC symbol available")
            self.symbol = sym
        self._market_id = await self.connector.get_market_id(self.symbol)
        # scales and min size from market info
        p_dec, s_dec = await self.connector.get_price_size_decimals(self.symbol)
        self._price_scale = 10 ** p_dec
        self._size_scale = 10 ** s_dec
        info = await self.connector.get_market_info(self.symbol)
        try:
            min_size = float(
                info.get("min_qty")
                or info.get("minQuantity")
                or info.get("minBase")
                or info.get("minBaseAmount")
                or 0.0
            )
        except Exception:
            min_size = 0.0
        try:
            step_size = float(
                info.get("step_size")
                or info.get("quantity_step")
                or info.get("stepSize")
                or info.get("quantityStep")
                or 0.0
            )
        except Exception:
            step_size = 0.0
        try:
            tick_size = float(
                info.get("tick_size")
                or info.get("price_tick")
                or info.get("tickSize")
                or info.get("priceTick")
                or 0.0
            )
        except Exception:
            tick_size = 0.0
        min_size_i = max(1, int(round(min_size * (10 ** s_dec))))
        self._size_step_i = max(1, int(round(step_size * (10 ** s_dec)))) if step_size > 0 else 1
        if self._size_step_i > 1:
            min_size_i = ((min_size_i + self._size_step_i - 1) // self._size_step_i) * self._size_step_i
        self._min_size_i = min_size_i
        self._price_tick_i = max(1, int(round(tick_size * (10 ** p_dec)))) if tick_size > 0 else 1
        want_size_i = int(round(float(self.params.quantity_base) * (10 ** s_dec)))
        if self._size_step_i > 1:
            want_size_i = ((want_size_i + self._size_step_i - 1) // self._size_step_i) * self._size_step_i
        self._size_i = max(min_size_i, want_size_i)
        if self._size_step_i > 1:
            self._size_i = ((self._size_i + self._size_step_i - 1) // self._size_step_i) * self._size_step_i
        self.log.info(
            "TrendLadder ready: symbol=%s market_id=%s size_i(min)=%s size_i(use)=%s price_tick_i=%s size_step_i=%s",
            self.symbol,
            self._market_id,
            min_size_i,
            self._size_i,
            self._price_tick_i,
            self._size_step_i,
        )
        if not self._orders:
            self._immediate_entry = True

    # --------------- normalization helpers ---------------
    def _order_id_of(self, obj: Dict[str, Any]) -> Optional[str]:
        for key in ("order_index", "orderIndex", "orderId", "order_id", "id", "i"):
            val = obj.get(key)
            if val is None:
                continue
            try:
                sval = str(val).strip()
                if sval:
                    return sval
            except Exception:
                continue
        return None

    def _client_order_index_of(self, obj: Dict[str, Any]) -> Optional[int]:
        for key in (
            "client_order_index",
            "clientOrderIndex",
            "clientId",
            "clientID",
            "client_id",
            "clientid",
            "c",
        ):
            val = obj.get(key)
            if val is None:
                continue
            try:
                return int(str(val).strip())
            except Exception:
                continue
        return None

    def _order_is_reduce_only(self, obj: Dict[str, Any]) -> bool:
        for key in ("reduceOnly", "reduce_only", "reduce-only", "reduceonly"):
            if key in obj:
                try:
                    val = obj[key]
                    if isinstance(val, str):
                        return val.strip().lower() in {"1", "true", "yes", "on"}
                    return bool(val)
                except Exception:
                    continue
        return False

    def _order_is_post_only(self, obj: Dict[str, Any]) -> bool:
        for key in ("postOnly", "post_only", "post-only", "postonly"):
            if key in obj:
                try:
                    val = obj[key]
                    if isinstance(val, str):
                        return val.strip().lower() in {"1", "true", "yes", "on"}
                    return bool(val)
                except Exception:
                    continue
        return False

    def _order_side_label(self, obj: Dict[str, Any]) -> Optional[str]:
        side = str(obj.get("side", "") or obj.get("S", "")).strip().lower()
        if not side and obj.get("is_ask") is not None:
            return "short" if bool(obj.get("is_ask")) else "long"
        if side in {"sell", "ask", "short", "s"}:
            return "short"
        if side in {"buy", "bid", "long", "b"}:
            return "long"
        return None

    def _order_price_i(self, obj: Dict[str, Any]) -> Optional[int]:
        price = obj.get("price") or obj.get("p") or obj.get("avgPrice")
        if price is None:
            return None
        try:
            if isinstance(price, int):
                return price
            return int(round(float(price) * self._price_scale))
        except Exception:
            return None

    def _order_amount_i(self, obj: Dict[str, Any]) -> Optional[int]:
        for key in (
            "quantity",
            "qty",
            "size",
            "amount",
            "base_amount",
            "baseSize",
            "openQuantity",
            "origQty",
        ):
            val = obj.get(key)
            if val is None:
                continue
            try:
                if isinstance(val, int):
                    return val if val >= 0 else None
                return int(round(float(val) * self._size_scale))
            except Exception:
                continue
        return None

    def _quantize_price_i(self, price_i: int, is_ask: bool) -> int:
        tick = max(1, int(self._price_tick_i or 1))
        if tick <= 1:
            return max(1, price_i)
        if is_ask:
            price_i = ((int(price_i) + tick - 1) // tick) * tick
        else:
            price_i = (int(price_i) // tick) * tick
        return max(tick, price_i)

    def _quantize_size_i(self, size_i: int, prefer_up: bool) -> int:
        step = max(1, int(self._size_step_i or 1))
        if step <= 1:
            return max(0, size_i)
        if prefer_up:
            size_i = ((int(size_i) + step - 1) // step) * step
        else:
            size_i = (int(size_i) // step) * step
        return max(0, size_i)

    def _has_pending_entry(self, side: str) -> bool:
        if side == "long":
            return len(self._entry_cois_long) > 0
        if side == "short":
            return len(self._entry_cois_short) > 0
        return bool(self._entry_cois_long or self._entry_cois_short)

    async def _hydrate_existing_orders(self) -> None:
        opens = await self.connector.get_open_orders(self.symbol)
        if not isinstance(opens, list):
            return
        restored_entries = restored_tps = 0
        for order in opens:
            if not isinstance(order, dict):
                continue
            coi = self._client_order_index_of(order)
            if coi is None or coi in self._orders:
                continue
            side = self._order_side_label(order) or self._direction
            price_i = self._order_price_i(order)
            size_i = self._order_amount_i(order)
            order_id = self._order_id_of(order)
            meta: Dict[str, Any] = {
                "side": side,
                "order_index": order_id,
            }
            if price_i is not None:
                meta["entry_price_i"] = price_i
            if size_i is not None:
                meta["size_i"] = size_i
            if self._order_is_reduce_only(order):
                meta["type"] = "tp"
                self._tp_cois.add(coi)
                restored_tps += 1
            else:
                meta["type"] = "entry"
                if side == "long":
                    self._entry_cois_long.add(coi)
                else:
                    self._entry_cois_short.add(coi)
                restored_entries += 1
            self._orders[coi] = meta
        if restored_entries or restored_tps:
            self.log.info(
                "hydrated open orders: entries=%s tps=%s", restored_entries, restored_tps
            )
        else:
            self._immediate_entry = True
        # refresh cached positions so net checks consider persisted exposure
        try:
            for pos in await self.connector.get_positions():
                if not isinstance(pos, dict):
                    continue
                sym = str(pos.get("symbol") or pos.get("s") or "").upper()
                if sym:
                    self.connector._positions_by_symbol[sym] = pos
        except Exception:
            pass

    def _trade_amount_i(self, t: Dict[str, Any]) -> int:
        for key in (
            "base_amount",
            "baseAmount",
            "size",
            "baseSize",
            "amount",
            "quantity",
            "filledQuantity",
            "filledQty",
            "executedQuantity",
            "executedQty",
            "filled_size",
            "filledSize",
            "q",
        ):
            v = t.get(key)
            if v is None:
                continue
            try:
                if isinstance(v, int):
                    return max(0, int(v))
                if isinstance(v, float):
                    return max(0, int(round(v * self._size_scale)))
                s = str(v).strip()
                if s.isdigit():
                    return max(0, int(s))
                return max(0, int(round(float(s) * self._size_scale)))
            except Exception:
                continue
        return 0

    def _trade_is_ask(self, t: Dict[str, Any]) -> Optional[bool]:
        v = t.get("is_ask")
        if v is None:
            v = t.get("isAsk")
        if v is not None:
            try:
                if isinstance(v, str):
                    s = v.strip().lower()
                    if s in ("true","1","yes","on"): return True
                    if s in ("false","0","no","off"): return False
                    v = int(s)
                return bool(v)
            except Exception:
                pass
        side = str(t.get("side",""))
        if side:
            side = side.strip().lower()
            if side in ("sell","ask"): return True
            if side in ("buy","bid"): return False
        return None

    def _trade_coi(self, t: Dict[str, Any]) -> Optional[int]:
        for k in (
            "client_order_index",
            "clientOrderIndex",
            "clientId",
            "clientID",
            "client_id",
            "clientid",
            "c",
            "coi",
        ):
            v = t.get(k)
            if v is None:
                continue
            try:
                return int(v)
            except Exception:
                try:
                    return int(str(v).strip())
                except Exception:
                    continue
        return None

    async def _handle_trade(self, t: Dict[str, Any]) -> None:
        if not getattr(self.params, "partial_tp_enabled", True):
            return
        async with self._trade_lock:
            coi = self._trade_coi(t)
            is_ask = self._trade_is_ask(t)
            amt_i = self._trade_amount_i(t)
            if amt_i <= 0:
                return
            meta = self._orders.get(coi) if coi is not None else None
            # decide side: prefer entry meta; fallback to trade side
            if meta is not None and meta.get("type") == "entry":
                side = meta.get("side")  # 'long' or 'short'
            else:
                # fallback: buy increases long, sell increases short
                if is_ask is None:
                    return
                side = "long" if (is_ask is False) else "short"
            if side == "long" and (is_ask is False or is_ask is None):
                self._entry_filled_i_long += amt_i
            elif side == "short" and (is_ask is True or is_ask is None):
                self._entry_filled_i_short += amt_i
            else:
                # side mismatch; skip
                return
            # compute open coverage on this side
            opens = await self.connector.get_open_orders(self.symbol)
            need_sell = True if side == "long" else False
            covered_i, matched, total = self._sum_open_coverage_i(need_sell=need_sell, opens=opens)
            filled_i = self._entry_filled_i_long if side == "long" else self._entry_filled_i_short
            needed_i = max(0, filled_i - covered_i)
            if needed_i < self._min_size_i:
                return
            # place one TP chunk (at most lot size)
            tp_chunk = min(int(self._size_i or self._min_size_i), needed_i)
            tp_chunk = self._quantize_size_i(tp_chunk, prefer_up=False)
            if tp_chunk < self._min_size_i:
                return
            # compute TP price near current book +/- take_profit_abs
            bid, ask, scale = await self.connector.get_top_of_book(self.symbol)
            tp_i = max(1, int(round(self.params.take_profit_abs * self._price_scale)))
            tick_i = max(1, int(self._price_tick_i or 1))
            if side == "long":
                price_i = max(1, (ask if ask is not None else (bid + tick_i if bid else tick_i)) + tp_i)
                is_ask_flag = True
            else:
                price_i = max(1, (bid if bid is not None else (ask - tick_i if ask else tick_i)) - tp_i)
                is_ask_flag = False
            price_i = self._quantize_price_i(price_i, is_ask_flag)
            cio = int(time.time() * 1000) % 1_000_000
            cio, ret, err = await self._place_limit_with_retry(tp_chunk, price_i, is_ask_flag, post_only=True, reduce_only=1)
            if err:
                self.log.warning(f"partial TP rejected: side={side} amt_i={tp_chunk} price_i={price_i} err={err}")
            elif cio is not None:
                self._orders[cio] = {"type": "tp", "side": side, "partial": True}
                self._tp_cois.add(cio)
                self.log.info(f"partial TP placed: side={side} amt_i={tp_chunk} price_i={price_i} coi={cio}")

    # --------------- bar/indicators ---------------
    def _on_price(self, price_i: int, now_ms: int) -> None:
        minute = now_ms // 60000
        if self._cur_minute is None:
            self._cur_minute = minute
            self._bar_ohlc = (price_i, price_i, price_i, price_i)
            return
        if minute == self._cur_minute:
            o, h, l, _c = self._bar_ohlc
            h = max(h, price_i)
            l = min(l, price_i)
            self._bar_ohlc = (o, h, l, price_i)
        else:
            # finalize previous bar
            if self._bar_ohlc is not None:
                self._bars.append(self._bar_ohlc)
            # new bar
            self._cur_minute = minute
            self._bar_ohlc = (price_i, price_i, price_i, price_i)

    def _ema(self, values: List[float], length: int) -> List[float]:
        if not values:
            return []
        k = 2.0 / (length + 1)
        out: List[float] = []
        ema = values[0]
        out.append(ema)
        for v in values[1:]:
            ema = v * k + ema * (1 - k)
            out.append(ema)
        return out

    def _atr(self, bars: List[Tuple[int, int, int, int]], length: int) -> List[float]:
        if len(bars) < 2:
            return [0.0] * len(bars)
        trs: List[float] = []
        prev_close = float(bars[0][3])
        for b in bars[1:]:
            high = float(b[1])
            low = float(b[2])
            tr = max(high - low, abs(high - prev_close), abs(prev_close - low))
            trs.append(tr)
            prev_close = float(b[3])
        # pad to align lengths: len(trs) = len(bars)-1
        if not trs:
            return [0.0] * len(bars)
        # EMA ATR
        atr_series = self._ema(trs, length)
        return [0.0] + atr_series  # shift to align with bars

    def _compute_slope(self) -> Optional[float]:
        if len(self._bars) < max(self.params.ema_len + 1, self.params.atr_len + 1, self.params.warmup_minutes):
            return None
        closes = [float(b[3]) for b in self._bars]
        ema_series = self._ema(closes, self.params.ema_len)
        if len(ema_series) < 2:
            return None
        atr_series = self._atr(list(self._bars), self.params.atr_len)
        ema_curr = ema_series[-1]
        ema_prev = ema_series[-2]
        atr_curr = max(1e-9, atr_series[-1])
        slope = (ema_curr - ema_prev) / atr_curr
        self._last_slope = slope
        return slope

    def _net_position_base(self) -> float:
        try:
            sym = str(self.symbol or "").upper()
            pos_map = getattr(self.connector, "_positions_by_symbol", {}) or {}
            pos = pos_map.get(sym)
            if not isinstance(pos, dict):
                return 0.0
            raw = pos.get("position")
            if raw is None:
                for key in ("netQuantity", "quantity", "size", "positionSize"):
                    if pos.get(key) is not None:
                        raw = pos.get(key)
                        break
            return float(raw) if raw is not None else 0.0
        except Exception:
            return 0.0

    async def _collect_telemetry(self) -> Dict[str, Any]:
        import json  # noqa: F401  # ensure available for dumps
        data: Dict[str, Any] = {}
        try:
            lat_ms = await self.connector.best_effort_latency_ms()
        except Exception:
            lat_ms = None
        q_base = self._net_position_base()
        # compute max position base from params
        try:
            size_i = int(self._size_i or self._min_size_i or 0)
            max_pos_base = (int(self.params.max_orders) * size_i) / float(self._size_scale or 1)
        except Exception:
            max_pos_base = None
        # attempt to fetch account overview for balances and pnl
        acct: Dict[str, Any] = {}
        pnl = None
        balance_total = None
        try:
            ov = await self.connector.get_account_overview()
            if isinstance(ov, dict):
                acct = ov
                # try common pnl fields
                for k in ("unrealized_pnl", "unrealizedPnl", "upnl", "u_pnl", "pnl"):
                    v = ov.get(k)
                    if v is not None:
                        try:
                            pnl = float(v)
                            break
                        except Exception:
                            pass
                # try balance/equity fields (prefer 'collateral' for total equity incl. margin)
                # 1) top-level keys on overview
                if balance_total is None:
                    for k in (
                        "collateral",              # preferred: total equity incl. margin (lighter)
                        "total_equity",
                        "equity",
                        "total_asset_value",       # lighter overview total
                        "cross_asset_value",       # lighter cross total
                        "balance",
                        "totalBalance",
                        "account_equity",
                        "available_balance",
                    ):
                        v = ov.get(k)
                        if v is not None:
                            try:
                                balance_total = float(v)
                                break
                            except Exception:
                                try:
                                    balance_total = float(str(v))
                                    break
                                except Exception:
                                    pass
                # 2) nested account[0] fallbacks
                if balance_total is None:
                    accounts = None
                    for key in ("accounts", "account", "sub_accounts"):
                        a = ov.get(key)
                        if isinstance(a, list) and a:
                            accounts = a
                            break
                    if isinstance(accounts, list) and accounts:
                        a0 = accounts[0] if isinstance(accounts[0], dict) else None
                        if isinstance(a0, dict):
                            for k in (
                                "collateral",
                                "total_equity",
                                "equity",
                                "total_asset_value",
                                "cross_asset_value",
                                "balance",
                                "totalBalance",
                                "account_equity",
                                "available_balance",
                            ):
                                v = a0.get(k)
                                if v is not None:
                                    try:
                                        balance_total = float(v)
                                        break
                                    except Exception:
                                        try:
                                            balance_total = float(str(v))
                                            break
                                        except Exception:
                                            pass
        except Exception:
            pass
        # also aggregate pnl from positions if not found
        if pnl is None:
            try:
                poss = await self.connector.get_positions()
                total_upnl = 0.0
                has = False
                for p in poss:
                    for k in ("unrealized_pnl", "unrealizedPnl", "upnl", "u_pnl"):
                        if p.get(k) is not None:
                            try:
                                total_upnl += float(p.get(k))
                                has = True
                                break
                            except Exception:
                                pass
                pnl = total_upnl if has else None
            except Exception:
                pass
        data = {
            "ts": time.time(),
            "symbol": self.symbol,
            "direction": self._direction,
            "position_base": q_base,
            "max_position_base": max_pos_base,
            "tp_active": len(self._tp_cois),
            "latency_ms": lat_ms,
            "pnl_unrealized": pnl,
            "balance_total": balance_total,
            "account": {
                "overview": acct,
            },
        }
        return data

    async def _telemetry_loop(self) -> None:
        import urllib.request
        import json
        url = str(getattr(self.params, "telemetry_url", ""))
        interval = max(5, int(getattr(self.params, "telemetry_interval_secs", 60) or 60))
        while self._started and url:
            try:
                payload = await self._collect_telemetry()
                body = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
                # run blocking I/O in thread
                await asyncio.to_thread(urllib.request.urlopen, req, timeout=5)
            except Exception as e:
                # avoid noisy logs; print concise warning
                self.log.debug(f"telemetry send failed: {e}") if hasattr(self, 'log') else None
            await asyncio.sleep(interval)

    async def _cancel_all_entries_and_tps(self) -> None:
        opens = await self.connector.get_open_orders(self.symbol)
        to_cancel: List[str] = []
        for o in opens:
            if not isinstance(o, dict):
                continue
            oi = self._order_id_of(o)
            if oi is None:
                continue
            to_cancel.append(oi)
        for oi in to_cancel:
            try:
                _tx, _ret, err = await self.connector.cancel_order(oi, symbol=self.symbol)
                if err:
                    self.log.warning(f"cancel-all error: oi={oi} err={err}")
            except Exception as e:
                self.log.warning(f"cancel-all exception: oi={oi} err={e}")
        if to_cancel:
            for _ in range(50):
                o2 = await self.connector.get_open_orders(self.symbol)
                if len(o2) == 0:
                    break
                await asyncio.sleep(0.1)

    async def _place_flush_tp(self, bid: int, ask: int) -> Optional[int]:
        """Place a single reduce-only TP near best to flatten current net position."""
        q = self._net_position_base()
        if abs(q) < 1e-9:
            return None
        size_i = max(1, int(round(abs(q) * self._size_scale)))
        size_i = self._quantize_size_i(size_i, prefer_up=False)
        if size_i < self._min_size_i:
            return None
        ticks = 1  # default 1 tick outside best
        try:
            ticks = max(1, int(getattr(self.params, "flush_ticks", 1)))
        except Exception:
            ticks = 1
        tick_i = max(1, int(self._price_tick_i or 1))
        offset_i = max(tick_i, int(ticks) * tick_i)
        if q > 0:  # long -> need to sell
            price_i = max(tick_i, (ask if ask is not None else tick_i) + offset_i)
            is_ask = True
            side = "long"
        else:  # short -> need to buy
            bid0 = bid if bid is not None else (ask - tick_i if ask is not None else tick_i)
            price_i = max(tick_i, bid0 - offset_i)
            is_ask = False
            side = "short"
        price_i = self._quantize_price_i(price_i, is_ask)
        cio, ret, err = await self._place_limit_with_retry(size_i, price_i, is_ask, post_only=True, reduce_only=1)
        if err:
            self.log.warning(f"flush TP rejected: {err}")
            return None
        if cio is not None:
            self._orders[cio] = {"type": "tp", "side": side, "flush": True}
            self._tp_cois.add(cio)
            self.log.info(f"flush TP placed: side={side} price_i={price_i} size_i={size_i} cio={cio}")
            return cio
        return None

    # --------------- orders ---------------
    async def _place_entry(self, side: str, best_bid: int, best_ask: int) -> Optional[int]:
        assert self._size_i is not None
        # price: just inside book as maker
        tick = max(1, int(self._price_tick_i or 1))
        if side == "long":
            ref = best_bid if best_bid is not None else (best_ask - tick if best_ask is not None else tick)
            ref = max(tick, ref or tick)
            price_raw_i = max(tick, ref - tick)
            is_ask = False
            if best_ask is not None:
                floor_allowed = max(tick, best_ask - tick)
                if price_raw_i < floor_allowed:
                    price_raw_i = floor_allowed
        else:
            ref = best_ask if best_ask is not None else (best_bid + tick if best_bid is not None else tick)
            ref = max(tick, ref or tick)
            price_raw_i = ref + tick
            is_ask = True
        price_i = self._quantize_price_i(price_raw_i, is_ask)
        size_raw_i = int(self._size_i)
        size_i = self._quantize_size_i(size_raw_i, prefer_up=True)
        if size_i < self._min_size_i:
            size_i = self._quantize_size_i(self._min_size_i, prefer_up=True)
        price_f = price_i / float(self._price_scale or 1)
        size_f = size_i / float(self._size_scale or 1)
        mid_f = None
        if best_bid is not None and best_ask is not None and best_bid > 0 and best_ask > 0:
            mid_f = ((best_bid + best_ask) / 2.0) / float(self._price_scale or 1)
        self.log.info(
            "entry intent: side=%s bid_i=%s ask_i=%s tick_i=%s step_i=%s price_raw_i=%s price_i=%s price=%.8f size_raw_i=%s size_i=%s size=%.8f mid=%.8f",
            side,
            best_bid,
            best_ask,
            self._price_tick_i,
            self._size_step_i,
            price_raw_i,
            price_i,
            price_f,
            size_raw_i,
            size_i,
            size_f,
            mid_f if mid_f is not None else float("nan"),
        )
        cio, ret, err = await self._place_limit_with_retry(size_i, price_i, is_ask, post_only=True, reduce_only=0)
        if err:
            diff_pct = None
            if mid_f and mid_f > 0:
                diff_pct = abs(price_f - mid_f) / mid_f * 100
            self.log.warning(
                "entry rejected: side=%s price_i=%s price=%.8f size_i=%s size=%.8f diff_pct=%.4f err=%s",
                side,
                price_i,
                price_f,
                size_i,
                size_f,
                diff_pct if diff_pct is not None else float("nan"),
                err,
            )
            return None
        if cio is not None:
            self._orders[cio] = {"type": "entry", "side": side, "entry_price_i": price_i}
            (self._entry_cois_long if side == "long" else self._entry_cois_short).add(cio)
            self.log.info(
                "entry placed: side=%s price_i=%s price=%.8f size_i=%s size=%.8f cio=%s",
                side,
                price_i,
                price_f,
                size_i,
                size_f,
                cio,
            )
            return cio
        return None

    def _desired_entry_price(self, side: str, best_bid: int, best_ask: int) -> int:
        tick = max(1, int(self._price_tick_i or 1))
        if side == "long":
            ref = best_bid if best_bid is not None else (best_ask - tick if best_ask is not None else tick)
            ref = max(tick, ref or tick)
            price_i = max(tick, ref - tick)
            return self._quantize_price_i(price_i, is_ask=False)
        ref = best_ask if best_ask is not None else (best_bid + tick if best_bid is not None else tick)
        ref = max(tick, ref or tick)
        price_i = ref + tick
        return self._quantize_price_i(price_i, is_ask=True)

    async def _requote_stale_entries(self, best_bid: int, best_ask: int) -> int:
        side = self._direction
        desired = self._desired_entry_price(side, best_bid, best_ask)
        # compute threshold in integer units
        tick_thresh = max(0, int(getattr(self.params, "requote_ticks", 0) or 0))
        abs_thresh_i = 0
        try:
            abs_thresh_i = int(round(float(getattr(self.params, "requote_abs", 0.0) or 0.0) * self._price_scale))
        except Exception:
            abs_thresh_i = 0
        thresh = max(tick_thresh, abs_thresh_i)
        if thresh <= 0:
            return 0
        # build open entries list for current side
        opens = await self.connector.get_open_orders(self.symbol)
        targets: List[Tuple[int, str, int, int]] = []  # list of (coi, order_id, old_price_i, dist)
        for o in opens:
            if not isinstance(o, dict):
                continue
            coi = self._client_order_index_of(o)
            oi = self._order_id_of(o)
            if coi is None or oi is None:
                continue
            meta = self._orders.get(coi)
            if not meta or meta.get("type") != "entry" or meta.get("side") != side:
                continue
            old_price_i = int(meta.get("entry_price_i", 0) or 0)
            dist = abs(desired - old_price_i)
            if dist >= thresh:
                # honor backoff for stubborn orders
                until = self._requote_skip_until.get(oi, 0.0)
                if time.time() < until:
                    continue
                targets.append((coi, oi, old_price_i, dist))
        if not targets:
            return 0
        # sort by worst distance first so we refresh most stale first
        targets.sort(key=lambda t: t[3], reverse=True)
        # limit per tick (scan more than limit to skip stubborn ones)
        limit = max(1, int(getattr(self.params, "max_requotes_per_tick", 1) or 1))
        replaced = 0
        attempts = 0
        max_attempts = min(len(targets), limit * 5)

        mode = str(getattr(self.params, "requote_mode", "cancel_first") or "cancel_first").strip().lower()
        if mode not in ("cancel_first", "place_first"):
            mode = "cancel_first"

        if mode == "place_first":
            dupes_created = 0
            dupes_cap = max(0, int(getattr(self.params, "max_requote_dupes", 0) or 0))
            for coi, oi, oldp, _dist in targets:
                if replaced >= limit or attempts >= max_attempts:
                    break
                if dupes_created >= dupes_cap:
                    break
                attempts += 1
                # place new first (at desired using current best bid/ask)
                new_cio = await self._place_entry(side, best_bid, best_ask)
                if new_cio is None:
                    # fallback to cancel-first for this oi
                    try:
                        _tx, _ret, err = await self.connector.cancel_order(oi, symbol=self.symbol)
                        if err:
                            self.log.warning(f"requote(place-first)->cancel-fallback error: oi={oi} err={err}")
                            c = 1 + int(self._requote_skip_count.get(oi, 0))
                            base = float(getattr(self.params, "requote_skip_backoff_secs", 15.0) or 15.0)
                            cap = float(getattr(self.params, "requote_skip_backoff_max_secs", 120.0) or 120.0)
                            delay = min(cap, base * (2 ** min(c, 4)))
                            self._requote_skip_until[oi] = time.time() + delay
                            self._requote_skip_count[oi] = c
                            continue
                    except Exception as e:
                        self.log.warning(f"requote(place-first)->cancel-fallback exception: oi={oi} err={e}")
                        c = 1 + int(self._requote_skip_count.get(oi, 0))
                        base = float(getattr(self.params, "requote_skip_backoff_secs", 15.0) or 15.0)
                        cap = float(getattr(self.params, "requote_skip_backoff_max_secs", 120.0) or 120.0)
                        delay = min(cap, base * (2 ** min(c, 4)))
                        self._requote_skip_until[oi] = time.time() + delay
                        self._requote_skip_count[oi] = c
                        continue
                    # after cancel fallback, attempt to place again at desired
                    new_cio = await self._place_entry(side, best_bid, best_ask)
                    if new_cio is None:
                        continue
                # cancel old after successfully placing new
                try:
                    _tx, _ret, err = await self.connector.cancel_order(oi, symbol=self.symbol)
                    if err:
                        self.log.warning(f"requote(place-first) cancel-error: oi={oi} err={err}")
                    # give ws a short grace period to reflect removal; avoid reprocessing immediately
                    grace = float(getattr(self.params, "requote_wait_confirm_secs", 5.0) or 1.0)
                    self._requote_skip_until[oi] = time.time() + max(0.2, min(grace, 2.0))
                except Exception as e:
                    self.log.warning(f"requote(place-first) cancel-exception: oi={oi} err={e}")
                    c = 1 + int(self._requote_skip_count.get(oi, 0))
                    base = float(getattr(self.params, "requote_skip_backoff_secs", 15.0) or 15.0)
                    cap = float(getattr(self.params, "requote_skip_backoff_max_secs", 120.0) or 120.0)
                    delay = min(cap, base * (2 ** min(c, 4)))
                    self._requote_skip_until[oi] = time.time() + delay
                    self._requote_skip_count[oi] = c
                # do not eagerly remove old coi from tracking; leave for _on_cancelled
                self.log.info(f"requote(place-first): side={side} old_price_i={oldp} -> desired={desired} cancelled_oi={oi} new_cio={new_cio}")
                replaced += 1
                dupes_created += 1
            return replaced

        # cancel-first (original) path
        for coi, oi, oldp, _dist in targets:
            if replaced >= limit or attempts >= max_attempts:
                break
            attempts += 1
            try:
                _tx, _ret, err = await self.connector.cancel_order(oi, symbol=self.symbol)
                if err:
                    self.log.warning(f"requote-cancel-error: oi={oi} err={err}")
                    # backoff this oi
                    c = 1 + int(self._requote_skip_count.get(oi, 0))
                    base = float(getattr(self.params, "requote_skip_backoff_secs", 15.0) or 15.0)
                    cap = float(getattr(self.params, "requote_skip_backoff_max_secs", 120.0) or 120.0)
                    delay = min(cap, base * (2 ** min(c, 4)))
                    self._requote_skip_until[oi] = time.time() + delay
                    self._requote_skip_count[oi] = c
                    continue
            except Exception as e:
                self.log.warning(f"requote-cancel-exception: oi={oi} err={e}")
                c = 1 + int(self._requote_skip_count.get(oi, 0))
                base = float(getattr(self.params, "requote_skip_backoff_secs", 15.0) or 15.0)
                cap = float(getattr(self.params, "requote_skip_backoff_max_secs", 120.0) or 120.0)
                delay = min(cap, base * (2 ** min(c, 4)))
                self._requote_skip_until[oi] = time.time() + delay
                self._requote_skip_count[oi] = c
                continue
            # wait for WS to reflect removal (best-effort)
            removed = False
            wait_secs = float(getattr(self.params, "requote_wait_confirm_secs", 5.0) or 5.0)
            loops = max(1, int(wait_secs / 0.1))
            for _ in range(loops):
                opens2 = await self.connector.get_open_orders(self.symbol)
                if not any(self._order_id_of(x) == oi for x in opens2 if isinstance(x, dict)):
                    removed = True
                    break
                await asyncio.sleep(0.1)
            if not removed:
                # skip placing new to avoid accumulation
                # extra validation: query REST active orders to verify presence
                exists = None
                try:
                    order_info, err = await self.connector.get_order(self.symbol, order_id=oi)
                    if err == "not_found" or err == "gone":
                        exists = False
                    else:
                        exists = bool(order_info)
                except Exception:
                    exists = None
                if exists is False:
                    self.log.warning(f"requote-skip-override: oi={oi} not in REST-active; treating as removed")
                    removed = True
                else:
                    # consider force-forget if abnormal persistence
                    now = time.time()
                    first = float(self._requote_first_seen_ts.get(oi) or 0.0)
                    if not first:
                        self._requote_first_seen_ts[oi] = now
                    c_prev = int(self._requote_skip_count.get(oi, 0))
                    max_retries = max(1, int(getattr(self.params, "requote_force_forget_after_retries", 3) or 3))
                    max_secs = float(getattr(self.params, "requote_force_forget_after_secs", 600.0) or 600.0)
                    if c_prev >= max_retries or (first and (now - first) >= max_secs):
                        self.log.warning(
                            f"requote-skip-force-removed: oi={oi} still open after retries={c_prev} age={now-first:.1f}s; treating as removed"
                        )
                        removed = True
                        # best-effort: fire a background cancel attempt again
                        try:
                            asyncio.create_task(self.connector.cancel_order(oi, symbol=self.symbol))
                        except Exception:
                            pass
                        # mark long backoff to avoid hammering
                        self._requote_skip_until[oi] = now + max(60.0, max_secs)
                    else:
                        self.log.warning(f"requote-skip: oi={oi} still open; will retry later")
                        c = c_prev + 1
                        base = float(getattr(self.params, "requote_skip_backoff_secs", 15.0) or 15.0)
                        cap = float(getattr(self.params, "requote_skip_backoff_max_secs", 120.0) or 120.0)
                        delay = min(cap, base * (2 ** min(c, 4)))
                        self._requote_skip_until[oi] = now + delay
                        self._requote_skip_count[oi] = c
                        continue
            # update tracking: remove old coi entry
            try:
                self._orders.pop(coi, None)
                (self._entry_cois_long if side == "long" else self._entry_cois_short).discard(coi)
                # clear first-seen/skip counters on finalize
                self._requote_first_seen_ts.pop(oi, None)
                self._requote_skip_until.pop(oi, None)
                self._requote_skip_count.pop(oi, None)
            except Exception:
                pass
            # place new at desired
            new_cio = await self._place_entry(side, best_bid, best_ask)
            self.log.info(f"requote: side={side} old_price_i={oldp} -> desired={desired} cancelled_oi={oi} new_cio={new_cio}")
            replaced += 1
        return replaced

    async def _place_tp_for_entry(self, entry_price_i: int, side: str) -> Optional[int]:
        assert self._size_i is not None
        tp_i = max(1, int(round(self.params.take_profit_abs * self._price_scale)))
        # snapshot current top-of-book for audit
        try:
            bid0, ask0, _ = await self.connector.get_top_of_book(self.symbol)
        except Exception:
            bid0 = ask0 = None
        if side == "long":
            price_i = entry_price_i + tp_i
            is_ask = True
        else:
            price_i = max(1, entry_price_i - tp_i)
            is_ask = False
        price_i = self._quantize_price_i(price_i, is_ask)
        size_i = self._quantize_size_i(int(self._size_i), prefer_up=False)
        if size_i < self._min_size_i:
            size_i = self._quantize_size_i(self._min_size_i, prefer_up=True)
        size_i = max(self._min_size_i, size_i)
        # intent log
        try:
            self.log.info(
                f"tp submit intent: side={side} entry={entry_price_i} tp_price_i={price_i} "
                f"post_only=1 ro=1 tob(bid/ask)={bid0}/{ask0}"
            )
        except Exception:
            pass
        cio, ret, err = await self._place_limit_with_retry(size_i, price_i, is_ask, post_only=True, reduce_only=1)
        if err:
            self.log.warning(f"tp for {side} rejected: {err}")
            return None
        if cio is not None:
            self._orders[cio] = {"type": "tp", "side": side, "entry_price_i": entry_price_i}
            self._tp_cois.add(cio)
            self.log.info(f"tp placed: side={side} entry={entry_price_i} tp_price_i={price_i} cio={cio}")
            # schedule a short delayed verification (WS + REST by COI), log-only
            async def _verify():
                try:
                    await asyncio.sleep(0.8)
                    # WS presence by COI
                    try:
                        opens = await self.connector.get_open_orders(self.symbol)
                        ws_hit = any(
                            self._client_order_index_of(o) == int(cio)
                            for o in opens
                            if isinstance(o, dict)
                        )
                        from collections import Counter
                        ws_hist = Counter(
                            str(o.get("status", "")).strip().lower()
                            for o in opens
                            if isinstance(o, dict)
                        )
                    except Exception:
                        ws_hit, ws_hist = False, {}
                    # REST presence by COI (market-aware)
                    try:
                        order_info, err = await self.connector.get_order(self.symbol, client_id=int(cio))
                        if err in {"not_found", "gone"}:
                            rest_hit = False
                        else:
                            rest_hit = bool(order_info)
                    except Exception:
                        rest_hit = None
                    self.log.info(
                        f"tp verify: coi={cio} ws_open={ws_hit} rest_open={rest_hit} ws_status_hist={dict(ws_hist)}"
                    )
                    # if both WS and REST do not see the order, treat as failed placement and clean tracking
                    if (ws_hit is False) and (rest_hit is False):
                        try:
                            self._tp_cois.discard(int(cio))
                            self._orders.pop(int(cio), None)
                            self.log.warning(f"tp verify failed; dropped local tracking: coi={cio}")
                        except Exception:
                            pass
                except Exception:
                    pass
            try:
                asyncio.create_task(_verify())
            except Exception:
                pass
            return cio
        return None

    def _sum_open_coverage_i(self, need_sell: bool, opens: List[Dict[str, Any]]) -> Tuple[int, int, int]:
        """Sum integer base units of open orders that would reduce the position.
        If need_sell=True, count asks; else count bids. Prefer explicit base_amount; fallback to size/amount.
        Returns: (total_i, count_matched, count_total)
        """
        def _infer_is_ask(o: Dict[str, Any]) -> bool:
            label = self._order_side_label(o)
            if label == "short":
                return True
            if label == "long":
                return False
            v = o.get("is_ask")
            if v is None:
                v = o.get("isAsk")
            if v is None:
                side = str(o.get("side", "")).strip().lower()
                if side in ("sell", "ask"):
                    return True
                if side in ("buy", "bid"):
                    return False
            try:
                if isinstance(v, str):
                    v = v.strip().lower()
                    if v in ("true", "1", "yes", "on"): return True
                    if v in ("false", "0", "no", "off"): return False
                    # numeric string
                    v = int(v)
                return bool(v)
            except Exception:
                return False

        def _amount_i(o: Dict[str, Any]) -> int:
            # candidate fields in order of preference
            for key in ("base_amount", "baseAmount", "size", "baseSize", "amount", "quantity"):
                if key in o and o.get(key) is not None:
                    val = o.get(key)
                    try:
                        if isinstance(val, int):
                            return max(0, int(val))
                        if isinstance(val, float):
                            return max(0, int(round(val * self._size_scale)))
                        s = str(val).strip()
                        if s.isdigit():
                            return max(0, int(s))
                        # float-like string
                        return max(0, int(round(float(s) * self._size_scale)))
                    except Exception:
                        continue
            # fallback to configured lot size
            return int(self._size_i or 0)

        total = 0
        matched = 0
        for o in opens:
            if not isinstance(o, dict):
                continue
            is_ask = _infer_is_ask(o)
            if need_sell != is_ask:
                continue
            matched += 1
            amt_i = self._order_amount_i(o)
            if amt_i is None:
                amt_i = _amount_i(o)
            total += amt_i
        return total, matched, len(opens)

    async def _tp_coverage_check_and_correct(self, bid: int, ask: int) -> bool:
        """Check coverage between net position and open reduce-only side coverage.
        If deficit >= one lot, place reduce-only TPs to correct, rate-limited.
        Returns True if any correction orders were placed."""
        q_base = self._net_position_base()
        net_i = int(round(abs(q_base) * self._size_scale))
        if net_i <= 0:
            return False
        opens = await self.connector.get_open_orders(self.symbol)
        covered_i, matched, total_opens = self._sum_open_coverage_i(need_sell=(q_base > 0), opens=opens)
        deficit_i = net_i - covered_i
        placed_any = False
        if deficit_i >= self._min_size_i:
            # log and place corrections with rate limit
            self.log.warning(
                f"imbalance detected: net_i={net_i} covered_i={covered_i} deficit_i={deficit_i} opens={total_opens} matched_reduce_orders={matched}"
            )
            # rate-limit corrections within 10 minutes
            now = time.time()
            ten_min_ago = now - 600.0
            while self._imb_corrections and self._imb_corrections[0] < ten_min_ago:
                self._imb_corrections.popleft()
            max_corr = max(0, int(getattr(self.params, "imb_max_corr_in_10m", 3) or 0))
            remaining = max(0, max_corr - len(self._imb_corrections))
            # how many lots needed
            lots_needed = max(1, deficit_i // max(1, self._min_size_i))
            to_place = min(remaining, int(lots_needed)) if max_corr > 0 else int(lots_needed)
            # safety cap per tick
            to_place = min(to_place, 3)
            if to_place <= 0:
                self.log.info("imbalance correction rate-limited; will retry later")
                return False
            tp_i = max(1, int(round(self.params.take_profit_abs * self._price_scale)))
            tick_i = max(1, int(self._price_tick_i or 1))
            for _ in range(to_place):
                if q_base > 0:  # long -> place sell reduce-only
                    price_i = max(1, (ask if ask is not None else (bid + tick_i if bid else tick_i)) + tp_i)
                    is_ask_flag = True
                    side = "long"
                else:  # short -> place buy reduce-only
                    price_i = max(1, (bid if bid is not None else (ask - tick_i if ask else tick_i)) - tp_i)
                    is_ask_flag = False
                    side = "short"
                price_i = self._quantize_price_i(price_i, is_ask_flag)
                amt_i = min(int(self._size_i or self._min_size_i), max(self._min_size_i, deficit_i))
                amt_i = self._quantize_size_i(amt_i, prefer_up=False)
                if amt_i < self._min_size_i:
                    amt_i = self._quantize_size_i(self._min_size_i, prefer_up=True)
                if amt_i < self._min_size_i:
                    self.log.info("imbalance TP skipped due to size below min lot")
                    break
                cio = int(time.time() * 1000) % 1_000_000
                cio, ret, err = await self._place_limit_with_retry(amt_i, price_i, is_ask_flag, post_only=True, reduce_only=1)
                if err:
                    self.log.warning(f"imbalance TP rejected: side={side} amt_i={amt_i} price_i={price_i} err={err}")
                    break
                if cio is None:
                    self.log.warning(f"imbalance TP failed without cio: side={side} amt_i={amt_i} price_i={price_i}")
                    break
                self._orders[cio] = {"type": "tp", "side": side, "imbalance": True}
                self._tp_cois.add(cio)
                self._imb_corrections.append(time.time())
                placed_any = True
                covered_i += amt_i
                deficit_i = max(0, net_i - covered_i)
                self.log.info(f"imbalance TP placed: side={side} amt_i={amt_i} price_i={price_i} coi={cio}")
                if deficit_i < self._min_size_i:
                    break
        else:
            # balanced or within one lot; just log
            mult = int(getattr(self.params, "imb_threshold_mult", 3) or 3)
            threshold = max(1, mult) * self._min_size_i
            self.log.info(
                f"coverage ok: net_i={net_i} covered_i={covered_i} (threshold={threshold}) opens={total_opens} matched_reduce_orders={matched}"
            )
        return placed_any

    async def _cancel_side_entries(self, side: str) -> None:
        opens = await self.connector.get_open_orders(self.symbol)
        to_cancel: List[str] = []
        for o in opens:
            if not isinstance(o, dict):
                continue
            coi = self._client_order_index_of(o)
            if coi is None:
                continue
            meta = self._orders.get(coi)
            if meta and meta.get("type") == "entry" and meta.get("side") == side:
                oi = self._order_id_of(o)
                if oi is not None:
                    to_cancel.append(oi)
        for oi in to_cancel:
            try:
                _tx, _ret, err = await self.connector.cancel_order(oi, symbol=self.symbol)
                if err:
                    self.log.warning(f"cancel-side error: side={side} oi={oi} err={err}")
            except Exception as e:
                self.log.warning(f"cancel-side exception: side={side} oi={oi} err={e}")
        if to_cancel:
            for _ in range(30):
                opens2 = await self.connector.get_open_orders(self.symbol)
                if not any(
                    self._orders.get(self._client_order_index_of(x) or -1, {}).get("type") == "entry"
                    and self._orders.get(self._client_order_index_of(x) or -1, {}).get("side") == side
                    for x in opens2
                    if isinstance(x, dict)
                ):
                    break
                await asyncio.sleep(0.1)

    # --------------- timing ---------------
    def _calculate_wait_time(self) -> int:
        """Return 0 to place now, 1 to wait a tick based on active close orders."""
        wait_time = float(self.params.base_wait)
        # optionally exclude imbalance-cover TPs to avoid over-throttling entries
        if getattr(self.params, "pace_ignore_imbalance_tp", True):
            active_close = 0
            for coi in list(self._tp_cois):
                meta = self._orders.get(int(coi))
                if meta is None:
                    active_close += 1
                elif meta.get("type") == "tp" and not meta.get("imbalance"):
                    active_close += 1
        else:
            active_close = len(self._tp_cois)
        max_orders = max(1, int(self.params.max_orders))

        if active_close < self._last_close_orders:
            self._last_close_orders = active_close
            return 0

        self._last_close_orders = active_close
        if active_close >= max_orders:
            return 1

        ratio = active_close / max_orders
        if ratio >= (2.0 / 3.0):
            cool_down = 2.0 * wait_time
        elif ratio >= (1.0 / 3.0):
            cool_down = wait_time
        elif ratio >= (1.0 / 6.0):
            cool_down = wait_time / 2.0
        else:
            cool_down = 60.0
        # apply min/max caps if provided
        try:
            mn = float(getattr(self.params, "min_wait", 0.0) or 0.0)
            mx = float(getattr(self.params, "max_wait", 0.0) or 0.0)
            if mn > 0:
                cool_down = max(cool_down, mn)
            if mx > 0:
                cool_down = min(cool_down, mx)
        except Exception:
            pass

        if self._last_entry_time <= 0:
            self.log.info(
                f"wait-calc: active_close={active_close}/{max_orders} ratio={ratio:.2f} cool_down={cool_down:.1f}s elapsed=NA(no fills yet)"
            )
            return 0

        elapsed = time.time() - float(self._last_entry_time)
        self.log.info(
            f"wait-calc: active_close={active_close}/{max_orders} ratio={ratio:.2f} cool_down={cool_down:.1f}s elapsed={elapsed:.1f}s"
        )
        if elapsed > cool_down:
            return 0
        else:
            return 1

    # --------------- main tick ---------------
    async def on_tick(self, now_ms: float):
        if not self._started:
            return
        await self._ensure_ready()

        if not self._hydrated_open_orders:
            await self._hydrate_existing_orders()
            self._hydrated_open_orders = True

        bid, ask, scale = await self.connector.get_top_of_book(self.symbol)
        if bid is None or ask is None or scale <= 0:
            return
        mid_i = (bid + ask) // 2
        self._price_scale = scale
        self._on_price(mid_i, int(now_ms))

        # disable auto trend switching; honor fixed user direction
        slope = None
        old_dir = self._direction
        # ensure _direction remains fixed according to params
        self._direction = (str(self.params.fixed_direction).strip().lower() if getattr(self.params, "fixed_direction", None) else self._direction)
        if self._direction not in ("long", "short"):
            self._direction = old_dir
        self.log.info(
            f"tick: flush={self._flush_mode} dir={self._direction} slope={slope if slope is not None else 'NA'} tp_active={len(self._tp_cois)} entries(L/S)={len(self._entry_cois_long)}/{len(self._entry_cois_short)}"
        )
        # since direction is fixed, no FLUSH on slope changes

        # FLUSH mode: prioritize flattening, no new entries
        if self._flush_mode:
            # place a single flush TP if still have net position and none pending
            if abs(self._net_position_base()) > 1e-9:
                if not self._tp_cois:
                    await self._place_flush_tp(bid, ask)
                return
            # flat: exit flush and switch to pending direction
            self._flush_mode = False
            if self._pending_direction is not None:
                self._direction = self._pending_direction
            self._pending_direction = None
            self._immediate_entry = True
            self.log.info("FLUSH complete: flat -> resume entries with new direction")
            return

        # coverage check outside FLUSH; log-only (no auto-correction)
        try:
            _ = await self._tp_coverage_check_and_correct(bid, ask)
        except Exception:
            pass

        # refresh stale entries to keep near top-of-book
        try:
            nreq = await self._requote_stale_entries(bid, ask)
            if nreq:
                self.log.info(f"requote-done: count={nreq}")
        except Exception as e:
            self.log.warning(f"requote error: {e}")

        # decide placing new entry
        # cap by active close orders (positions needing TP)
        if len(self._tp_cois) >= self.params.max_orders:
            return
        if self._has_pending_entry(self._direction):
            self.log.debug("entry skip: pending entry order exists for side=%s", self._direction)
            return
        if self._immediate_entry or self._calculate_wait_time() == 0:
            cio = await self._place_entry(self._direction, best_bid=bid, best_ask=ask)
            self._immediate_entry = False
