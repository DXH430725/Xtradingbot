import asyncio
import logging
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mm_bot.logger.logger import setup_logging
from mm_bot.connector.grvt.grvt_exchange import GrvtConnector, GrvtConfig


async def main():
    # setup logging
    log_cfg = os.path.join(ROOT, "mm_bot", "conf", "logging.yaml")
    setup_logging(log_cfg)
    log = logging.getLogger("mm_bot.bin.test_grvt_connector")

    # init connector
    keys_file = os.getenv("XTB_GRVT_KEYS_FILE", os.path.abspath(os.path.join(os.getcwd(), "Grvt_key.txt")))
    conn = GrvtConnector(config=GrvtConfig(), debug=True)
    conn.start()

    # list symbols and pick BTC perp
    syms = await conn.list_symbols()
    if not syms:
        log.error("No symbols returned by GRVT")
        return
    symbol = next((s for s in syms if s.upper().startswith("BTC")), syms[0])
    log.info(f"Using symbol: {symbol}")

    # market info
    mid = await conn.get_market_id(symbol)
    p_dec, s_dec = await conn.get_price_size_decimals(symbol)
    min_size_i = await conn.get_min_order_size_i(symbol)
    log.info(f"market_id={mid} price_decimals={p_dec} size_decimals={s_dec} min_size_i={min_size_i}")

    # top of book
    bid_i, ask_i, scale = await conn.get_top_of_book(symbol)
    log.info(f"TOB: bid_i={bid_i} ask_i={ask_i} scale={scale}")
    if bid_i is None or ask_i is None:
        log.error("Empty order book; aborting order tests")
        return

    # positions and open orders
    pos = await conn.get_positions()
    log.info(f"positions: {pos}")
    oo0 = await conn.get_open_orders(symbol)
    log.info(f"open_orders(before): {oo0}")

    # place two post-only quotes
    cio_bid = int(asyncio.get_event_loop().time() * 1000) % 1_000_000
    cio_ask = (cio_bid + 1) % 1_000_000
    # be conservative: quote one tick inside
    price_bid_i = max(1, bid_i - 1)
    price_ask_i = max(price_bid_i + 1, ask_i + 1)
    _tx, ret_b, err_b = await conn.place_limit(symbol, cio_bid, base_amount=min_size_i, price=price_bid_i, is_ask=False, post_only=True)
    _tx, ret_a, err_a = await conn.place_limit(symbol, cio_ask, base_amount=min_size_i, price=price_ask_i, is_ask=True, post_only=True)
    log.info(f"placed bid ret={ret_b} err={err_b}; ask ret={ret_a} err={err_a}")
    await asyncio.sleep(1.0)

    oo1 = await conn.get_open_orders(symbol)
    log.info(f"open_orders(after place): {oo1}")

    # cancel one order by order_index if available, else by client_order_index fallback through cancel_all
    if oo1:
        oi = int(oo1[0].get("order_index", 0) or 0)
        log.info(f"cancel one order oi={oi}")
        try:
            await conn.cancel_order(oi)
        except Exception as e:
            log.warning(f"cancel_order failed: {e}")
    await asyncio.sleep(1.0)

    oo2 = await conn.get_open_orders(symbol)
    log.info(f"open_orders(after cancel one): {oo2}")

    # cancel all
    log.info("cancel_all…")
    try:
        await conn.cancel_all()
    except Exception as e:
        log.warning(f"cancel_all failed: {e}")
    await asyncio.sleep(1.0)
    oo3 = await conn.get_open_orders(symbol)
    log.info(f"open_orders(after cancel_all): {oo3}")

    # account overview
    acc = await conn.get_account_overview()
    log.info(f"account_overview: {acc}")


if __name__ == "__main__":
    asyncio.run(main())

