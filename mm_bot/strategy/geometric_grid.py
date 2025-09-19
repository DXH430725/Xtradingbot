import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from mm_bot.strategy.strategy_base import StrategyBase


@dataclass
class GeometricGridParams:
    # Trading range [low, high]
    range_low: float = 100.0
    range_high: float = 200.0
    # Total grid levels between low and high (inclusive of bounds)
    levels: int = 20
    # Max active orders per side selected at start (fixed selection unless recenter_on_move=True)
    orders_per_side: int = 5
    # Grid mode: neutral | long | short
    mode: str = "neutral"
    # Total quote allocation for grid (approximate, for sizing). If None, use min order size.
    quote_allocation: Optional[float] = None
    # Post-only limits by default
    post_only: bool = True
    # Optional: if True, re-center active selection when mid moves by requote_ticks
    recenter_on_move: bool = False
    requote_ticks: int = 1
    # Telemetry (optional; not implemented here)
    telemetry_enabled: bool = False
    telemetry_url: Optional[str] = None
    telemetry_interval_secs: int = 30


class GeometricGridStrategy(StrategyBase):
    def __init__(self, connector: Any, symbol: Optional[str], params: Optional[GeometricGridParams] = None):
        self.log = logging.getLogger("mm_bot.strategy.geometric_grid")
        self.connector = connector
        self.symbol = symbol
        self.params = params or GeometricGridParams()

        self._core: Any = None
        self._started: bool = False

        # Market/scales
        self._market_id: Optional[int] = None
        self._price_scale: int = 1
        self._size_scale: int = 1
        self._min_size_i: int = 1

        # State
        self._grid_prices_i: List[int] = []  # all target grid level prices (ints)
        self._price_to_index: Dict[int, int] = {}
        self._last_mid_i: Optional[int] = None
        self._last_mid_tick: Optional[int] = None
        self._last_pos_i: Optional[int] = None
        self._fixed_initialized: bool = False

        # Fixed active selection (price integers)
        self._active_buy_levels: set[int] = set()
        self._active_sell_levels: set[int] = set()

        # Orders tracked by client_order_index
        self._orders: Dict[int, Dict[str, Any]] = {}
        self._next_coi: int = 1_000_000

    # ----- lifecycle -----
    def start(self, core: Any) -> None:
        self._core = core
        self._started = True
        # subscribe to events to keep cache in sync
        self.connector.set_event_handlers(
            on_order_filled=self._on_filled,
            on_order_cancelled=self._on_cancelled,
        )

    def stop(self) -> None:
        self._started = False

    # ----- events -----
    def _on_filled(self, info: Dict[str, Any]) -> None:
        try:
            coi = int(info.get("client_order_index", -1))
        except Exception:
            return
        meta = self._orders.get(coi)
        if meta is None:
            # unknown; nothing we can do
            return
        price_i = int(meta.get("price_i", 0))
        is_ask = bool(meta.get("is_ask", False))
        # remove from cache first
        self._orders.pop(coi, None)
        # schedule follow-ups
        try:
            asyncio.create_task(self._handle_fill_followups(price_i, is_ask))
        except Exception:
            pass

    def _on_cancelled(self, info: Dict[str, Any]) -> None:
        try:
            coi = int(info.get("client_order_index", -1))
        except Exception:
            return
        self._orders.pop(coi, None)

    # ----- helpers -----
    async def _ensure_ready(self) -> None:
        if self._market_id is not None and self.symbol is not None:
            return
        await self.connector.start_ws_state()
        # Symbol discovery
        if self.symbol is None:
            # Best-effort pick a BTC-like market
            await self.connector._ensure_markets()  # type: ignore[attr-defined]
            sym = next((s for s in self.connector._symbol_to_market.keys() if str(s).upper().startswith("BTC")), None)  # type: ignore[attr-defined]
            if sym is None:
                raise RuntimeError("No default symbol available; please configure symbol")
            self.symbol = sym
        # Market id and scales
        self._market_id = await self.connector.get_market_id(self.symbol)
        p_dec, s_dec = await self.connector.get_price_size_decimals(self.symbol)
        self._price_scale = 10 ** int(p_dec)
        self._size_scale = 10 ** int(s_dec)
        # Min order size (integer units)
        try:
            self._min_size_i = await self.connector.get_min_order_size_i(self.symbol)
        except Exception:
            self._min_size_i = 1
        # Build initial grid levels
        self._grid_prices_i = self._build_grid_prices()
        self._price_to_index = {pi: idx for idx, pi in enumerate(self._grid_prices_i)}
        self.log.info(
            f"GeometricGrid ready: symbol={self.symbol} mid_scale={self._price_scale} size_scale={self._size_scale} min_size_i={self._min_size_i} levels={len(self._grid_prices_i)}"
        )

    def _build_grid_prices(self) -> List[int]:
        lo = float(self.params.range_low)
        hi = float(self.params.range_high)
        n = int(self.params.levels)
        n = max(2, n)
        if hi <= lo:
            hi = lo * 1.001
        # geometric ratio r so that lo * r^n = hi => r = (hi/lo)^(1/n)
        r = (hi / lo) ** (1.0 / n)
        prices: List[int] = []
        for k in range(0, n + 1):
            pk = lo * (r ** k)
            pi = int(round(pk * self._price_scale))
            # ensure strictly increasing integer sequence
            if not prices or pi > prices[-1]:
                prices.append(pi)
        return prices

    def _budget_size_i(self, price_i: int, per_side_slots: int) -> int:
        # If quote budget provided, split across both sides and slots
        if self.params.quote_allocation and self.params.quote_allocation > 0 and per_side_slots > 0:
            per_order_quote = float(self.params.quote_allocation) / float(2 * per_side_slots)
            price = price_i / float(self._price_scale)
            size_base = per_order_quote / max(price, 1e-9)
            size_i = int(max(self._min_size_i, round(size_base * self._size_scale)))
            return size_i
        # Otherwise use minimum size
        return int(self._min_size_i)

    def _extract_order_price_i(self, od: Dict[str, Any]) -> Optional[int]:
        v = od.get("price") if isinstance(od, dict) else None
        if v is None:
            return None
        try:
            if isinstance(v, int):
                return v
            s = str(v)
            if s.isdigit():
                return int(s)
            # floats as string: remove dot
            if "." in s:
                return int(s.replace(".", ""))
            return int(float(s))
        except Exception:
            return None

    async def _sync_open_orders_cache(self) -> None:
        # Build local cache from connector.get_open_orders
        try:
            open_list = await self.connector.get_open_orders(self.symbol)
        except Exception:
            open_list = []
        known = {}
        for od in open_list or []:
            try:
                # Identify coi and side
                coi = int(od.get("client_order_index", od.get("clientOrderIndex", 0)) or 0)
                if coi == 0:
                    # fabricate if missing
                    oi = od.get("order_index") or od.get("orderIndex") or 0
                    coi = int(oi) if isinstance(oi, int) else int(oi) if str(oi).isdigit() else 0
                is_ask = bool(od.get("is_ask", od.get("isAsk", False)))
                price_i = self._extract_order_price_i(od)
                if price_i is None:
                    continue
                order_index = od.get("order_index") or od.get("orderIndex")
                try:
                    order_index = int(order_index) if order_index is not None else None
                except Exception:
                    order_index = None
                known[coi] = {"price_i": price_i, "is_ask": is_ask, "order_index": order_index}
            except Exception:
                continue
        self._orders = known

    async def _current_position_i(self) -> int:
        try:
            poss = await self.connector.get_positions()
        except Exception:
            return 0
        for p in poss or []:
            try:
                sym = p.get("symbol") or p.get("instrument")
                if sym == self.symbol:
                    v = p.get("position") or p.get("base_position") or p.get("basePosition")
                    if v is None:
                        return 0
                    if isinstance(v, int):
                        return v
                    if isinstance(v, float):
                        return int(round(v * self._size_scale))
                    s = str(v)
                    if s.isdigit():
                        return int(s)
                    return int(round(float(s) * self._size_scale))
            except Exception:
                continue
        return 0

    def _coi(self) -> int:
        self._next_coi += 1
        return self._next_coi

    async def _place_limit(self, price_i: int, is_ask: bool, size_i: int, *, reduce_only: int = 0) -> None:
        coi = self._coi()
        _tx, _ret, err = await self.connector.place_limit(
            symbol=self.symbol,
            client_order_index=coi,
            base_amount=size_i,
            price=price_i,
            is_ask=is_ask,
            post_only=bool(self.params.post_only),
            reduce_only=reduce_only,
        )
        if err is None:
            self._orders[coi] = {"price_i": price_i, "is_ask": is_ask}
        else:
            self.log.warning(f"place_limit failed: {err}")

    async def _ensure_grid_orders(self) -> None:
        # Ensure orders exist at fixed active levels; do not re-center unless recenter_on_move=True
        per_side = max(1, int(self.params.orders_per_side))
        # current open sets
        cur_buys = set(pi for _, m in self._orders.items() if not m.get("is_ask") for pi in [m.get("price_i")])
        cur_sells = set(pi for _, m in self._orders.items() if m.get("is_ask") for pi in [m.get("price_i")])

        # Cancel only orders outside global range
        cancel_tasks = []
        for coi, m in list(self._orders.items()):
            pi = int(m.get("price_i", 0))
            in_range = (self._grid_prices_i[0] <= pi <= self._grid_prices_i[-1])
            if not in_range:
                oi = m.get("order_index")
                if oi is not None:
                    cancel_tasks.append(self.connector.cancel_order(int(oi), market_index=self._market_id))
        if cancel_tasks:
            try:
                await asyncio.gather(*cancel_tasks, return_exceptions=True)
            except Exception:
                pass

        # Place any missing orders at the fixed selection
        for pi in sorted(self._active_buy_levels):
            if pi not in cur_buys:
                sz_i = self._budget_size_i(pi, per_side)
                await self._place_limit(pi, is_ask=False, size_i=sz_i)
        for pi in sorted(self._active_sell_levels):
            if pi not in cur_sells:
                sz_i = self._budget_size_i(pi, per_side)
                await self._place_limit(pi, is_ask=True, size_i=sz_i)

    def _select_initial_levels(self, mid_i: int) -> None:
        mode = str(getattr(self.params, "mode", "neutral")).strip().lower()
        per_side = max(1, int(self.params.orders_per_side))
        below = [p for p in self._grid_prices_i if p < mid_i]
        above = [p for p in self._grid_prices_i if p > mid_i]
        if mode == "long":
            self._active_buy_levels = set(list(reversed(below))[:per_side])
            self._active_sell_levels = set()
        elif mode == "short":
            self._active_buy_levels = set()
            self._active_sell_levels = set(above[:per_side])
        else:
            # neutral
            self._active_buy_levels = set(list(reversed(below))[:per_side])
            self._active_sell_levels = set(above[:per_side])

    async def _handle_fill_followups(self, price_i: int, is_ask: bool) -> None:
        # On fill, place TP on the next grid step and re-arm the level if part of active selection
        idx = self._price_to_index.get(price_i)
        if idx is None:
            return
        mode = str(getattr(self.params, "mode", "neutral")).strip().lower()
        # Determine TP direction
        if is_ask:
            # filled a sell -> TP buy one step below
            tp_idx = idx - 1
            tp_is_ask = False
        else:
            # filled a buy -> TP sell one step above
            tp_idx = idx + 1
            tp_is_ask = True
        if 0 <= tp_idx < len(self._grid_prices_i):
            tp_price = self._grid_prices_i[tp_idx]
            size_i = self._budget_size_i(price_i, max(1, int(self.params.orders_per_side)))
            # For TP, set reduce_only to avoid increasing exposure
            await self._place_limit(tp_price, is_ask=tp_is_ask, size_i=size_i, reduce_only=1)
        # Re-arm the same level according to mode and active selection
        if price_i in (self._active_sell_levels if is_ask else self._active_buy_levels):
            size_i = self._budget_size_i(price_i, max(1, int(self.params.orders_per_side)))
            await self._place_limit(price_i, is_ask=is_ask, size_i=size_i)

    # ----- main loop -----
    async def on_tick(self, now_ms: float):
        if not self._started:
            return
        await self._ensure_ready()

        # Top of book
        bid_i, ask_i, scale = await self.connector.get_top_of_book(self.symbol)
        if bid_i is None or ask_i is None or scale <= 0:
            return
        mid_i = (bid_i + ask_i) // 2
        self._price_scale = int(scale)

        # Rebuild grid levels if scale changed or params changed significantly
        if not self._grid_prices_i:
            self._grid_prices_i = self._build_grid_prices()

        # Initialize fixed selection once (no implicit re-centering)
        refresh = False
        if not self._fixed_initialized:
            self._select_initial_levels(mid_i)
            self._fixed_initialized = True
            refresh = True
        elif getattr(self.params, "recenter_on_move", False):
            moved = abs(mid_i - int(self._last_mid_i or mid_i))
            if moved >= max(1, int(self.params.requote_ticks)):
                self._select_initial_levels(mid_i)
                refresh = True
        self._last_mid_i = mid_i

        # Position sync check
        try:
            pos_i = await self._current_position_i()
            if self._last_pos_i is not None and pos_i != self._last_pos_i:
                # If we missed events, resync open orders cache
                await self._sync_open_orders_cache()
            self._last_pos_i = pos_i
        except Exception:
            pass

        if refresh:
            await self._sync_open_orders_cache()
            await self._ensure_grid_orders()
