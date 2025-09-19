import asyncio
import logging
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mm_bot.logger.logger import setup_logging
from mm_bot.core.trading_core import TradingCore
from mm_bot.connector.lighter.lighter_exchange import LighterConnector
from mm_bot.strategy.market_making import MarketMakingMid100Strategy


async def run(ticks: int = 4, tick_size: float = 10.0) -> int:
    # logging
    cfg_path = os.path.join(ROOT, "mm_bot", "conf", "logging.yaml")
    setup_logging(cfg_path if os.path.exists(cfg_path) else None)
    log = logging.getLogger("mm_bot.bin.run_mm_perf_test")

    # core + connector + strategy
    core = TradingCore(tick_size=tick_size, debug=os.getenv("XTB_DEBUG") in {"1","true","yes","on"})
    conn = LighterConnector(debug=False)
    conn.start()
    core.add_connector("lighter", conn)
    offset_abs = float(os.getenv("XTB_OFFSET_ABS", "100.0"))
    strat = MarketMakingMid100Strategy(connector=conn, symbol=None, price_offset_abs=offset_abs)
    core.set_strategy(strat)

    # start core
    await core.start()

    # run for N ticks
    await asyncio.sleep(tick_size * ticks + 1.0)

    # snapshot before stopping
    snap = await strat.snapshot()
    log.info(f"Snapshot: {snap}")

    # validations
    ok = True
    reasons = []
    if snap["filled"] != 0:
        ok = False
        reasons.append(f"filled != 0 ({snap['filled']})")
    # expect two open orders at end
    if snap["open_orders"] != 2:
        ok = False
        reasons.append(f"open_orders != 2 ({snap['open_orders']})")
    # cancelled events should roughly match per-tick cancellations (2 per tick after first)
    min_expected = max(0, (snap["ticks"] - 1))  # tolerate WS aggregation by counting events not exactly 2x
    max_expected = 2 * max(0, (snap["ticks"] - 1)) + 2
    if not (min_expected <= snap["cancelled"] <= max_expected):
        ok = False
        reasons.append(f"cancelled out of expected range [{min_expected},{max_expected}] -> {snap['cancelled']}")
    # cache consistency: last CIOs should be present among open orders
    last_cios = [c for c in snap["last_cios"] if c is not None]
    if last_cios:
        opens = set(int(x) for x in snap["open_client_order_indices"] if x is not None)
        if not set(last_cios).issubset(opens):
            ok = False
            reasons.append("last CIOs not found in open orders")

    # cleanup: try to cancel individually to reduce signer errors in tests
    try:
        opens = await conn.get_open_orders()
        for o in opens:
            try:
                oi = int(o.get("order_index"))
            except Exception:
                continue
            try:
                await conn.cancel_order(oi)
            except Exception:
                pass
        await asyncio.sleep(1.0)
    except Exception:
        pass

    await core.stop(cancel_orders=False)
    await core.shutdown()

    if ok:
        print("PASS: mid±100 perf test (3–5 ticks) OK")
        return 0
    else:
        print("FAIL:", "; ".join(reasons))
        return 1


if __name__ == "__main__":
    t = int(os.getenv("XTB_TICKS", "4"))
    ts = float(os.getenv("XTB_TICK_SIZE", "10.0"))
    raise SystemExit(asyncio.run(run(ticks=t, tick_size=ts)))
