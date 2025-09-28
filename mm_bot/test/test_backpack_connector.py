import asyncio
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mm_bot.connector.backpack import BackpackConnector, BackpackConfig


async def main():
    symbol = os.getenv("XTB_SYMBOL", "BTC_USDC_PERP")
    cfg = BackpackConfig()
    conn = BackpackConnector(cfg)
    conn.start()

    # start ws (public depth + private updates if available)
    # simple event prints
    conn.set_event_handlers(
        on_order_filled=lambda info: print(f"on_order_filled: {info}"),
        on_order_cancelled=lambda info: print(f"on_order_cancelled: {info}"),
        on_position_update=lambda info: print(f"on_position_update: {info}"),
    )
    await conn.start_ws_state([symbol])

    # basics
    syms = await conn.list_symbols()
    print(f"symbols_count={len(syms)} has_{symbol}={symbol in syms}")

    p_dec, s_dec = await conn.get_price_size_decimals(symbol)
    print(f"decimals: price={p_dec} size={s_dec}")

    # account
    acct = await conn.get_account_overview()
    print(f"account_keys={list(acct.keys())[:8]}")

    # top of book
    bid, ask, scale = await conn.get_top_of_book(symbol)
    print(f"tob: bid_i={bid} ask_i={ask} scale={scale}")

    # compute min order size from markets info if possible
    min_size_i = 1 * (10 ** s_dec)
    try:
        # private field via market info accessor
        await conn._ensure_markets()
        mi = conn._market_info.get(symbol) or {}
        msz = mi.get("minOrderSize")
        if msz is not None:
            min_size_i = max(min_size_i, int(round(float(msz) * (10 ** s_dec))))
    except Exception:
        pass
    print(f"min_size_i={min_size_i}")

    # place minimal market buy then market sell (close)
    cio = int(time.time() * 1000) % 1_000_000
    ret, _r, err = await conn.place_market(symbol, cio, base_amount=min_size_i, is_ask=False, reduce_only=0)
    print(f"market_buy: err={err} ret={ret}")
    await asyncio.sleep(1.0)

    # try market close (buy then market sell reduce-only)
    cio2 = (cio + 1) % 1_000_000
    ret2, _r2, err2 = await conn.place_market(symbol, cio2, base_amount=min_size_i, is_ask=True, reduce_only=0)
    print(f"market_close: err={err2} ret={ret2}")

    # cancel leftover
    opens2 = await conn.get_open_orders(symbol)
    for od in opens2:
        oid = str(od.get("id") or od.get("orderId"))
        sym = od.get("symbol") or symbol
        _x, _y, e = await conn.cancel_order(oid, sym)
        print(f"cancel {oid}: err={e}")

    await conn.stop_ws_state()
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
