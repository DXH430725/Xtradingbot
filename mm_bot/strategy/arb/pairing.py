"""Pair discovery and sizing helpers shared by cross-market arbitrage strategies."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "normalize_backpack_symbol",
    "normalize_lighter_symbol",
    "discover_pairs",
    "pick_common_sizes",
]


def normalize_backpack_symbol(sym: str) -> Tuple[str, str, bool]:
    """Return (base, quote, is_perp) for a Backpack symbol."""
    s = sym.upper().strip()
    perp = False
    for sep in ("_", "-", "/"):
        if sep in s:
            parts = s.split(sep)
            if parts[-1] in ("PERP", "FUT", "SWAP"):
                perp = True
                parts = parts[:-1]
            if len(parts) >= 2:
                return parts[0], parts[1], perp or ("PERP" in s)
    if s.endswith("PERP"):
        perp = True
        s = s[:-4]
    if s.endswith("USDC"):
        base = s[:-4]
        return base, "USDC", perp or ("PERP" in sym.upper())
    return s, "", perp or ("PERP" in sym.upper())


def normalize_lighter_symbol(sym: str) -> Tuple[str, str, bool]:
    """Return (base, quote, is_perp) for a Lighter symbol."""
    s = sym.upper().strip()
    for sep in ("-", "_", "/"):
        if sep in s:
            parts = s.split(sep)
            if len(parts) >= 2:
                base = parts[0]
                quote = parts[1]
                perp = (len(parts) >= 3 and parts[2] in ("PERP", "FUT", "SWAP")) or ("PERP" in s)
                return base, quote, perp
    if s.endswith("USDC"):
        base = s[:-4]
        return base, "USDC", ("PERP" in s)
    # Lighter symbols often omit quote; default to USDC perp
    return s, "USDC", True


async def discover_pairs(
    lighter: Any,
    backpack: Any,
    *,
    symbol_filters: Optional[List[str]] = None,
) -> Tuple[List[Tuple[str, str]], List[str], List[str], List[Tuple[str, Tuple[str, str, bool]]]]:
    """Return overlapping Backpack/Lighter markets along with discovery diagnostics."""
    lg_syms = await lighter.list_symbols()
    bp_syms = await backpack.list_symbols()
    idx: Dict[Tuple[str, str, bool], List[str]] = {}
    for s in lg_syms:
        b, q, p = normalize_lighter_symbol(s)
        idx.setdefault((b, q, p), []).append(s)
        # allow matching spot vs perp under same base/quote
        if p:
            idx.setdefault((b, q, False), []).append(s)
        else:
            idx.setdefault((b, q, True), []).append(s)
    matches: List[Tuple[str, str]] = []
    misses: List[Tuple[str, Tuple[str, str, bool]]] = []
    for s in bp_syms:
        b, q, p = normalize_backpack_symbol(s)
        if q != "USDC" or not p:
            continue
        found = False
        for k in [(b, q, True), (b, q, False)]:
            cands = idx.get(k)
            if cands:
                matches.append((s, cands[0]))
                found = True
                break
        if not found:
            misses.append((s, (b, q, p)))
    if symbol_filters:
        sf = {sym.upper() for sym in symbol_filters}
        matches = [p for p in matches if p[0].upper() in sf or p[1].upper() in sf]
    # prefer perps first
    matches.sort(key=lambda x: 0 if x[0].upper().endswith("_PERP") else 1)
    return matches, lg_syms, bp_syms, misses


async def pick_common_sizes(backpack, lighter, bp_sym: str, lg_sym: str) -> Optional[Dict[str, Any]]:
    """Pick venue-specific minimal sizes that align both legs in base units."""

    await backpack._ensure_markets()
    p_dec_bp, s_dec_bp = await backpack.get_price_size_decimals(bp_sym)
    mi = backpack._market_info.get(bp_sym) or {}
    qf = (mi.get("filters") or {}).get("quantity") or {}
    try:
        minq_bp = float(qf.get("minQuantity", "0.00001"))
    except Exception:
        minq_bp = 0.00001
    size_bp_min_i = max(1, int(round(minq_bp * (10 ** s_dec_bp))))

    try:
        size_lg_min_i = await lighter.get_min_order_size_i(lg_sym)
    except Exception:
        size_lg_min_i = 1
    try:
        _p_lg, s_dec_lg = await lighter.get_price_size_decimals(lg_sym)
    except Exception:
        s_dec_lg = 0

    base_bp_min = size_bp_min_i / float(10 ** s_dec_bp)
    base_lg_min = size_lg_min_i / float(10 ** max(s_dec_lg, 0))
    base_size = max(base_bp_min, base_lg_min)

    # iterate to align both venues after rounding to their increments
    for _ in range(4):
        size_bp_i = max(size_bp_min_i, int(round(base_size * (10 ** s_dec_bp))))
        size_lg_i = max(size_lg_min_i, int(round(base_size * (10 ** s_dec_lg)))) if s_dec_lg >= 0 else size_lg_min_i
        base_bp = size_bp_i / float(10 ** s_dec_bp)
        base_lg = size_lg_i / float(10 ** max(s_dec_lg, 0))
        new_base = max(base_bp, base_lg)
        if abs(new_base - base_size) <= 1e-12:
            break
        base_size = new_base

    # sanity: ensure both sides within tolerance
    base_bp = size_bp_i / float(10 ** s_dec_bp)
    base_lg = size_lg_i / float(10 ** max(s_dec_lg, 0))
    if base_bp <= 0 or base_lg <= 0:
        return None

    info = {
        "size_bp_i": int(size_bp_i),
        "size_lg_i": int(size_lg_i),
        "base_bp": float(base_bp),
        "base_lg": float(base_lg),
        "decimals_bp": int(s_dec_bp),
        "decimals_lg": int(s_dec_lg),
    }
    return info

