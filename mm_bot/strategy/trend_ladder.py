import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple
from collections import deque

from mm_bot.strategy.strategy_base import StrategyBase
from mm_bot.connector.lighter.lighter_exchange import LighterConnector


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
        connector: LighterConnector,
        symbol: Optional[str] = None,
        params: Optional[TrendLadderParams] = None,
    ):
        self.log = logging.getLogger("mm_bot.strategy.trend_ladder")
        self.connector = connector
        self.symbol = symbol
        self.params = params or TrendLadderParams()

        self._core: Any = None
        self._started = False

        # market scales
        self._market_id: Optional[int] = None
        self._size_i: Optional[int] = None
        self._price_scale: int = 1
        self._size_scale: int = 1
        self._min_size_i: int = 1

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
        self._did_initial_cleanup: bool = False
        self._last_close_orders: int = 0
        self._entry_filled_i_long: int = 0
        self._entry_filled_i_short: int = 0
        self._trade_lock: asyncio.Lock = asyncio.Lock()
        # imbalance correction rate-limit
        self._imb_corrections: Deque[float] = deque(maxlen=100)
        # telemetry task
        self._telemetry_task: Optional[asyncio.Task] = None
        # requote backoff for stubborn orders (by order_index)
        self._requote_skip_until: Dict[int, float] = {}
        self._requote_skip_count: Dict[int, int] = {}
        # first-seen timestamp for stuck oi during requote
        self._requote_first_seen_ts: Dict[int, float] = {}

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
        cio = int(time.time() * 1000) % 1_000_000
        last_err = None
        for attempt in range(max(1, retries)):
            try:
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
            coi = int(info.get("client_order_index", -1))
        except Exception:
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
            asyncio.create_task(self._place_tp_for_entry(entry_i, side))
        elif typ == "tp":
            # TP done -> schedule an immediate new entry if direction matches
            self._tp_cois.discard(coi)
            self._immediate_entry = True

    def _on_cancelled(self, info: Dict[str, Any]) -> None:
        try:
            coi = int(info.get("client_order_index", -1))
        except Exception:
            return
        meta = self._orders.pop(coi, None)
        if not meta:
            return
        if meta.get("type") == "entry":
            (self._entry_cois_long if meta.get("side") == "long" else self._entry_cois_short).discard(coi)
        elif meta.get("type") == "tp":
            self._tp_cois.discard(coi)

    # --------------- setup helpers ---------------
    async def _ensure_ready(self) -> None:
        if self._market_id is not None and self.symbol is not None and self._size_i is not None:
            return
        await self.connector.start_ws_state()
        await self.connector._ensure_markets()
        if self.symbol is None:
            sym = next((s for s in self.connector._symbol_to_market.keys() if s.upper().startswith("BTC")), None)
            if sym is None:
                raise RuntimeError("No BTC symbol available")
            self.symbol = sym
        self._market_id = await self.connector.get_market_id(self.symbol)
        # scales
        _p_dec, s_dec = await self.connector.get_price_size_decimals(self.symbol)
        self._price_scale = 10 ** _p_dec
        self._size_scale = 10 ** s_dec
        # min size and desired per-order size
        ob_list = await self.connector.order_api.order_books()
        entry = next((e for e in ob_list.order_books if int(e.market_id) == self._market_id), None)
        if entry is None:
            raise RuntimeError("Market entry not found")
        min_size_i = max(1, int(round(float(entry.min_base_amount) * (10 ** s_dec))))
        self._min_size_i = min_size_i
        want_size_i = int(round(float(self.params.quantity_base) * (10 ** s_dec)))
        self._size_i = max(min_size_i, want_size_i)
        self.log.info(f"TrendLadder ready: symbol={self.symbol} market_id={self._market_id} size_i(min)={min_size_i} size_i(use)={self._size_i}")

    def _trade_amount_i(self, t: Dict[str, Any]) -> int:
        for key in ("base_amount", "baseAmount", "size", "baseSize", "amount", "quantity"):
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
        for k in ("client_order_index","clientOrderIndex","coi"):
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
            # compute TP price near current book +/- take_profit_abs
            bid, ask, scale = await self.connector.get_top_of_book(self.symbol)
            tp_i = max(1, int(round(self.params.take_profit_abs * self._price_scale)))
            if side == "long":
                price_i = max(1, (ask if ask is not None else (bid + 1 if bid else 1)) + tp_i)
                is_ask_flag = True
            else:
                price_i = max(1, (bid if bid is not None else (ask - 1 if ask else 1)) - tp_i)
                is_ask_flag = False
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
            pos = self.connector._positions_by_market.get(int(self._market_id or -1))
            raw = pos.get("position") if isinstance(pos, dict) else None
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
        to_cancel: List[int] = []
        for o in opens:
            try:
                oi = int(o.get("order_index"))
            except Exception:
                continue
            to_cancel.append(oi)
        for oi in to_cancel:
            try:
                _tx, _ret, err = await self.connector.cancel_order(oi, market_index=self._market_id)
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
        ticks = 1  # default 1 tick outside best
        try:
            ticks = max(1, int(getattr(self.params, "flush_ticks", 1)))
        except Exception:
            ticks = 1
        if q > 0:  # long -> need to sell
            price_i = max(1, (ask if ask is not None else 1) + ticks)
            is_ask = True
            side = "long"
        else:  # short -> need to buy
            bid0 = bid if bid is not None else (ask - 1 if ask is not None else 1)
            price_i = max(1, bid0 - ticks)
            is_ask = False
            side = "short"
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
        if side == "long":
            price_i = max(1, min(best_bid, best_ask - 1) - 1)
            is_ask = False
        else:
            price_i = max(best_bid + 1, best_ask + 1)
            is_ask = True
        cio, ret, err = await self._place_limit_with_retry(int(self._size_i), price_i, is_ask, post_only=True, reduce_only=0)
        if err:
            self.log.warning(f"entry {side} rejected: {err}")
            return None
        if cio is not None:
            self._orders[cio] = {"type": "entry", "side": side, "entry_price_i": price_i}
            (self._entry_cois_long if side == "long" else self._entry_cois_short).add(cio)
            self.log.info(f"entry placed: side={side} price_i={price_i} cio={cio}")
            return cio
        return None

    def _desired_entry_price(self, side: str, best_bid: int, best_ask: int) -> int:
        if side == "long":
            return max(1, min(best_bid, best_ask - 1) - 1)
        else:
            return max(best_bid + 1, best_ask + 1)

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
        targets: List[Tuple[int,int,int,int]] = []  # list of (coi, oi, old_price_i, dist)
        for o in opens:
            try:
                coi = int(o.get("client_order_index", -1))
                oi = int(o.get("order_index", -1))
            except Exception:
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
                        _tx, _ret, err = await self.connector.cancel_order(oi, market_index=self._market_id)
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
                    _tx, _ret, err = await self.connector.cancel_order(oi, market_index=self._market_id)
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
                _tx, _ret, err = await self.connector.cancel_order(oi, market_index=self._market_id)
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
                if not any(int(x.get("order_index", -1)) == oi for x in opens2):
                    removed = True
                    break
                await asyncio.sleep(0.1)
            if not removed:
                # skip placing new to avoid accumulation
                # extra validation: query REST active orders to verify presence
                exists = None
                try:
                    exists = await self.connector.is_order_open(oi, market_index=self._market_id)
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
                            asyncio.create_task(self.connector.cancel_order(oi, market_index=self._market_id))
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
        # intent log
        try:
            self.log.info(
                f"tp submit intent: side={side} entry={entry_price_i} tp_price_i={price_i} "
                f"post_only=1 ro=1 tob(bid/ask)={bid0}/{ask0}"
            )
        except Exception:
            pass
        cio, ret, err = await self._place_limit_with_retry(int(self._size_i), price_i, is_ask, post_only=True, reduce_only=1)
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
                        ws_hit = any(int(o.get("client_order_index", -1)) == int(cio) for o in opens)
                        from collections import Counter
                        ws_hist = Counter(str(o.get("status", "")).strip().lower() for o in opens)
                    except Exception:
                        ws_hit, ws_hist = False, {}
                    # REST presence by COI (market-aware)
                    try:
                        rest_hit = await self.connector.is_coi_open(int(cio), market_index=self._market_id)
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
            is_ask = _infer_is_ask(o)
            if need_sell != is_ask:
                continue
            matched += 1
            total += _amount_i(o)
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
            for _ in range(to_place):
                if q_base > 0:  # long -> place sell reduce-only
                    price_i = max(1, (ask if ask is not None else (bid + 1 if bid else 1)) + tp_i)
                    is_ask_flag = True
                    side = "long"
                else:  # short -> place buy reduce-only
                    price_i = max(1, (bid if bid is not None else (ask - 1 if ask else 1)) - tp_i)
                    is_ask_flag = False
                    side = "short"
                amt_i = min(int(self._size_i or self._min_size_i), max(self._min_size_i, deficit_i))
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
        to_cancel: List[int] = []
        for o in opens:
            try:
                coi = int(o.get("client_order_index", -1))
            except Exception:
                continue
            meta = self._orders.get(coi)
            if meta and meta.get("type") == "entry" and meta.get("side") == side:
                try:
                    oi = int(o.get("order_index"))
                    to_cancel.append(oi)
                except Exception:
                    pass
        for oi in to_cancel:
            try:
                _tx, _ret, err = await self.connector.cancel_order(oi, market_index=self._market_id)
                if err:
                    self.log.warning(f"cancel-side error: side={side} oi={oi} err={err}")
            except Exception as e:
                self.log.warning(f"cancel-side exception: side={side} oi={oi} err={e}")
        if to_cancel:
            for _ in range(30):
                opens2 = await self.connector.get_open_orders(self.symbol)
                if not any(self._orders.get(int(x.get("client_order_index", -1)), {}).get("type") == "entry" and self._orders.get(int(x.get("client_order_index", -1)), {}).get("side") == side for x in opens2):
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

        # one-time cleanup
        if not self._did_initial_cleanup:
            try:
                await self.connector.cancel_all()
            except Exception:
                pass
            for _ in range(50):
                o2 = await self.connector.get_open_orders(self.symbol)
                if len(o2) == 0:
                    break
                await asyncio.sleep(0.1)
            self._did_initial_cleanup = True

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
        if self._immediate_entry or self._calculate_wait_time() == 0:
            cio = await self._place_entry(self._direction, best_bid=bid, best_ask=ask)
            if cio is not None:
                self._last_entry_time = time.time()
            self._immediate_entry = False
