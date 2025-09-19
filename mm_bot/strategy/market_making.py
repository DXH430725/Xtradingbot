import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from mm_bot.strategy.strategy_base import StrategyBase
from mm_bot.connector.lighter.lighter_exchange import LighterConnector


class MarketMakingMid100Strategy(StrategyBase):
    """
    Simple performance-testing strategy:
    - On each tick: cancel all existing open orders (individually) for symbol, then place 1 bid and 1 ask at mid ± 100 (integer price units), post-only
    - Expect almost no fills; mainly stress event/cancel flow and WS cache correctness.
    """

    def __init__(
        self,
        connector: LighterConnector,
        symbol: Optional[str] = None,
        price_offset_abs: float = 100.0,
    ):
        self.log = logging.getLogger("mm_bot.strategy.market_making")
        self.connector = connector
        self.symbol = symbol
        # desired absolute price offset in human units (e.g., $100)
        self.price_offset_abs = float(price_offset_abs)

        self._core = None
        self._started = False

        # state & metrics
        self.tick_counter = 0
        self.cancelled_count = 0
        self.filled_count = 0
        self._last_cios: Tuple[Optional[int], Optional[int]] = (None, None)
        self._size_i: Optional[int] = None
        self._price_dec: Optional[int] = None
        self._offset_i: Optional[int] = None
        self._market_id: Optional[int] = None
        self._did_initial_cleanup: bool = False

        # event snapshots
        self._events = {"cancelled": [], "filled": []}

    def start(self, core: Any) -> None:
        self._core = core
        self._started = True
        # attach WS state maintenance and event handlers
        # done lazily in on_tick (async) for safety
        self.connector.set_event_handlers(
            on_order_cancelled=lambda info: self._on_cancelled(info),
            on_order_filled=lambda info: self._on_filled(info),
        )

    def stop(self) -> None:
        self._started = False

    def _on_cancelled(self, info: Dict):
        self.cancelled_count += 1
        try:
            self._events["cancelled"].append(dict(info))
        except Exception:
            pass

    def _on_filled(self, info: Dict):
        self.filled_count += 1
        try:
            self._events["filled"].append(dict(info))
        except Exception:
            pass

    async def _ensure_ready(self) -> None:
        if self._market_id is not None and self.symbol is not None and self._size_i is not None:
            return
        # ensure WS running
        await self.connector.start_ws_state()
        # resolve symbol if not provided (prefer BTC*)
        await self.connector._ensure_markets()
        if self.symbol is None:
            sym = next((s for s in self.connector._symbol_to_market.keys() if s.upper().startswith("BTC")), None)
            if sym is None:
                raise RuntimeError("No BTC symbol available")
            self.symbol = sym
        self._market_id = await self.connector.get_market_id(self.symbol)
        # compute size scale from market; offset_i will be recomputed each tick based on top-of-book scale
        _p_dec, s_dec = await self.connector.get_price_size_decimals(self.symbol)
        ob_list = await self.connector.order_api.order_books()
        entry = next((e for e in ob_list.order_books if int(e.market_id) == self._market_id), None)
        if entry is None:
            raise RuntimeError("Market entry not found")
        self._size_i = max(1, int(float(entry.min_base_amount) * (10 ** s_dec)))
        self.log.info(f"Strategy ready: symbol={self.symbol} market_id={self._market_id} size_i(min)={self._size_i} offset_abs={self.price_offset_abs}")

    async def _cancel_existing_orders(self) -> int:
        cancelled = 0
        opens = await self.connector.get_open_orders(self.symbol)
        self.log.info(f"tick cancel: opens={len(opens)}")
        # cancel individually to exercise sendTx rate/weight
        for o in list(opens):
            try:
                oi = int(o.get("order_index"))
            except Exception:
                continue
            try:
                await self.connector.cancel_order(oi, market_index=self._market_id)
                cancelled += 1
            except Exception:
                pass
        return cancelled

    async def _place_two_sides(self, mid_i: int) -> Tuple[Optional[int], Optional[int]]:
        assert self._size_i is not None and self._offset_i is not None
        bid_price = max(1, int(mid_i - self._offset_i))
        ask_price = max(bid_price + 1, int(mid_i + self._offset_i))
        try:
            self.log.info(f"place two sides: mid_i={mid_i} offset_i={self._offset_i} -> bid_i={bid_price} ask_i={ask_price}")
        except Exception:
            pass
        now_tag = int(time.time() * 1000) % 1_000_000
        cio_bid = (now_tag + 1) % 1_000_000
        cio_ask = (now_tag + 2) % 1_000_000
        # place as post-only
        try:
            _tx, ret, err = await self.connector.place_limit(self.symbol, cio_bid, self._size_i, price=bid_price, is_ask=False, post_only=True)
            if err:
                self.log.warning(f"place bid rejected: {err}")
                cio_bid = None
        except Exception as e:
            self.log.warning(f"place bid error: {e}")
            cio_bid = None
        try:
            _tx, ret, err = await self.connector.place_limit(self.symbol, cio_ask, self._size_i, price=ask_price, is_ask=True, post_only=True)
            if err:
                self.log.warning(f"place ask rejected: {err}")
                cio_ask = None
        except Exception as e:
            self.log.warning(f"place ask error: {e}")
            cio_ask = None
        return cio_bid, cio_ask

    async def on_tick(self, now_ms: float):
        if not self._started:
            return
        await self._ensure_ready()

        # one-time cleanup to eliminate any legacy opens from previous runs
        if not self._did_initial_cleanup:
            try:
                await self.connector.cancel_all()
            except Exception:
                pass
            for _ in range(50):
                opens0 = await self.connector.get_open_orders(self.symbol)
                if len(opens0) == 0:
                    break
                await asyncio.sleep(0.1)
            self._did_initial_cleanup = True

        # compute mid from top-of-book and dynamic integer scale
        best_bid, best_ask, scale = await self.connector.get_top_of_book(self.symbol)
        if best_bid is None or best_ask is None or scale <= 0:
            self.log.info("order book incomplete; skip tick")
            return
        mid_i = (int(best_bid) + int(best_ask)) // 2
        self._offset_i = max(1, int(round(self.price_offset_abs * scale)))

        # cancel existing then place new ones
        _c = await self._cancel_existing_orders()
        # wait for WS cache to reflect cancellations (best-effort)
        for _ in range(20):
            opens = await self.connector.get_open_orders(self.symbol)
            if len(opens) == 0:
                break
            await asyncio.sleep(0.1)

        cio_bid, cio_ask = await self._place_two_sides(mid_i)
        # if only one side placed, cancel it to avoid imbalance
        if (cio_bid is None) ^ (cio_ask is None):
            try:
                opens = await self.connector.get_open_orders(self.symbol)
                for o in opens:
                    coi = int(o.get("client_order_index", -1))
                    if (cio_bid is not None and coi == cio_bid) or (cio_ask is not None and coi == cio_ask):
                        oi = int(o.get("order_index"))
                        await self.connector.cancel_order(oi)
            except Exception:
                pass
        self._last_cios = (cio_bid, cio_ask)
        self.tick_counter += 1

    # validation helpers --------------------------------------------------------
    async def snapshot(self) -> Dict[str, Any]:
        opens = await self.connector.get_open_orders(self.symbol)
        return {
            "ticks": self.tick_counter,
            "filled": self.filled_count,
            "cancelled": self.cancelled_count,
            "open_orders": len(opens),
            "open_client_order_indices": [int(o.get("client_order_index", -1)) for o in opens],
            "last_cios": list(self._last_cios),
        }
