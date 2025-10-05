from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
from pathlib import Path

from xbot.connector.factory import build_connector
from xbot.execution.market_data_service import MarketDataService
from xbot.execution.order_service import OrderService
from xbot.execution.position_service import PositionService
from xbot.execution.risk_service import RiskService
from xbot.execution.tracking_limit import TrackingLimitEngine
from xbot.utils.logging import setup_logging, get_logger


async def run(venue: str, symbol: str, qty: float, offset_ticks: int) -> None:
    setup_logging("INFO")
    logger = get_logger(__name__)
    connector = build_connector(venue)
    market_data = MarketDataService(connector=connector, symbol_map={symbol.upper(): symbol})
    position_service = PositionService()
    risk_service = RiskService(market_data=market_data, position_service=position_service)
    tracking_engine = TrackingLimitEngine(market_data=market_data)
    orders = OrderService(
        connector=connector,
        market_data=market_data,
        risk_service=risk_service,
        tracking_engine=tracking_engine,
    )
    await connector.start()
    try:
        price_dec, size_dec = await market_data.get_price_size_decimals(symbol)
        min_size_i = await market_data.get_min_size_i(symbol)
        bid_i, ask_i, _ = await market_data.get_top_of_book(symbol)
        if not bid_i or not ask_i:
            raise RuntimeError("top of book unavailable")
        size_i = max(min_size_i, await market_data.to_size_i(symbol, Decimal(str(qty))))
        # Place far from market to avoid fill: for buy, price below bid; for sell, price above ask
        is_ask = False
        ref = bid_i
        price_i = max(1, ref - abs(int(offset_ticks)))
        logger.info(
            "cancel_test_plan",
            extra={
                "symbol": symbol,
                "size_i": size_i,
                "price_i": price_i,
                "offset_ticks": offset_ticks,
            },
        )
        order = await orders.submit_limit(
            symbol=symbol,
            is_ask=is_ask,
            size_i=size_i,
            price_i=price_i,
            post_only=True,
        )
        logger.info(
            "cancel_test_open",
            extra={
                "coi": order.client_order_index,
                "exchange_order_id": order.exchange_order_id,
                "price_i": price_i,
                "size_i": size_i,
            },
        )
        # Small delay to ensure exchange registers the order
        await asyncio.sleep(2.0)
        venue_symbol = market_data.resolve_symbol(symbol)
        if order.exchange_order_id:
            resp = await connector.cancel_by_order_id(venue_symbol, order.exchange_order_id)  # type: ignore[attr-defined]
        else:
            resp = await connector.cancel_by_client_id(venue_symbol, order.client_order_index)
        logger.info("cancel_test_response", extra={"coi": order.client_order_index, "resp": resp})
    finally:
        await connector.stop()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--venue", required=True)
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", type=float, default=0.01)
    p.add_argument("--offset-ticks", type=int, default=100)
    a = p.parse_args()
    asyncio.run(run(a.venue, a.symbol, a.qty, a.offset_ticks))


if __name__ == "__main__":
    main()

