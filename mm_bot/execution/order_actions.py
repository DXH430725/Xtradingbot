from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional

from mm_bot.execution.orders import OrderState, TrackingLimitOrder, TrackingMarketOrder
from mm_bot.execution.tracking_limit import place_tracking_limit_order as legacy_tracking_limit

from .ids import COIManager, NonceManager


@asynccontextmanager
async def _optional_lock(lock: Optional[asyncio.Lock]) -> AsyncIterator[None]:
    if lock is None:
        yield
    else:
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()


async def place_tracking_market_order(
    connector: Any,
    *,
    symbol: str,
    size_i: int,
    is_ask: bool,
    reduce_only: int = 0,
    max_slippage: Optional[float] = None,
    attempts: int = 1,
    retry_delay: float = 0.0,
    wait_timeout: float = 30.0,
    coi_manager: Optional[COIManager] = None,
    nonce_manager: Optional[NonceManager] = None,
    api_key_index: Optional[int] = None,
    lock: Optional[asyncio.Lock] = None,
    label: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> Optional[TrackingMarketOrder]:
    """Submit a market order with retries, optional nonce refresh, and final-state wait."""

    if size_i <= 0:
        raise ValueError("size_i must be positive")

    attempts = max(1, int(attempts))
    retry_delay = max(0.0, float(retry_delay))
    wait_timeout = max(1.0, float(wait_timeout))
    venue = getattr(connector, "name", "connector")
    label = label or venue
    log = logger or logging.getLogger(f"mm_bot.execution.market.{venue}")

    tracker: Optional[TrackingMarketOrder] = None

    for attempt in range(1, attempts + 1):
        async with _optional_lock(lock):
            coi = coi_manager.next(label) if coi_manager else int(asyncio.get_event_loop().time() * 1_000_000) % 9_000_000 + 1
            submit_kwargs: Dict[str, Any] = {
                "symbol": symbol,
                "client_order_index": coi,
                "base_amount": size_i,
                "is_ask": is_ask,
                "reduce_only": reduce_only,
            }
            if max_slippage is not None:
                submit_kwargs["max_slippage"] = max_slippage

            log.debug(
                "%s attempt=%s submit coi=%s reduce_only=%s max_slippage=%s",
                label,
                attempt,
                coi,
                reduce_only,
                max_slippage,
            )

            tracker = await _submit_market_order(connector, submit_kwargs)

        if tracker is None:
            log.warning("%s attempt=%s failed to submit", label, attempt)
            await _sleep_if_needed(retry_delay, attempt)
            continue

        try:
            await tracker.wait_final(timeout=wait_timeout)
        except asyncio.TimeoutError:
            log.warning("%s attempt=%s timed out waiting for final state", label, attempt)

        if tracker.state == OrderState.FILLED:
            log.info("%s attempt=%s filled", label, attempt)
            return tracker

        snapshot = tracker.snapshot()
        info = snapshot.info if snapshot else None
        reason = getattr(tracker, "last_error", None) or getattr(tracker, "reject_reason", None)
        if reason is None and isinstance(info, dict):
            reason = info.get("error") or info.get("message") or info.get("reason")

        log.warning(
            "%s attempt=%s state=%s reason=%s info=%s",
            label,
            attempt,
            tracker.state.value,
            reason,
            info,
        )

        if nonce_manager and nonce_manager.is_nonce_error(info, reason):
            await nonce_manager.refresh(connector, api_key_index)

        if attempt < attempts:
            await _sleep_if_needed(retry_delay, attempt)

    return tracker


async def _submit_market_order(connector: Any, kwargs: Dict[str, Any]) -> Optional[TrackingMarketOrder]:
    submit = getattr(connector, "submit_market_order", None)
    if not callable(submit):
        return None
    try:
        return await submit(**kwargs)
    except TypeError:
        # retry without optional parameters not supported by connector
        cleaned = {k: v for k, v in kwargs.items() if k != "max_slippage"}
        return await submit(**cleaned)
    except Exception:
        return None


async def place_tracking_limit_order(
    connector: Any,
    *,
    symbol: str,
    base_amount_i: int,
    is_ask: bool,
    coi_manager: Optional[COIManager] = None,
    lock: Optional[asyncio.Lock] = None,
    label: Optional[str] = None,
    **kwargs: Any,
) -> TrackingLimitOrder:
    """Wrapper adding COI sequencing and optional locking around legacy limit helper."""

    venue = getattr(connector, "name", "connector")
    label = label or venue
    log = kwargs.get("logger") or logging.getLogger(f"mm_bot.execution.limit.{venue}")

    async with _optional_lock(lock):
        if coi_manager:
            kwargs.setdefault("coi_provider", lambda: coi_manager.next(label))
        return await legacy_tracking_limit(
            connector,
            symbol=symbol,
            base_amount_i=base_amount_i,
            is_ask=is_ask,
            **kwargs,
        )


async def _sleep_if_needed(delay: float, attempt: int) -> None:
    if delay <= 0:
        return
    await asyncio.sleep(delay if attempt <= 1 else delay * attempt)


__all__ = [
    "place_tracking_market_order",
    "place_tracking_limit_order",
]
