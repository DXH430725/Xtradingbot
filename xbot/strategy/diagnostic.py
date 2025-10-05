from __future__ import annotations

from decimal import Decimal
from typing import Optional, Dict, Any

from xbot.core.clock import WallClock
from xbot.execution.router import ExecutionRouter
from .base import Strategy, StrategyConfig
from ..utils.logging import get_logger


class DiagnosticStrategy(Strategy):
    """Validates connector capabilities with minimal live interactions.

    Steps:
    - Fetch price/size decimals and minimum order size.
    - Fetch top-of-book.
    - If private API available, place a post-only limit order, query, then cancel.
    - Read positions and margin snapshot.
    """

    def __init__(self, *, router: ExecutionRouter, clock: WallClock, config: StrategyConfig) -> None:
        super().__init__(router=router, clock=clock, config=config)
        self._logger = get_logger(__name__)

    async def start(self) -> None:
        await super().start()
        sym = self.config.symbol
        self._logger.info("diagnostic_start", extra={"symbol": sym})

        # Public capabilities
        venue_symbol = self.router.market_data.resolve_symbol(sym)
        price_dec, size_dec = await self.router.market_data.get_price_size_decimals(sym)
        min_size_i = await self.router.market_data.get_min_size_i(sym)
        bid_i, ask_i, scale = await self.router.market_data.get_top_of_book(sym)
        self._logger.info(
            "md_ok",
            extra={
                "symbol": sym,
                "venue_symbol": venue_symbol,
                "price_dec": price_dec,
                "size_dec": size_dec,
                "min_size_i": min_size_i,
                "bid_i": bid_i,
                "ask_i": ask_i,
                "scale": scale,
            },
        )

        # Private capabilities (best-effort)
        placed_coi: Optional[int] = None
        try:
            if bid_i and ask_i:
                # Place a post-only order away from market to avoid fills
                offset_ticks = max(1, int(self.config.price_offset_ticks or 10))
                is_ask = self.config.side == "sell"
                # Use minimum size for safety
                size_i = min_size_i
                # Price selection: move outside spread in the intended direction
                ref = ask_i if is_ask else bid_i
                price_i = ref + (offset_ticks if is_ask else -offset_ticks)
                await self._dump_ws("before_initial_limit")
                self._logger.info(
                    "private_plan",
                    extra={
                        "venue_symbol": venue_symbol,
                        "is_ask": is_ask,
                        "offset_ticks": offset_ticks,
                        "size_i": size_i,
                        "ref_price_i": ref,
                        "submit_price_i": price_i,
                        "post_only": True,
                    },
                )
                order = await self.router.submit_limit(
                    symbol=sym,
                    is_ask=is_ask,
                    size_i=size_i,
                    price_i=price_i,
                    post_only=True,
                )
                placed_coi = order.client_order_index
                self._logger.info(
                    "limit_order_open",
                    extra={
                        "coi": placed_coi,
                        "exchange_order_id": order.exchange_order_id,
                        "size_i": size_i,
                        "price_i": price_i,
                    },
                )
                await self._dump_ws("after_initial_limit")
                # Try fetch and then cancel (also log raw order if available)
                await self.router.fetch_order(sym, placed_coi)
                try:
                    raw = await self.router.orders._connector.get_order(venue_symbol, placed_coi)  # type: ignore[attr-defined]
                    self._logger.info("order_raw", extra={"coi": placed_coi, "raw": raw})
                except Exception:
                    pass
                await self.router.cancel(sym, placed_coi)
                cancel_info = {}
                try:
                    cancel_info = order.history[-1].info if order.history else {}
                except Exception:
                    pass
                self._logger.info(
                    "limit_order_cancelled",
                    extra={"coi": placed_coi, "cancel_response": cancel_info.get("cancel_response")},
                )
                await self._dump_ws("after_cancel_initial_limit")
                # Print order state machine/history
                hist = [e.to_dict() for e in order.history]
                self._logger.info("order_history", extra={"coi": placed_coi, "history": hist})
        except Exception:
            # Include stacktrace for better diagnostics
            self._logger.exception("private_ops_skipped")

        # Account snapshots (if available)
        try:
            positions = await self.router.positions.all_positions()
            self._logger.info("positions_ok", extra={"count": len(list(positions))})
        except Exception as exc:
            self._logger.info("positions_unavailable", extra={"error": str(exc)})

        try:
            margin = await self.router.fetch_margin()
            self._logger.info("margin_ok", extra={"has_data": bool(margin)})
        except Exception as exc:
            self._logger.info("margin_unavailable", extra={"error": str(exc)})

        # Extended diagnostic: run tracking-limit until fill, then close with market
        try:
            if bid_i and ask_i:
                is_ask_open = self.config.side == "sell"
                size_i = await self.router.market_data.to_size_i(sym, Decimal(str(self.config.qty)))
                await self._dump_ws("before_tracking_limit")

                async def observer(kind: str, ctx: Dict[str, Any]) -> None:
                    await self._dump_ws(f"tracking_{kind}", extra=ctx)

                tracking = await self.router.tracking_limit(
                    symbol=sym,
                    base_amount_i=size_i,
                    is_ask=is_ask_open,
                    price_offset_ticks=self.config.price_offset_ticks or 0,
                    interval_secs=self.config.interval_secs,
                    timeout_secs=self.config.timeout_secs,
                    max_attempts=9999,
                    observer=observer,
                )
                await tracking.wait_final()
                # Log attempts/state machine
                attempts = [
                    {
                        "attempt": a.attempt,
                        "coi": a.client_order_index,
                        "price_i": a.price_i,
                        "state": a.state.value,
                        "info": a.info,
                    }
                    for a in tracking.attempts
                ]
                self._logger.info(
                    "tracking_done",
                    extra={"attempts": attempts, "filled_base_i": tracking.filled_base_i},
                )
                await self._dump_ws("after_tracking_limit")

                # Close with market order
                await self._dump_ws("before_close_market")
                close_order = await self.router.submit_market(
                    symbol=sym,
                    is_ask=not is_ask_open,
                    size_i=size_i,
                    reduce_only=1,
                )
                self._logger.info(
                    "close_market_submitted",
                    extra={
                        "coi": close_order.client_order_index,
                        "exchange_order_id": close_order.exchange_order_id,
                    },
                )
                await self._dump_ws("after_close_market")
                # Print close order history if any updates persisted
                close_hist = [e.to_dict() for e in close_order.history]
                self._logger.info("order_history", extra={"coi": close_order.client_order_index, "history": close_hist})
        except Exception:
            self._logger.exception("extended_diagnostic_failed")

        self._logger.info("diagnostic_done", extra={"symbol": sym})

    async def _dump_ws(self, phase: str, *, extra: Optional[Dict[str, Any]] = None) -> None:
        cache = self.router.cache
        if cache is None:
            return
        try:
            positions = await cache.snapshot_positions()
            trades = await cache.snapshot_trades(self.router.market_data.resolve_symbol(self.config.symbol))
            balances = await cache.snapshot_balances()
            payload: Dict[str, Any] = {"phase": phase, "positions": positions, "trades": trades, "balances": balances}
            if extra:
                payload.update(extra)
            self._logger.info("ws_snapshot", extra=payload)
        except Exception:
            self._logger.exception("ws_snapshot_error")


__all__ = ["DiagnosticStrategy"]
