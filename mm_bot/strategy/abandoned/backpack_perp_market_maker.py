import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .strategy_base import StrategyBase
from mm_bot.connector.backpack import BackpackConnector


@dataclass
class PerpMarketMakerParams:
    symbol: str
    base_spread_pct: float = 0.2
    order_quantity: Optional[float] = None
    max_orders: int = 3
    target_position: float = 0.0
    max_position: float = 1.0
    position_threshold: float = 0.1
    inventory_skew: float = 0.0
    tick_interval_secs: float = 15.0
    cancel_before_place: bool = True
    post_only: bool = True


class BackpackPerpMarketMakerStrategy(StrategyBase):
    def __init__(
        self,
        connector: BackpackConnector,
        params: PerpMarketMakerParams,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.backpack = connector
        self.params = params
        self.log = logger or logging.getLogger("mm_bot.strategy.backpack_perp_mm")

        self.core = None
        self._initialized: bool = False
        self._price_precision: int = 4
        self._quantity_precision: int = 4
        self._tick_size: float = 0.0
        self._step_size: float = 0.0
        self._min_qty: float = 0.0
        self._base_asset: str = "BASE"
        self._quote_asset: str = "QUOTE"
        self._price_scale: int = 10 ** self._price_precision
        self._quantity_scale: int = 10 ** self._quantity_precision

        self._last_tick_ms: float = 0.0
        seed = int(time.time() * 1000) % 1_000_000
        self._cio_counter: int = seed if seed > 0 else 1

        self._latest_position: float = 0.0
        self._session_trades: int = 0
        self._session_volume: float = 0.0

        self._lock = asyncio.Lock()

    # lifecycle -----------------------------------------------------------------
    def start(self, core: Any) -> None:
        self.core = core
        self.backpack.set_event_handlers(
            on_order_filled=self._handle_order_event,
            on_order_cancelled=None,
            on_trade=self._handle_trade_event,
            on_position_update=self._handle_position_update,
        )
        self.log.info(
            "Backpack perp MM initialized with symbol=%s spread=%.4f%% max_orders=%s",
            self.params.symbol,
            self.params.base_spread_pct,
            self.params.max_orders,
        )

    def stop(self) -> None:
        try:
            self.backpack.set_event_handlers()
        except Exception:
            pass

    # tick loop -----------------------------------------------------------------
    async def on_tick(self, now_ms: float):
        if (now_ms - self._last_tick_ms) < (self.params.tick_interval_secs * 1000.0):
            return
        if not self._lock.locked():
            await self._run_cycle(now_ms)

    async def _run_cycle(self, now_ms: float) -> None:
        async with self._lock:
            self._last_tick_ms = now_ms
            try:
                await self._ensure_initialized()
            except Exception as exc:
                self.log.error("failed to initialize market data: %s", exc)
                return

            net_position = await self._get_net_position()
            if await self._manage_position(net_position):
                return

            price_bands = await self._calculate_prices(net_position)
            if price_bands is None:
                return
            buy_prices, sell_prices, mid_price = price_bands

            order_qty = self._resolve_order_quantity()
            if order_qty <= 0:
                self.log.warning("order quantity %.8f below minimum %.8f", order_qty, self._min_qty)
                return

            if self.params.cancel_before_place:
                await self._cancel_existing()

            await self._place_side_orders(buy_prices, is_ask=False, quantity=order_qty)
            await self._place_side_orders(sell_prices, is_ask=True, quantity=order_qty)

            self.log.debug(
                "cycle complete mid=%.6f net=%.6f qty=%.6f", mid_price, net_position, order_qty
            )

    # initialization helpers ----------------------------------------------------
    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        info = await self.backpack.get_market_info(self.params.symbol)
        self._price_precision = int(info.get("price_precision", 4))
        self._quantity_precision = int(info.get("quantity_precision", 4))
        self._tick_size = float(info.get("tick_size") or 0.0)
        if self._tick_size <= 0:
            self._tick_size = 1 / (10 ** self._price_precision)
        self._step_size = float(info.get("step_size") or 0.0)
        if self._step_size <= 0:
            self._step_size = 1 / (10 ** self._quantity_precision)
        self._min_qty = max(float(info.get("min_qty") or 0.0), self._step_size)
        self._base_asset = info.get("base_asset") or self._base_asset
        self._quote_asset = info.get("quote_asset") or self._quote_asset
        self._price_scale = 10 ** self._price_precision
        self._quantity_scale = 10 ** self._quantity_precision
        self._initialized = True
        self.log.info(
            "market info loaded: tick_size=%s step_size=%s min_qty=%s base=%s quote=%s",
            self._tick_size,
            self._step_size,
            self._min_qty,
            self._base_asset,
            self._quote_asset,
        )

    # market data ---------------------------------------------------------------
    async def _calculate_prices(self, net_position: float) -> Optional[Tuple[List[float], List[float], float]]:
        bid_i, ask_i, scale = await self.backpack.get_top_of_book(self.params.symbol)
        if not bid_i or not ask_i:
            depth = await self.backpack.get_order_book(self.params.symbol, depth=5)
            bids = depth.get("bids") or []
            asks = depth.get("asks") or []
            try:
                bid_price = float(bids[0][0]) if bids else None
                ask_price = float(asks[0][0]) if asks else None
            except Exception:
                return None
        else:
            bid_price = bid_i / float(scale)
            ask_price = ask_i / float(scale)

        if bid_price is None or ask_price is None:
            return None
        mid_price = (bid_price + ask_price) / 2
        spread = mid_price * (self.params.base_spread_pct / 100)
        base_buy = mid_price - spread / 2
        base_sell = mid_price + spread / 2

        base_buy = self._round_price(base_buy)
        base_sell = self._round_price(base_sell)

        buy_prices = []
        sell_prices = []
        for idx in range(max(1, self.params.max_orders)):
            gradient = (idx ** 1.5) * self._tick_size * 1.5
            buy_prices.append(self._round_price(base_buy - gradient))
            sell_prices.append(self._round_price(base_sell + gradient))

        if self.params.inventory_skew > 0 and self.params.max_position > 0:
            skew_ratio = max(-1.0, min(1.0, net_position / self.params.max_position))
            skew_offset = mid_price * self.params.inventory_skew * skew_ratio
            adjusted_buys = [self._round_price(price - skew_offset) for price in buy_prices]
            adjusted_sells = [self._round_price(price - skew_offset) for price in sell_prices]
            if adjusted_buys[0] < adjusted_sells[0]:
                buy_prices = adjusted_buys
                sell_prices = adjusted_sells
            else:
                self.log.debug("inventory skew adjustment skipped due to crossed prices")

        return buy_prices, sell_prices, mid_price

    # order management ----------------------------------------------------------
    async def _cancel_existing(self) -> None:
        try:
            _, _, err = await self.backpack.cancel_all(self.params.symbol)
            if err:
                self.log.warning("cancel_all returned error: %s", err)
        except Exception as exc:
            self.log.error("cancel_all failed: %s", exc)

    async def _place_side_orders(self, prices: List[float], *, is_ask: bool, quantity: float) -> None:
        size_f = self._sanitize_quantity(quantity)
        if size_f <= 0:
            return
        size_i = self._quantity_to_int(size_f)
        for price in prices:
            price_i = self._price_to_int(price)
            if price_i <= 0:
                continue
            coi = self._next_client_order_index()
            try:
                _tx, _ret, err = await self.backpack.place_limit(
                    self.params.symbol,
                    client_order_index=coi,
                    base_amount=size_i,
                    price=price_i,
                    is_ask=is_ask,
                    post_only=self.params.post_only,
                    reduce_only=0,
                )
                if err:
                    self.log.warning("limit order rejected price=%.6f qty=%.6f err=%s", price, size_f, err)
                else:
                    self.log.debug(
                        "limit order placed side=%s price=%.6f qty=%.6f coi=%s",
                        "ask" if is_ask else "bid",
                        price,
                        size_f,
                        coi,
                    )
            except Exception as exc:
                self.log.error("limit order exception price=%.6f qty=%.6f err=%s", price, size_f, exc)

    async def _manage_position(self, net_position: float) -> bool:
        abs_net = abs(net_position)
        if abs_net > self.params.max_position:
            qty_to_close = abs_net - self.params.max_position
            return await self._close_position(qty_to_close, net_position)
        deviation = abs_net - abs(self.params.target_position)
        if deviation > self.params.position_threshold:
            qty_to_close = deviation
            return await self._close_position(qty_to_close, net_position)
        return False

    async def _close_position(self, qty: float, net_position: float) -> bool:
        qty_sanitized = self._sanitize_quantity(qty)
        if qty_sanitized <= 0:
            return False
        side_is_ask = net_position > 0
        size_i = self._quantity_to_int(qty_sanitized)
        try:
            _tx, _ret, err = await self.backpack.place_market(
                self.params.symbol,
                client_order_index=self._next_client_order_index(),
                base_amount=size_i,
                is_ask=side_is_ask,
                reduce_only=1,
            )
            if err:
                self.log.error("reduce-only market failed qty=%.6f err=%s", qty_sanitized, err)
                return False
            self.log.info(
                "reduce position %s qty=%.6f", "sell" if side_is_ask else "buy", qty_sanitized
            )
            return True
        except Exception as exc:
            self.log.error("reduce-only market exception qty=%.6f err=%s", qty_sanitized, exc)
            return False

    # utilities -----------------------------------------------------------------
    async def _get_net_position(self) -> float:
        try:
            positions = await self.backpack.get_positions()
        except Exception:
            positions = []
        symbol_upper = self.params.symbol.upper()
        for entry in positions:
            sym = str(entry.get("symbol") or entry.get("market") or "").upper()
            if sym != symbol_upper:
                continue
            for key in ("netQuantity", "position", "size", "amount", "net"):
                val = entry.get(key)
                if val is None:
                    continue
                try:
                    self._latest_position = float(val)
                    return self._latest_position
                except (TypeError, ValueError):
                    continue
        return self._latest_position

    def _resolve_order_quantity(self) -> float:
        if self.params.order_quantity is not None:
            return max(self.params.order_quantity, self._min_qty)
        # fallback: allocate 25% of max position per side
        target = max(self.params.max_position * 0.25, self._min_qty)
        return target

    def _sanitize_quantity(self, qty: float) -> float:
        if qty <= 0:
            return 0.0
        step = self._step_size
        if step <= 0:
            step = 1 / (10 ** self._quantity_precision)
        steps = max(1, math.floor(qty / step))
        sanitized = steps * step
        sanitized = round(sanitized, self._quantity_precision)
        if sanitized < self._min_qty:
            sanitized = self._min_qty
        return sanitized

    def _round_price(self, price: float) -> float:
        tick = self._tick_size
        if tick <= 0:
            tick = 1 / (10 ** self._price_precision)
        steps = round(price / tick)
        rounded = steps * tick
        return round(rounded, self._price_precision)

    def _price_to_int(self, price: float) -> int:
        return int(round(price * self._price_scale))

    def _quantity_to_int(self, quantity: float) -> int:
        return int(round(quantity * self._quantity_scale))

    def _next_client_order_index(self) -> int:
        self._cio_counter = (self._cio_counter + 1) % 1_000_000
        if self._cio_counter == 0:
            self._cio_counter = 1
        return self._cio_counter

    # event handlers ------------------------------------------------------------
    def _handle_order_event(self, payload: Dict[str, Any]) -> None:
        status = str(payload.get("status") or "").lower()
        if status == "filled":
            qty = payload.get("filledQuantity") or payload.get("l") or payload.get("quantity")
            try:
                qty_f = float(qty)
            except (TypeError, ValueError):
                qty_f = 0.0
            if qty_f > 0:
                self._session_trades += 1
                self._session_volume += qty_f

    def _handle_trade_event(self, payload: Dict[str, Any]) -> None:
        pass

    def _handle_position_update(self, payload: Dict[str, Any]) -> None:
        try:
            qty = payload.get("position") or payload.get("netQuantity") or payload.get("q")
            if qty is None:
                return
            self._latest_position = float(qty)
        except (TypeError, ValueError):
            return
