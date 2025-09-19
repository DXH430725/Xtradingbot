import asyncio
import os
import sys
from typing import Any, Dict, List

# ensure project root on sys.path for absolute import when executing as a script
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mm_bot.connector.lighter.lighter_exchange import LighterConnector, LighterConfig


def _is_true_like(v: Any) -> bool:
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "on"): return True
        if s in ("false", "0", "no", "off"): return False
        try:
            return bool(int(s))
        except Exception:
            return False
    try:
        return bool(v)
    except Exception:
        return False


def _is_reduce_only(o: Dict[str, Any]) -> bool:
    for k in ("reduce_only", "reduceOnly", "reduce_only_flag", "reduceOnlyFlag"):
        if k in o and o.get(k) is not None:
            return _is_true_like(o.get(k))
    return False


def _is_ask(o: Dict[str, Any]) -> bool:
    v = o.get("is_ask")
    if v is None:
        v = o.get("isAsk")
    if v is not None:
        return _is_true_like(v)
    side = str(o.get("side", "")).strip().lower()
    if side in ("sell", "ask"):
        return True
    if side in ("buy", "bid"):
        return False
    return False


async def main():
    symbol = os.getenv("XTB_SYMBOL")
    cfg = LighterConfig()
    conn = LighterConnector(cfg)
    conn.start()
    await conn.start_ws_state()
    await conn._ensure_markets()
    if not symbol:
        # best-effort default to first BTC-like
        syms = await conn.list_symbols()
        symbol = next((s for s in syms if s.upper().startswith("BTC")), syms[0] if syms else None)
    if not symbol:
        print("No symbol available")
        return
    # pull once via WS cache (populated by state WS loop)
    await asyncio.sleep(0.8)
    opens: List[Dict[str, Any]] = await conn.get_open_orders(symbol)
    total = len(opens)
    red = [o for o in opens if _is_reduce_only(o)]
    red_total = len(red)
    red_asks = sum(1 for o in red if _is_ask(o))
    red_bids = red_total - red_asks
    cois = [int(o.get("client_order_index", 0) or 0) for o in red if o.get("client_order_index") is not None]
    # status histogram for WS opens
    def _status(o: Dict[str, Any]) -> str:
        s = str(o.get("status", "")).strip().lower()
        return s or "<empty>"
    from collections import Counter
    ws_status_counts = Counter(_status(o) for o in opens)
    ws_red_status_counts = Counter(_status(o) for o in red)
    # also fetch REST active orders for cross-check
    try:
        idx = await conn._ensure_account_index()
        mid = await conn.get_market_id(symbol)
        await conn._throttler.acquire("/api/v1/accountActiveOrders")
        rest = await conn.order_api.account_active_orders(int(idx), int(mid))
        rest_orders = list(getattr(rest, "orders", []) or [])
        rest_dicts: List[Dict[str, Any]] = [od.to_dict() if hasattr(od, "to_dict") else dict(od) for od in rest_orders]
    except Exception as e:
        rest_dicts = []

    rest_total = len(rest_dicts)
    rest_red = [o for o in rest_dicts if _is_reduce_only(o)]
    rest_red_total = len(rest_red)

    print(f"symbol={symbol}")
    print(f"WS_cache: open_orders_total={total}, reduce_only_total={red_total} (asks={red_asks}, bids={red_bids})")
    print(f"WS_cache: reduce_only_cois={sorted([c for c in cois if c])}")
    print(f"WS_cache: status_counts={dict(ws_status_counts)}")
    print(f"WS_cache: reduce_only_status_counts={dict(ws_red_status_counts)}")
    print(f"REST:     open_orders_total={rest_total}, reduce_only_total={rest_red_total}")

    try:
        ws_ois = sorted(int(o.get("order_index")) for o in opens if o.get("order_index") is not None)
        rest_ois = sorted(int(o.get("order_index")) for o in rest_dicts if o.get("order_index") is not None)
        extra = sorted(set(ws_ois) - set(rest_ois))
        missing = sorted(set(rest_ois) - set(ws_ois))
        print(f"DIFF: ws_only_oi_count={len(extra)} rest_only_oi_count={len(missing)}")
        if extra:
            print(f"DIFF: ws_only_oi_sample={extra[:10]}")
    except Exception:
        pass

    # cleanup
    try:
        await conn.stop_ws_state()
    except Exception:
        pass
    finally:
        try:
            await conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
