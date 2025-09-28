import asyncio
import logging
import os
import sys
import time
import platform
import os.path as _path
import json

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mm_bot.connector.lighter import LighterConnector, LighterConfig


async def main():
    # Keep logs readable: downgrade noisy libraries
    logging.basicConfig(level=logging.INFO)
    for name in [
        "websockets.client",
        "websockets.protocol",
        "urllib3",
        "asyncio",
    ]:
        logging.getLogger(name).setLevel(logging.WARNING)
    cfg = LighterConfig()
    conn = LighterConnector(cfg, debug=True)
    conn.start()

    # Discover BTC market id
    # Common symbols might be BTC-USDC, BTC-USD, BTC-PERP depending on Lighter; try fuzzy pick for BTC
    await conn._ensure_markets()
    btc_symbol = None
    for s in conn._symbol_to_market.keys():
        if s.upper().startswith("BTC"):
            btc_symbol = s
            break
    if not btc_symbol:
        raise RuntimeError("No BTC market found in orderBooks")

    print(f"Using BTC symbol: {btc_symbol} (market_id={await conn.get_market_id(btc_symbol)})")

    # WS: Order book subscription for BTC
    print("Starting WS order book...")
    await conn.start_ws_order_book([btc_symbol])
    await asyncio.sleep(2)
    await conn.stop_ws()
    print("WS order book OK")

    # WS: Account positions and balances
    print("Starting WS account...")
    # capture account WS messages to inspect trades payload
    last_ws = {"msg": None}
    def on_acc(aid, body):
        last_ws["msg"] = body
        trades = body.get("trades") if isinstance(body, dict) else None
        if isinstance(trades, list) and trades:
            has_tx = any(isinstance(t, dict) and ("tx_hash" in t) for t in trades)
            if has_tx:
                print("WS trades present; contains tx_hash fields.")
    await conn.start_ws_account(on_update=on_acc)
    await asyncio.sleep(2)
    await conn.stop_ws()
    print("WS account OK")

    # REST: account overview fetch
    overview = await conn.get_account_overview()
    print("Account overview keys:", list(overview.keys()))

    # Latency test
    latency = await conn.best_effort_latency_ms()
    print(f"Root status latency: {latency:.1f} ms")

    # Orders: determine min size from orderBooks entry (Windows requires signer DLL)
    if platform.system() == "Windows":
        signer_dll = _path.join(_path.dirname(__file__), "..", "..", "..", "lighter-python", "lighter", "signers", "signer-amd64.dll")
        if not _path.exists(_path.abspath(signer_dll)):
            print("Skipping order placement on Windows (signer DLL not found). See build/README_SIGNER.md.")
            await conn.close()
            return
        else:
            print("Windows signer DLL detected; attempting order tests...")
        await conn.close()
        return

    await conn._ensure_markets()
    market_id = await conn.get_market_id(btc_symbol)
    ob_list = await conn.order_api.order_books()
    entry = next((e for e in ob_list.order_books if int(e.market_id) == market_id), None)
    if not entry:
        raise RuntimeError("Failed to find BTC order book entry")
    # min_base_amount is a string decimal; Lighter uses integers scaled by supported_size_decimals
    size_decimals = int(entry.supported_size_decimals)
    min_base_amount = max(1, int(float(entry.min_base_amount) * (10 ** size_decimals)))
    print(f"Min base amount (scaled int): {min_base_amount}")

    # Place minimal unit orders (use small CIO indices)
    cio = int(time.time()) % 1000000
    # subscribe simple order events
    filled_events = []
    cancelled_events = []
    conn._on_order_filled = lambda info: filled_events.append(dict(info))
    conn._on_order_cancelled = lambda info: cancelled_events.append(dict(info))

    # Market buy (reduce_only=0)
    tx, tx_hash, err = await conn.place_market(btc_symbol, cio + 1, min_base_amount, is_ask=False, reduce_only=0)
    print("Market buy:", tx_hash, err)
    # Wait briefly for WS to report fill via trades/positions
    for _ in range(40):
        await asyncio.sleep(0.05)
        if filled_events:
            break
    if filled_events:
        e = filled_events[-1]
        print("EVENT filled via WS:", {k: e.get(k) for k in ("client_order_index","market_index","symbol","tx_hash","status")})
    # Limit sell post-only: place slightly above best ask to ensure maker and avoid accidental price filter
    ob_top = await conn.order_api.order_book_orders(market_id, 1)
    top_ask = ob_top.asks[0].price if ob_top and ob_top.asks else None
    if top_ask is None:
        raise RuntimeError("Order book empty; cannot place post-only order")
    ask_int = int(str(top_ask).replace(".", ""))
    # add ~0.1% or at least 1 tick
    add = max(ask_int // 1000, 1)
    lim_price = ask_int + add
    tx, tx_hash, err = await conn.place_limit(
        btc_symbol,
        cio + 2,
        min_base_amount,
        price=lim_price,
        is_ask=True,
        post_only=True,
    )
    print("Limit post-only sell:", tx_hash, err)
    # Force quick stale reap to exercise order_index refresh + cancel logic
    await conn.reap_stale_orders(timeout_sec=0.5)

    # Market close (reduce-only=1)
    tx, tx_hash, err = await conn.place_market(btc_symbol, cio + 3, min_base_amount, is_ask=True, reduce_only=1)
    print("Market close reduce-only:", tx_hash, err)

    # Query open orders (WS-cache-first, REST fallback) and try cancel the one we just placed (if present)
    open_orders = await conn.get_open_orders(btc_symbol)
    print("Open orders count:", len(open_orders))
    # Try match by client_order_index
    target = None
    for o in open_orders:
        if int(o.get('client_order_index', -1)) == (cio + 2):
            target = o
            break
    if target is not None:
        order_index = int(target.get('order_index'))
        tx_info, cancel_hash, cancel_err = await conn.cancel_order(order_index, market_index=market_id)
        print("Cancel target order:", cancel_hash, cancel_err)
    else:
        # fallback: cancel all to keep account clean
        tx_info, cancel_hash, cancel_err = await conn.cancel_all()
        print("Cancel all:", cancel_hash, cancel_err)

    if cancelled_events:
        e = cancelled_events[-1]
        print("EVENT cancelled via REST/WS:", {k: e.get(k) for k in ("client_order_index","market_index","symbol","order_index","status")})

    # Probe raw WS for orders channels (account_orders requires auth token)
    try:
        import websockets
        base_host = conn.config.base_url.replace("https://", "")
        uri = f"wss://{base_host}/stream"
        idx = await conn._ensure_account_index()
        await conn._ensure_signer()
        auth_token, _ = conn.signer.create_auth_token_with_expiry()
        async def probe_channel(chan: str, with_market: bool = False):
            try:
                async with websockets.connect(uri) as ws:
                    await ws.recv()  # connected
                    channel = f"{chan}/{market_id}/{idx}" if with_market else f"{chan}/{idx}"
                    payload = {"type":"subscribe", "channel": channel}
                    # account_orders requires auth in message
                    if chan.startswith("account_orders"):
                        payload["auth"] = auth_token
                    await ws.send(json.dumps(payload))
                    # read a few messages
                    for _ in range(10):
                        msg = await asyncio.wait_for(ws.recv(), timeout=1.5)
                        if isinstance(msg, (bytes, bytearray)):
                            continue
                        data = json.loads(msg)
                        mtype = data.get("type")
                        if mtype and mtype.startswith("subscribed/"):
                            print(f"WS subscribed: {chan}")
                        orders = data.get("orders")
                        # account_orders returns dict keyed by market_index
                        if isinstance(orders, dict):
                            for k,v in orders.items():
                                if isinstance(v, list) and v:
                                    print(f"WS {chan} {k} orders len={len(v)} keys={list(v[0].keys())[:6]}")
                                    return True
                        if isinstance(orders, list) and orders:
                            print(f"WS {chan} orders len={len(orders)} keys={list(orders[0].keys())[:6]}")
                            return True
            except Exception:
                return False
            return False
        found = await probe_channel("account_orders", with_market=True)
        print("WS account_orders working:", found)
    except Exception as e:
        print("WS orders probe skipped:", e)

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
