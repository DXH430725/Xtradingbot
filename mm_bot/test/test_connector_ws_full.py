import asyncio
import logging
import os
import sys
import time
from typing import List, Dict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mm_bot.connector.lighter import LighterConnector


async def main():
    logging.basicConfig(level=logging.INFO)
    for n in ["websockets.client", "websockets.protocol", "urllib3", "asyncio"]:
        logging.getLogger(n).setLevel(logging.WARNING)

    conn = LighterConnector(debug=False)
    conn.start()

    # start full WS state maintenance
    await conn.start_ws_state()

    # register events
    events: Dict[str, List[Dict]] = {"filled": [], "cancelled": [], "trades": [], "positions": []}
    conn.set_event_handlers(
        on_order_filled=lambda info: events["filled"].append(dict(info)),
        on_order_cancelled=lambda info: events["cancelled"].append(dict(info)),
        on_trade=lambda t: events["trades"].append(dict(t)),
        on_position_update=lambda p: events["positions"].append(dict(p)),
    )

    # resolve BTC market
    await conn._ensure_markets()
    symbol = next((s for s in conn._symbol_to_market.keys() if s.upper().startswith("BTC")), None)
    if not symbol:
        raise RuntimeError("No BTC market")
    market_id = await conn.get_market_id(symbol)

    # compute min size
    ob_list = await conn.order_api.order_books()
    entry = next((e for e in ob_list.order_books if int(e.market_id) == market_id), None)
    size_dec = int(entry.supported_size_decimals)
    min_size_i = max(1, int(float(entry.min_base_amount) * (10 ** size_dec)))

    # place a limit post-only order and wait to see it in WS orders
    cio = int(time.time()) % 1_000_000
    ob = await conn.order_api.order_book_orders(market_id, 1)
    ask = int(str(ob.asks[0].price).replace(".", ""))
    lim_price = ask + max(ask // 1000, 1)
    tx, tx_hash, err = await conn.place_limit(symbol, cio + 1, min_size_i, price=lim_price, is_ask=True, post_only=True)
    print("Limit PO:", tx_hash, err)

    # wait for WS cache to include the order
    found_oi = None
    for _ in range(40):
        await asyncio.sleep(0.1)
        opens = await conn.get_open_orders(symbol)
        tgt = next((o for o in opens if int(o.get("client_order_index", -1)) == (cio + 1)), None)
        if tgt is not None:
            found_oi = int(tgt.get("order_index"))
            break
    print("Open found order_index:", found_oi)

    # cancel it via cached order_index
    if found_oi is not None:
        _txi, cancel_hash, cerr = await conn.cancel_order(found_oi, market_index=market_id)
        print("Cancel order:", cancel_hash, cerr)

    # wait to observe canceled event
    for _ in range(40):
        await asyncio.sleep(0.1)
        if any(int(e.get("client_order_index", -1)) == (cio + 1) for e in events["cancelled"]):
            break

    # place market buy then reduce-only sell
    tx, tx_hash, err = await conn.place_market(symbol, cio + 2, min_size_i, is_ask=False, reduce_only=0)
    print("Market buy:", tx_hash, err)
    await asyncio.sleep(1.0)
    tx, tx_hash, err = await conn.place_market(symbol, cio + 3, min_size_i, is_ask=True, reduce_only=1)
    print("Market sell RO:", tx_hash, err)

    # wait a bit for trades and positions updates
    await asyncio.sleep(2.0)

    # snapshot cache
    opens = await conn.get_open_orders(symbol)
    poss = await conn.get_positions()
    print("Cache open orders:", len(opens))
    print("Cache positions markets:", [p.get("market_id") or p.get("market_index") for p in poss])
    print("Recent trades count:", sum(len(v) for v in conn._trades_by_market.values()))
    print("Events filled/cancelled/trades/positions:", [len(events[k]) for k in ("filled","cancelled","trades","positions")])

    # shutdown
    await conn.stop_ws_state()
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
