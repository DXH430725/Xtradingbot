from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from mm_bot.execution.positions import flatten_position


async def emergency_unwind(
    connectors: Dict[str, Any],
    *,
    symbols: Dict[str, str],
    size_scales: Dict[str, int],
    tolerance: float = 1e-6,
    coi_manager=None,
    nonce_manager=None,
    api_key_indices: Optional[Dict[str, int]] = None,
    locks: Optional[Dict[str, asyncio.Lock]] = None,
    logger: Optional[logging.Logger] = None,
    notify: Optional[Callable[[Dict[str, bool]], Awaitable[None]]] = None,
) -> Dict[str, bool]:
    """Reduce all connector positions to zero using reduce-only market orders."""

    log = logger or logging.getLogger("mm_bot.execution.emergency")
    results: Dict[str, bool] = {}
    for venue, connector in connectors.items():
        symbol = symbols.get(venue)
        if not symbol:
            continue
        size_scale = size_scales.get(venue, 1)
        lock = locks.get(venue) if locks else None
        api_key = api_key_indices.get(venue) if api_key_indices else None
        success = await flatten_position(
            connector,
            symbol=symbol,
            size_scale=size_scale,
            tolerance=tolerance,
            attempts=3,
            retry_delay=0.5,
            coi_manager=coi_manager,
            nonce_manager=nonce_manager,
            api_key_index=api_key,
            lock=lock,
            logger=log,
        )
        results[venue] = success
        log.info("emergency unwind %s success=%s", venue, success)
    if notify is not None:
        try:
            await notify(results)
        except Exception as exc:
            log.debug("emergency notifier error: %s", exc)
    return results


__all__ = ["emergency_unwind"]
