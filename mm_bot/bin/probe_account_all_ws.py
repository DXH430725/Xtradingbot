import asyncio
import json
import logging
import os
import sys
import time
from typing import Any, Dict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mm_bot.connector.lighter.lighter_exchange import LighterConnector


async def ws_connect(conn: LighterConnector):
    import websockets
    await conn._ensure_signer()
    host = conn.config.base_url.replace("https://", "")
    uri = f"wss://{host}/stream"
    ws = await websockets.connect(uri)
    try:
        await asyncio.wait_for(ws.recv(), timeout=2.0)
    except Exception:
        pass
    return ws


async def subscribe(ws, channel: str, auth: str):
    payload = {"type": "subscribe", "channel": channel, "auth": auth}
    await ws.send(json.dumps(payload))


def summarize(label: str, data: Dict[str, Any]):
    if label == "orders":
        orders = data.get("orders")
        if isinstance(orders, dict):
            totals = {k: (len(v) if isinstance(v, list) else 0) for k, v in orders.items()}
            sample = None
            for v in orders.values():
                if isinstance(v, list) and v:
                    sample = v[0]
                    break
            if isinstance(sample, dict):
                fields = ["order_index","client_order_index","status","market_index","price","remaining_base_amount"]
                print(f"orders {totals} sample={{" + ", ".join(f"{f}={sample.get(f)}" for f in fields) + "}}")
            else:
                print(f"orders {totals}")
    elif label == "trades":
        trades = data.get("trades")
        if isinstance(trades, dict):
            totals = {k: (len(v) if isinstance(v, list) else 0) for k, v in trades.items()}
            sample = None
            for v in trades.values():
                if isinstance(v, list) and v:
                    sample = v[0]
                    break
            if isinstance(sample, dict):
                fields = ["trade_id","tx_hash","market_id","size","price","timestamp"]
                print(f"trades {totals} sample={{" + ", ".join(f"{f}={sample.get(f)}" for f in fields) + "}}")
            else:
                print(f"trades {totals}")
    elif label == "positions":
        positions = data.get("positions")
        if isinstance(positions, dict):
            keys = list(positions.keys())
            one = positions.get(keys[0]) if keys else None
            if isinstance(one, dict):
                fields = ["market_id","symbol","position","avg_entry_price","unrealized_pnl","allocated_margin"]
                print(f"positions markets={keys} sample={{" + ", ".join(f"{f}={one.get(f)}" for f in fields) + "}}")
            else:
                print(f"positions markets={keys}")


async def main():
    logging.basicConfig(level=logging.INFO)
    for n in ["websockets.client", "websockets.protocol", "urllib3", "asyncio"]:
        logging.getLogger(n).setLevel(logging.WARNING)

    conn = LighterConnector()
    conn.start()
    await conn._ensure_markets()
    symbol = next((s for s in conn._symbol_to_market.keys() if s.upper().startswith("BTC")), None)
    if not symbol:
        raise RuntimeError("No BTC market")
    market_id = await conn.get_market_id(symbol)
    idx = await conn._ensure_account_index()
    await conn._ensure_signer()
    auth, _ = conn.signer.create_auth_token_with_expiry()

    ws = await ws_connect(conn)
    # subscribe channels
    await subscribe(ws, f"account_all_orders/{idx}", auth)
    await subscribe(ws, f"account_all_trades/{idx}", auth)
    await subscribe(ws, f"account_all_positions/{idx}", auth)
    print(f"Subscribed account_all_* for account={idx}, market_id={market_id}")

    # Place actions to generate updates
    ob = await conn.order_api.order_book_orders(market_id, 1)
    ask = int(str(ob.asks[0].price).replace(".", "")) if ob and ob.asks else None
    if ask is None:
        raise RuntimeError("empty order book")
    # compute min size
    ob_list = await conn.order_api.order_books()
    entry = next((e for e in ob_list.order_books if int(e.market_id) == market_id), None)
    size_dec = int(entry.supported_size_decimals)
    min_size_i = max(1, int(float(entry.min_base_amount) * (10 ** size_dec)))

    cio = int(time.time()) % 1_000_000
    # limit PO sell
    lim_price = ask + max(ask // 1000, 1)
    tx, tx_hash, err = await conn.place_limit(symbol, cio + 1, min_size_i, price=lim_price, is_ask=True, post_only=True)
    print("Placed limit PO:", tx_hash, err)

    # consumer for a short window
    start = time.time()
    while time.time() - start < 8.0:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=1.5)
        except Exception:
            continue
        if isinstance(raw, (bytes, bytearray)):
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        t = data.get("type")
        if not isinstance(t, str):
            continue
        if t.startswith("update/"):
            if "orders" in data:
                summarize("orders", data)
            if "trades" in data:
                summarize("trades", data)
            if "positions" in data:
                summarize("positions", data)
        # after first update of orders open, try cancel
        orders = data.get("orders") if isinstance(data, dict) else None
        if isinstance(orders, dict):
            arr = orders.get(str(market_id)) or orders.get(market_id)
            if isinstance(arr, list):
                tgt = next((o for o in arr if int(o.get("client_order_index", -1)) == (cio + 1)), None)
                if tgt and tgt.get("status") == "open":
                    oi = int(tgt.get("order_index"))
                    _txi, cancel_hash, cerr = await conn.cancel_order(oi, market_index=market_id)
                    print("Cancelled via WS view:", cancel_hash, cerr)

    # market buy then reduce-only sell
    tx, tx_hash, err = await conn.place_market(symbol, cio + 2, min_size_i, is_ask=False, reduce_only=0)
    print("Market buy:", tx_hash, err)
    await asyncio.sleep(1.0)
    tx, tx_hash, err = await conn.place_market(symbol, cio + 3, min_size_i, is_ask=True, reduce_only=1)
    print("Market sell RO:", tx_hash, err)

    # drain a bit more
    end = time.time() + 4.0
    while time.time() < end:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
        except Exception:
            continue
        if isinstance(raw, (bytes, bytearray)):
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        t = data.get("type")
        if isinstance(t, str) and t.startswith("update/"):
            if "orders" in data:
                summarize("orders", data)
            if "trades" in data:
                summarize("trades", data)
            if "positions" in data:
                summarize("positions", data)

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

