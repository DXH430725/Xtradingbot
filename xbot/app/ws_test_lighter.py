from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from typing import Optional

from xbot.connector.factory import build_connector
from xbot.connector.interface import IConnector
from xbot.connector.lighter_ws import LighterWsClient
from xbot.core.cache import MarketCache
from xbot.execution.order_service import OrderUpdatePayload
from xbot.utils.logging import setup_logging, get_logger


async def run(symbol: str, *, key_file: Optional[str], log_level: str = "INFO") -> None:
    setup_logging(log_level)
    logger = get_logger(__name__)

    connector: IConnector = build_connector("lighter")
    await connector.start()
    try:
        market_index = getattr(connector, "get_market_index")(symbol)  # type: ignore[misc]
        account_index = getattr(connector, "get_account_index")()  # type: ignore[misc]
        logger.info(
            "ws_test_lighter_boot", extra={"market_index": market_index, "account_index": account_index}
        )

        cache = MarketCache()

        async def on_update(p: OrderUpdatePayload) -> None:
            logger.info(
                "ws_test_order_update",
                extra={
                    "coi": p.client_order_index,
                    "state": p.state.value,
                    "ex_id": p.exchange_order_id,
                    "keys": list(p.info.keys()) if isinstance(p.info, dict) else None,
                },
            )

        key_path = Path(key_file) if key_file else Path(os.getenv("LIGHTER_KEY_FILE", str(Path.cwd() / "Lighter_key.txt")))
        ws = LighterWsClient(
            market_index=market_index,
            venue_symbol=symbol,
            account_index=account_index,
            cache=cache,
            key_file=key_path,
            on_order_update=on_update,
        )
        await ws.start()
        try:
            while True:
                await asyncio.sleep(5)
                top = await cache.get_top_of_book(symbol)
                if top is not None:
                    b, a = top
                    logger.info("ws_test_top", extra={"bid": b, "ask": a})
        finally:
            await ws.stop()
    finally:
        await connector.stop()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Lighter WS test harness (account orders + top-of-book)")
    p.add_argument("--symbol", required=True, help="venue symbol, e.g. SOL")
    p.add_argument("--key-file")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(run(args.symbol, key_file=args.key_file, log_level=args.log_level))


if __name__ == "__main__":
    main()

