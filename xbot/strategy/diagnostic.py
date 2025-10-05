from __future__ import annotations

from decimal import Decimal
from typing import Optional

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
                # Try fetch and then cancel (also log raw order if available)
                await self.router.fetch_order(sym, placed_coi)
                try:
                    raw = await self.router.orders._connector.get_order(venue_symbol, placed_coi)  # type: ignore[attr-defined]
                    self._logger.info("order_raw", extra={"coi": placed_coi, "raw": raw})
                except Exception:
                    pass
                await self.router.cancel(sym, placed_coi)
                self._logger.info("limit_order_cancelled", extra={"coi": placed_coi})
        except Exception:
            # Include stacktrace for better diagnostics
            self._logger.exception("private_ops_skipped")

        # Account snapshots (if available)
        try:
            positions = await self.router.positions.get_positions()
            self._logger.info("positions_ok", extra={"count": len(positions)})
        except Exception as exc:
            self._logger.info("positions_unavailable", extra={"error": str(exc)})

        try:
            margin = await self.router.fetch_margin()
            self._logger.info("margin_ok", extra={"has_data": bool(margin)})
        except Exception as exc:
            self._logger.info("margin_unavailable", extra={"error": str(exc)})

        self._logger.info("diagnostic_done", extra={"symbol": sym})


__all__ = ["DiagnosticStrategy"]
