import asyncio
import os
import sys
from typing import Any, Dict, List, Tuple

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


def _coi(o: Dict[str, Any]) -> int:
    try:
        v = o.get("client_order_index")
        return int(v) if v is not None else 0
    except Exception:
        try:
            return int(str(o.get("client_order_index")))
        except Exception:
            return 0


async def main():
    symbol = os.getenv("XTB_SYMBOL")
    cfg = LighterConfig()
    conn = LighterConnector(cfg)
    conn.start()
    try:
        await conn.start_ws_state()
        await conn._ensure_markets()

        if not symbol:
            syms = await conn.list_symbols()
            symbol = next((s for s in syms if s.upper().startswith("BTC")), syms[0] if syms else None)
        if not symbol:
            print("No symbol available")
            return

        # wait a bit to populate cache
        await asyncio.sleep(2.0)
        opens: List[Dict[str, Any]] = await conn.get_open_orders(symbol)
        total = len(opens)

        # directional counts: like strategy's _sum_open_coverage_i for a long net -> need to SELL (asks)
        # determine net position sign best-effort
        q_base = 0.0
        try:
            mid = await conn.get_market_id(symbol)
            pos = conn._positions_by_market.get(int(mid)) or {}
            qv = pos.get("position")
            q_base = float(qv) if qv is not None else 0.0
        except Exception:
            q_base = 0.0
        need_sell = q_base > 0

        asks = [o for o in opens if _is_ask(o)]
        bids = [o for o in opens if not _is_ask(o)]
        red = [o for o in opens if _is_reduce_only(o)]
        red_asks = [o for o in red if _is_ask(o)]
        red_bids = [o for o in red if not _is_ask(o)]

        # mimic matched_reduce_orders by side only
        matched_by_side = len(asks) if need_sell else len(bids)
        matched_by_ro = len(red_asks) if need_sell else len(red_bids)

        print(f"symbol={symbol} net_pos_base={q_base:.8f} need_sell={need_sell}")
        print(f"WS_cache: opens_total={total} asks={len(asks)} bids={len(bids)}")
        print(f"WS_cache: reduce_only_total={len(red)} ro_asks={len(red_asks)} ro_bids={len(red_bids)}")
        print(f"COMPARE: matched_by_side={matched_by_side} matched_by_reduce_only={matched_by_ro}")

        # list mismatching COIs (side-matched but not RO)
        side_set = set(_coi(o) for o in (asks if need_sell else bids))
        ro_set = set(_coi(o) for o in (red_asks if need_sell else red_bids))
        diff = sorted(c for c in side_set - ro_set if c)
        if diff:
            sample = diff[:25]
            print(f"SIDE_ONLY_COIS (not reduce-only) count={len(diff)} sample={sample}")
        else:
            print("No side-only COIs; side and RO sets align")
    finally:
        try:
            await conn.stop_ws_state()
        except Exception:
            pass
        try:
            await conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())

