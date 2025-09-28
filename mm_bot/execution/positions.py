from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from mm_bot.execution.orders import OrderState, TrackingMarketOrder

from .data import get_position
from .order_actions import place_tracking_market_order


async def confirm_position(
    connector: Any,
    *,
    symbol: str,
    target: float,
    tolerance: float,
    timeout: float = 5.0,
    poll_interval: float = 0.2,
    logger: Optional[logging.Logger] = None,
) -> Optional[float]:
    """Poll connector positions until target within tolerance or timeout."""

    log = logger or logging.getLogger("mm_bot.execution.confirm")
    tolerance = max(tolerance, 0.0)
    deadline = asyncio.get_event_loop().time() + max(timeout, poll_interval)
    while True:
        current = await get_position(connector, symbol)
        if abs(current - target) <= tolerance:
            return current
        if asyncio.get_event_loop().time() >= deadline:
            log.debug(
                "%s confirm timeout target=%.8f current=%.8f tolerance=%.8f",
                getattr(connector, "name", "connector"),
                target,
                current,
                tolerance,
            )
            return None
        await asyncio.sleep(poll_interval)


def resolve_filled_amount(
    tracker: TrackingMarketOrder,
    *,
    size_scale: int,
    fallback: int = 0,
) -> int:
    snap = tracker.snapshot()
    if snap and snap.filled_base is not None:
        try:
            amount = abs(float(snap.filled_base))
            amount_i = int(round(amount * size_scale))
            if amount_i > 0:
                return amount_i
        except Exception:
            pass
    inner = getattr(tracker, "_tracker", None)
    if inner is not None:
        for past in reversed(getattr(inner, "history", [])):
            filled = getattr(past, "filled_base", None)
            if filled is None:
                continue
            try:
                amount = abs(float(filled))
                amount_i = int(round(amount * size_scale))
                if amount_i > 0:
                    return amount_i
            except Exception:
                continue
    return fallback


async def rebalance_position(
    connector: Any,
    *,
    symbol: str,
    target: float,
    size_scale: int,
    min_size_i: int,
    tolerance: float,
    attempts: int = 3,
    retry_delay: float = 0.5,
    coi_manager=None,
    nonce_manager=None,
    api_key_index=None,
    lock=None,
    logger: Optional[logging.Logger] = None,
) -> bool:
    log = logger or logging.getLogger("mm_bot.execution.rebalance")
    attempts = max(1, int(attempts))
    retry_delay = max(0.0, float(retry_delay))
    tolerance = max(float(tolerance), 0.0)

    for attempt in range(1, attempts + 1):
        current = await get_position(connector, symbol)
        delta = target - current
        if abs(delta) <= tolerance:
            return True
        is_ask = delta < 0
        size_i = max(min_size_i, int(round(abs(delta) * size_scale)))
        tracker = await place_tracking_market_order(
            connector,
            symbol=symbol,
            size_i=size_i,
            is_ask=is_ask,
            reduce_only=0,
            attempts=1,
            wait_timeout=30.0,
            retry_delay=0.0,
            coi_manager=coi_manager,
            nonce_manager=nonce_manager,
            api_key_index=api_key_index,
            lock=lock,
            label=f"rebalance-{symbol}",
            logger=log,
        )
        if tracker and tracker.state == OrderState.FILLED:
            continue
        if attempt < attempts and retry_delay:
            await asyncio.sleep(retry_delay)
    log.error("rebalance failed symbol=%s target=%.8f", symbol, target)
    return False


async def flatten_position(
    connector: Any,
    *,
    symbol: str,
    size_scale: int,
    tolerance: float,
    attempts: int = 3,
    retry_delay: float = 0.5,
    coi_manager=None,
    nonce_manager=None,
    api_key_index=None,
    lock=None,
    logger: Optional[logging.Logger] = None,
) -> bool:
    log = logger or logging.getLogger("mm_bot.execution.flatten")
    for attempt in range(1, max(1, attempts) + 1):
        current = await get_position(connector, symbol)
        if abs(current) <= tolerance:
            return True
        is_ask = current > 0
        size_i = max(1, int(round(abs(current) * size_scale)))
        tracker = await place_tracking_market_order(
            connector,
            symbol=symbol,
            size_i=size_i,
            is_ask=is_ask,
            reduce_only=1,
            attempts=1,
            wait_timeout=30.0,
            coi_manager=coi_manager,
            nonce_manager=nonce_manager,
            api_key_index=api_key_index,
            lock=lock,
            label=f"flatten-{symbol}",
            logger=log,
        )
        if tracker and tracker.state == OrderState.FILLED:
            continue
        if attempt < attempts and retry_delay:
            await asyncio.sleep(retry_delay)
    log.error("flatten failed symbol=%s", symbol)
    return False


__all__ = [
    "confirm_position",
    "resolve_filled_amount",
    "rebalance_position",
    "flatten_position",
]
