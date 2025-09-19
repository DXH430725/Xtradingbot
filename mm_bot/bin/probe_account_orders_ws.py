import asyncio
import json
import logging
import os
import sys
import time
from typing import Any, Dict, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mm_bot.connector.lighter.lighter_exchange import LighterConnector


async def subscribe_account_all_orders(conn: LighterConnector, account_index: int):
    import websockets

    await conn._ensure_signer()
    auth, _ = conn.signer.create_auth_token_with_expiry()
    host = conn.config.base_url.replace("https://", "")
    uri = f"wss://{host}/stream"

    ws = await websockets.connect(uri)
    # wait for connected message
    try:
        msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
    except Exception:
        msg = None
    # subscribe
    payload = {
        "type": "subscribe",
        "channel": f"account_all_orders/{account_index}",
        "auth": auth,
    }
    await ws.send(json.dumps(payload))
    return ws


def summarize_orders_payload(data: Dict[str, Any]) -> Optional[str]:
    orders = data.get("orders")
    if not isinstance(orders, dict):
        return None
    keys = list(orders.keys())
    total = 0
    one = None
    for k, v in orders.items():
        if isinstance(v, list):
            total += len(v)
            if not one and v:
                one = v[0]
    head = f"markets={keys} total_orders={total}"
    if isinstance(one, dict):
        fields = ["order_index", "client_order_index", "status", "market_index", "price", "remaining_base_amount"]
        sample = {f: one.get(f) for f in fields}
        return f"{head} sample={sample}"
    return head


async def main():
    logging.basicConfig(level=logging.INFO)
    # Reduce noise
    for n in ["websockets.client", "websockets.protocol", "urllib3", "asyncio"]:
        logging.getLogger(n).setLevel(logging.WARNING)

    conn = LighterConnector()
    conn.start()

    # resolve BTC market and scales
    await conn._ensure_markets()
    btc_symbol = next((s for s in conn._symbol_to_market.keys() if s.upper().startswith("BTC")), None)
    if not btc_symbol:
        raise RuntimeError("No BTC market found")
    market_id = await conn.get_market_id(btc_symbol)

    idx = await conn._ensure_account_index()
    ws = await subscribe_account_all_orders(conn, idx)

    print(f"Subscribed account_all_orders for account={idx}, market_id={market_id}")

    # consumer task: read and print summaries
    async def consumer():
        try:
            while True:
                raw = await ws.recv()
                if isinstance(raw, (bytes, bytearray)):
                    continue
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                t = data.get("type")
                if t:
                    print(f"WS type={t}")
                summary = summarize_orders_payload(data)
                if summary:
                    print(f"WS orders: {summary}")
        except Exception:
            pass

    cons_task = asyncio.create_task(consumer())

    # small helper to get top ask/bid
    async def top(side: str) -> int:
        ob = await conn.order_api.order_book_orders(market_id, 1)
        p = (ob.asks[0].price if side == "ask" else ob.bids[0].price)
        return int(str(p).replace(".", ""))

    # Place a limit post-only order, wait, then cancel
    cio = int(time.time()) % 1_000_000
    min_size_i = 20  # falls back; the test script elsewhere computes from order_books
    try:
        ob_list = await conn.order_api.order_books()
        entry = next((e for e in ob_list.order_books if int(e.market_id) == market_id), None)
        if entry:
            size_dec = int(entry.supported_size_decimals)
            min_size_i = max(1, int(float(entry.min_base_amount) * (10 ** size_dec)))
    except Exception:
        pass

    ask_int = await top("ask")
    lim_price = ask_int + max(ask_int // 1000, 1)  # ~0.1% above
    tx, tx_hash, err = await conn.place_limit(btc_symbol, cio + 1, min_size_i, price=lim_price, is_ask=True, post_only=True)
    print("Placed limit PO sell:", tx_hash, err)
    await asyncio.sleep(1.5)

    # cancel that specific order (via active orders refresh)
    open_orders = await conn.get_open_orders(btc_symbol)
    tgt = next((o for o in open_orders if int(o.get("client_order_index", -1)) == (cio + 1)), None)
    if tgt is not None:
        oi = int(tgt.get("order_index"))
        _txi, cancel_hash, cerr = await conn.cancel_order(oi, market_index=market_id)
        print("Cancelled target limit:", cancel_hash, cerr)
    else:
        _txi, cancel_hash, cerr = await conn.cancel_all()
        print("Cancelled all:", cancel_hash, cerr)

    # Market buy to open long, then reduce-only sell to close
    tx, tx_hash, err = await conn.place_market(btc_symbol, cio + 2, min_size_i, is_ask=False, reduce_only=0)
    print("Market buy open:", tx_hash, err)
    await asyncio.sleep(1.0)
    tx, tx_hash, err = await conn.place_market(btc_symbol, cio + 3, min_size_i, is_ask=True, reduce_only=1)
    print("Market sell close (RO):", tx_hash, err)

    # Allow some time to receive WS updates
    await asyncio.sleep(2.0)

    cons_task.cancel()
    try:
        await cons_task
    except asyncio.CancelledError:
        pass
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

