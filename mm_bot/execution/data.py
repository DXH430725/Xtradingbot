from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple


def normalize_symbol(symbol: Any) -> str:
    return "".join(ch for ch in str(symbol or "").upper() if ch.isalnum() or ch in {"_", "-"})


async def get_positions(connector: Any) -> List[Dict[str, Any]]:
    positions = await connector.get_positions()
    if isinstance(positions, list):
        return [dict(item) for item in positions]
    return []


async def get_position(
    connector: Any,
    symbol: str,
    *,
    default: float = 0.0,
    apply_sign: bool = True,
) -> float:
    key = normalize_symbol(symbol)
    for entry in await get_positions(connector):
        sym = normalize_symbol(entry.get("symbol") or entry.get("market"))
        if sym != key:
            continue
        try:
            value = entry.get("position") or entry.get("size") or entry.get("netQuantity")
            size = float(value)
        except Exception:
            return default
        if apply_sign:
            sign = entry.get("sign")
            if sign is not None:
                try:
                    size *= float(sign)
                except Exception:
                    pass
        return size
    return default


async def get_collateral(connector: Any, *, default: float = 0.0) -> float:
    try:
        overview = await connector.get_account_overview()
    except Exception:
        return default
    if not isinstance(overview, dict):
        return default
    accounts = overview.get("accounts")
    if isinstance(accounts, list) and accounts:
        account = accounts[0]
        for key in ("collateral", "available_balance", "balance", "equity"):
            if account.get(key) is not None:
                try:
                    return float(account.get(key))
                except Exception:
                    continue
    return default


async def plan_order_size(
    connector: Any,
    *,
    symbol: str,
    leverage: float,
    min_collateral: float,
    size_scale: int,
    min_size_i: int,
    price_scale_hint: Optional[int] = None,
    collateral_buffer: float = 0.96,
    logger: Optional[logging.Logger] = None,
) -> Optional[Dict[str, Any]]:
    log = logger or logging.getLogger("mm_bot.execution.plan")
    collateral = await get_collateral(connector)
    if collateral <= min_collateral:
        log.warning("collateral %.6f below minimum %.6f", collateral, min_collateral)
        return None
    try:
        bid_i, ask_i, scale = await connector.get_top_of_book(symbol)
    except Exception as exc:
        log.debug("top_of_book fetch failed: %s", exc)
        return None
    scale = scale or price_scale_hint or 1
    price_i = ask_i if leverage >= 0 else bid_i
    if price_i is None:
        price_i = bid_i or ask_i
    if price_i is None:
        return None
    price = price_i / float(scale)
    if price <= 0:
        return None
    leverage = max(leverage, 1.0)
    eff_collateral = collateral * max(min(collateral_buffer, 1.0), 0.0)
    notional = eff_collateral * leverage
    base_amount = notional / price
    size_i = max(min_size_i, int(round(base_amount * size_scale)))
    if size_i <= 0:
        return None
    return {
        "base_amount": base_amount,
        "size_i": size_i,
        "collateral": collateral,
        "price": price,
        "price_scale": scale,
    }


__all__ = [
    "normalize_symbol",
    "get_positions",
    "get_position",
    "get_collateral",
    "plan_order_size",
]
