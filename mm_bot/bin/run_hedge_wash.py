import asyncio
import os
import random
import signal
import time
from typing import Dict, List, Optional, Tuple

from mm_bot.connector.lighter.lighter_exchange import LighterConnector, LighterConfig
from mm_bot.connector.backpack.backpack_exchange import BackpackConnector, BackpackConfig


def _norm_sym_bp(sym: str) -> Tuple[str, str, bool]:
    # e.g., BTC_USDC or BTC_USDC_PERP
    s = sym.upper().strip()
    parts = s.split("_")
    perp = s.endswith("_PERP")
    if perp:
        parts = parts[:-1]
    if len(parts) >= 2:
        return parts[0], parts[1], perp
    return s, "", perp


def _norm_sym_lg(sym: str) -> Tuple[str, str, bool]:
    # best-effort: split by non-alnum, search for BASE,QUOTE tokens
    s = sym.upper().strip()
    for sep in ("-", "_", "/"):
        if sep in s:
            parts = s.split(sep)
            if len(parts) >= 2:
                base = parts[0]
                quote = parts[1]
                perp = (len(parts) >= 3 and parts[2] in ("PERP", "FUT", "SWAP")) or ("PERP" in s)
                return base, quote, perp
    # fallback: assume BASEUSDC style
    if s.endswith("USDC"):
        base = s[:-4]
        return base, "USDC", ("PERP" in s)
    return s, "", ("PERP" in s)


async def discover_pairs(lighter: LighterConnector, backpack: BackpackConnector) -> List[Tuple[str, str]]:
    lg_syms = await lighter.list_symbols()
    bp_syms = await backpack.list_symbols()
    # index lighter by (base,quote,perp?) with tolerance on perp flag (many lighter symbols may omit suffix)
    idx: Dict[Tuple[str, str, bool], List[str]] = {}
    for s in lg_syms:
        b, q, p = _norm_sym_lg(s)
        idx.setdefault((b, q, p), []).append(s)
        # also allow map without perp flag
        idx.setdefault((b, q, False), []).append(s)
    matches: List[Tuple[str, str]] = []
    for s in bp_syms:
        b, q, p = _norm_sym_bp(s)
        if q != "USDC":
            continue
        # prefer perp
        keys = [ (b, q, True), (b, q, False) ]
        for k in keys:
            cands = idx.get(k)
            if cands:
                matches.append((s, cands[0]))
                break
    return matches


async def pick_size_common(backpack: BackpackConnector, lighter: LighterConnector, bp_sym: str, lg_sym: str) -> Tuple[int, int, int]:
    # returns (size_i, p_dec_bp, s_dec_bp) in backpack integer units; lighter uses its own inside place_market
    p_dec_bp, s_dec_bp = await backpack.get_price_size_decimals(bp_sym)
    # min on bp
    await backpack._ensure_markets()
    mi = backpack._market_info.get(bp_sym) or {}
    qf = (mi.get("filters") or {}).get("quantity") or {}
    minq = float(qf.get("minQuantity", "0.00001"))
    size_bp_i = max(1, int(round(minq * (10 ** s_dec_bp))))
    # try to respect lighter min as well
    try:
        size_lg_i = await lighter.get_min_order_size_i(lg_sym)
    except Exception:
        size_lg_i = 1
    # choose the larger in bp units by converting to approx float
    size = size_bp_i
    return size, p_dec_bp, s_dec_bp


async def main():
    # configs
    lg = LighterConnector(LighterConfig())
    bp = BackpackConnector(BackpackConfig())
    lg.start()
    bp.start()

    # WS minimal (not strictly needed here)
    await lg.start_ws_state()
    await bp.start_ws_state([])

    pairs = await discover_pairs(lg, bp)
    if not pairs:
        print("No matching symbols found between Backpack and Lighter")
        return
    print(f"matched_pairs={len(pairs)} sample={pairs[:5]}")

    try:
        while True:
            bp_sym, lg_sym = random.choice(pairs)
            # focus on perps if available
            if not bp_sym.endswith("_PERP"):
                # try to find a perp alt
                base, quote, _ = _norm_sym_bp(bp_sym)
                perp_candidate = f"{base}_{quote}_PERP"
                if any(p[0] == perp_candidate for p in pairs):
                    bp_sym = perp_candidate
                    lg_sym = next(p[1] for p in pairs if p[0] == bp_sym)

            size_i, p_dec_bp, s_dec_bp = await pick_size_common(bp, lg, bp_sym, lg_sym)
            bid_bp, ask_bp, scale_bp = await bp.get_top_of_book(bp_sym)
            print(f"cycle pick: {bp_sym} ~ {lg_sym} size_i={size_i} tob_bp=({bid_bp},{ask_bp}) scale={scale_bp}")

            # choose direction randomly: long means Bid on backpack and Buy on lighter? For hedge, do opposite sides.
            long = bool(random.getrandbits(1))
            # backpack挂单（限价不post_only，尽量靠近盘口）
            if long:
                price_i = bid_bp or ask_bp or scale_bp
                if bid_bp and ask_bp:
                    price_i = max(1, min(bid_bp, ask_bp - 1))
                bp_cio = int(time.time() * 1000) % 1_000_000
                ret_bp, _r, err_bp = await bp.place_limit(bp_sym, bp_cio, size_i, price=price_i, is_ask=False, post_only=False, reduce_only=0)
                print(f"bp limit bid placed: err={err_bp} ret_id={getattr(ret_bp,'id',None) if hasattr(ret_bp,'id') else (ret_bp.get('id') if isinstance(ret_bp,dict) else None)}")
                # lighter吃单（市价卖）以对冲
                lg_cio = (bp_cio + 1) % 1_000_000
                _tbid, _task, _sc = await lg.get_top_of_book(lg_sym)
                _tx, ret_lg, err_lg = await lg.place_market(lg_sym, lg_cio, base_amount=size_i, is_ask=True, reduce_only=0)
                print(f"lg market sell (open hedge): err={err_lg}")
            else:
                price_i = ask_bp or bid_bp or scale_bp
                if bid_bp and ask_bp:
                    price_i = max(1, ask_bp)
                bp_cio = int(time.time() * 1000) % 1_000_000
                ret_bp, _r, err_bp = await bp.place_limit(bp_sym, bp_cio, size_i, price=price_i, is_ask=True, post_only=False, reduce_only=0)
                print(f"bp limit ask placed: err={err_bp} ret_id={ret_bp.get('id') if isinstance(ret_bp,dict) else None}")
                # lighter吃单（市价买）以对冲
                lg_cio = (bp_cio + 1) % 1_000_000
                _tbid, _task, _sc = await lg.get_top_of_book(lg_sym)
                _tx, ret_lg, err_lg = await lg.place_market(lg_sym, lg_cio, base_amount=size_i, is_ask=False, reduce_only=0)
                print(f"lg market buy (open hedge): err={err_lg}")

            # 持仓 10-15 分钟
            hold_s = random.randint(600, 900)
            print(f"holding for {hold_s}s...")
            await asyncio.sleep(hold_s)

            # 平仓：backpack 挂相反限价，lighter 市价相反
            bid_bp, ask_bp, _ = await bp.get_top_of_book(bp_sym)
            if long:
                # close long -> sell on bp, buy on lg
                price_i = ask_bp or bid_bp or scale_bp
                if bid_bp and ask_bp:
                    price_i = max(1, ask_bp)
                bp_cio2 = int(time.time() * 1000) % 1_000_000
                ret_bp2, _r2, err_bp2 = await bp.place_limit(bp_sym, bp_cio2, size_i, price=price_i, is_ask=True, post_only=False, reduce_only=1)
                print(f"bp limit ask close: err={err_bp2}")
                lg_cio2 = (bp_cio2 + 1) % 1_000_000
                _tx2, ret_lg2, err_lg2 = await lg.place_market(lg_sym, lg_cio2, base_amount=size_i, is_ask=False, reduce_only=1)
                print(f"lg market buy (close hedge): err={err_lg2}")
            else:
                price_i = bid_bp or ask_bp or scale_bp
                if bid_bp and ask_bp:
                    price_i = max(1, min(bid_bp, ask_bp - 1))
                bp_cio2 = int(time.time() * 1000) % 1_000_000
                ret_bp2, _r2, err_bp2 = await bp.place_limit(bp_sym, bp_cio2, size_i, price=price_i, is_ask=False, post_only=False, reduce_only=1)
                print(f"bp limit bid close: err={err_bp2}")
                lg_cio2 = (bp_cio2 + 1) % 1_000_000
                _tx2, ret_lg2, err_lg2 = await lg.place_market(lg_sym, lg_cio2, base_amount=size_i, is_ask=True, reduce_only=1)
                print(f"lg market sell (close hedge): err={err_lg2}")

            cool = random.randint(0, 300)
            print(f"cooldown {cool}s...")
            await asyncio.sleep(cool)

    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        try:
            await lg.stop_ws_state()
        except Exception:
            pass
        try:
            await bp.stop_ws_state()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

