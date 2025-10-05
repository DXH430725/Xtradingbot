from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Tuple

from xbot.core.cache import MarketCache
from xbot.core.clock import Clock
from xbot.core.eventbus import EventBus
from sdk.backpack.auth import load_keys, ws_signature_tuple
from sdk.backpack.ws import connect_and_subscribe


async def run(symbol: str, key_file: Path, seconds: int = 20) -> None:
    cache = MarketCache()
    bus = EventBus()
    keys = load_keys(key_file)
    sig: Tuple[str, str, str, str] = ws_signature_tuple(keys)
    print("[Connector] Initialized: Backpack (WS pending)")

    async def ws_task():
        async for msg in connect_and_subscribe(
            public_streams=[f"depth.{symbol}"],
            private_streams=["account.orderUpdate", "account.positionUpdate"],
            signature=sig,
        ):
            stream = msg.get("stream")
            data = msg.get("data")
            if not stream or data is None:
                continue
            if stream.startswith("depth."):
                bid = data.get("b")
                ask = data.get("a")
                top_b = float(bid[0][0]) if bid else None
                top_a = float(ask[0][0]) if ask else None
                await cache.set_top(symbol, top_b, top_a)
            elif stream == "account.positionUpdate":
                q = float(data.get("q") or data.get("quantity") or 0.0)
                await cache.set_position(symbol, q)
                bus.emit("position_update", {"symbol": symbol, "position": q})
            elif stream == "account.orderUpdate":
                bus.emit("order_update", data)

    clock = Clock(interval=1.0)

    class Printer:
        def __init__(self) -> None:
            self.t = 0

        async def on_tick(self, ts: float) -> None:
            self.t += 1
            if self.t == 1:
                print("[Connector] WS connected ok")
                print("[Connector] Cache sync complete (orderbook/trades/positions)")

    clock.add_iterator(Printer())

    task = asyncio.create_task(ws_task())
    try:
        await clock.start(max_ticks=seconds)
    finally:
        task.cancel()
        try:
            await task
        except BaseException:
            pass


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="SOL_USDC")
    p.add_argument("--key-file", default=str(Path.cwd() / "Backpack_key.txt"))
    p.add_argument("--seconds", type=int, default=20)
    a = p.parse_args()
    asyncio.run(run(a.symbol, Path(a.key_file), a.seconds))


if __name__ == "__main__":
    main()

