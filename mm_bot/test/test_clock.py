import asyncio
import time
import os
import sys

import pytest


# ensure project root on sys.path for imports
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mm_bot.core.clock import SimpleClock


def test_simple_clock_ticks_and_stops():
    async def _run():
        count = 0

        async def on_tick(_now_ms: float):
            nonlocal count
            count += 1

        clk = SimpleClock(tick_size=0.05)
        clk.add_tick_handler(on_tick)
        _ = time.time()
        clk.start()

        # let it tick for a short while
        await asyncio.sleep(0.22)
        await clk.stop()

        # should have produced several ticks
        assert count >= 3, f"expected >=3 ticks, got {count}"

        # after stop, ensure no more ticks occur
        after_stop = count
        await asyncio.sleep(0.15)
        assert count == after_stop, "clock should not tick after stop()"

    asyncio.run(_run())
