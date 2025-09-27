from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional, Tuple

from .orders import OrderState, TrackingLimitOrder

TopOfBookFetcher = Callable[[], Awaitable[Tuple[Optional[int], Optional[int], int]]]


class TrackingLimitTimeoutError(TimeoutError):
    """Raised when a tracking limit order fails to fill before the timeout."""


async def place_tracking_limit_order(
    connector: Any,
    *,
    symbol: str,
    base_amount_i: int,
    is_ask: bool,
    interval_secs: float = 10.0,
    timeout_secs: float = 120.0,
    price_offset_ticks: int = 0,
    cancel_wait_secs: float = 2.0,
    post_only: bool = False,
    reduce_only: int = 0,
    logger: Optional[Any] = None,
    max_attempts: Optional[int] = None,
) -> TrackingLimitOrder:
    """Continuously re-post a limit order at the top of book until filled.

    Args:
        connector: Exchange connector implementing submit/cancel helpers.
        symbol: Trading symbol.
        base_amount_i: Base asset amount expressed in integer units.
        is_ask: True to sell, False to buy.
        interval_secs: How often to refresh the quote (seconds).
        timeout_secs: Maximum total time to keep tracking (seconds).
        price_offset_ticks: Optional adjustment in integer ticks away from top of book.
        cancel_wait_secs: Grace period to wait for cancellation settlement.
        post_only: Whether to request post-only placement when connector supports it.
        reduce_only: Reduce-only flag (0/1) forwarded to the connector.
        logger: Optional logger for progress messages.

    Returns:
        TrackingLimitOrder representing the final attempt.

    Raises:
        TrackingLimitTimeoutError: When the order is not filled before timeout.
        RuntimeError: When top-of-book information cannot be determined.
    """

    if base_amount_i <= 0:
        raise ValueError("base_amount_i must be positive")

    loop = asyncio.get_event_loop()
    coi_seed = int(time.time() * 1000) % 1_000_000 or 1

    def next_coi() -> int:
        nonlocal coi_seed
        coi_seed = (coi_seed + 1) % 1_000_000
        return coi_seed or 1

    price_dec, _size_dec = await _get_price_size_decimals(connector, symbol)
    price_scale = 10 ** price_dec

    start_ts = time.monotonic()
    attempts = 0
    tracker: Optional[TrackingLimitOrder] = None

    max_attempts = max_attempts if (max_attempts is None or max_attempts > 0) else 1

    while True:
        attempts += 1
        remaining = timeout_secs - (time.monotonic() - start_ts)
        if remaining <= 0:
            raise TrackingLimitTimeoutError("tracking limit timeout reached")

        bid_i, ask_i, scale = await _top_of_book(connector, symbol, price_scale)
        price_i = _select_price(bid_i, ask_i, scale, price_offset_ticks, is_ask)
        client_order_index = next_coi()

        if logger:
            logger.info(
                "tracking_limit book symbol=%s bid_i=%s ask_i=%s bid=%s ask=%s",
                symbol,
                bid_i,
                ask_i,
                _format_price(bid_i, scale),
                _format_price(ask_i, scale),
            )
            logger.info(
                "tracking_limit attempt=%s symbol=%s side=%s price_i=%s size_i=%s",
                attempts,
                symbol,
                "sell" if is_ask else "buy",
                price_i,
                base_amount_i,
            )

        tracker = await _submit_limit(
            connector,
            symbol=symbol,
            client_order_index=client_order_index,
            base_amount_i=base_amount_i,
            price_i=price_i,
            is_ask=is_ask,
            post_only=post_only,
            reduce_only=reduce_only,
        )

        wait_time = min(interval_secs, remaining)
        try:
            await tracker.wait_final(timeout=wait_time if wait_time > 0 else None)
        except asyncio.TimeoutError:
            pass

        if tracker.state == OrderState.FILLED:
            return tracker

        if tracker.state == OrderState.FAILED:
            return tracker

        if tracker.state == OrderState.CANCELLED:
            if max_attempts is not None and attempts >= max_attempts:
                return tracker
            continue

        await _cancel_order(
            connector,
            symbol=symbol,
            client_order_index=client_order_index,
            wait_secs=cancel_wait_secs,
        )

        if max_attempts is not None and attempts >= max_attempts:
            return tracker

    # Should never reach, added for mypy completeness.
    return tracker  # type: ignore[return-value]


async def _get_price_size_decimals(connector: Any, symbol: str) -> Tuple[int, int]:
    func = getattr(connector, "get_price_size_decimals", None)
    if not callable(func):
        raise RuntimeError("connector missing get_price_size_decimals")
    return await func(symbol)


async def _top_of_book(connector: Any, symbol: str, fallback_scale: int) -> Tuple[Optional[int], Optional[int], int]:
    getter = getattr(connector, "get_top_of_book", None)
    if callable(getter):
        try:
            bid_i, ask_i, scale = await getter(symbol)
            return bid_i, ask_i, scale
        except Exception:
            pass
    order_book_fn = getattr(connector, "get_order_book", None)
    if callable(order_book_fn):
        try:
            ob = await order_book_fn(symbol, depth=5)
            if isinstance(ob, dict):
                bids = ob.get("bids") or []
                asks = ob.get("asks") or []
                bid_i = _price_to_int(bids[0][0], fallback_scale) if bids else None
                ask_i = _price_to_int(asks[0][0], fallback_scale) if asks else None
                return bid_i, ask_i, fallback_scale
        except Exception:
            pass
    return None, None, fallback_scale


def _price_to_int(value: Any, scale: int) -> int:
    try:
        return int(float(value) * scale)
    except Exception:
        return 0


def _select_price(
    bid_i: Optional[int],
    ask_i: Optional[int],
    scale: int,
    offset_ticks: int,
    is_ask: bool,
) -> int:
    offset = max(offset_ticks, 0)
    fallback = max(int(25_000 * scale), 1)
    if is_ask:
        base = ask_i if ask_i is not None else bid_i
        if base is None:
            return fallback
        price = base + offset
        if bid_i is not None:
            price = max(price, bid_i + 1)
        return max(price, 1)
    base = bid_i if bid_i is not None else ask_i
    if base is None:
        return fallback
    price = max(base - offset, 1)
    if ask_i is not None:
        price = min(price, max(ask_i - 1, 1))
    return max(price, 1)


def _format_price(value_i: Optional[int], scale: int) -> str:
    if value_i is None or scale <= 0:
        return "?"
    try:
        return f"{value_i / scale:.8f}"
    except Exception:
        return str(value_i)


async def _submit_limit(
    connector: Any,
    *,
    symbol: str,
    client_order_index: int,
    base_amount_i: int,
    price_i: int,
    is_ask: bool,
    post_only: bool,
    reduce_only: int,
) -> TrackingLimitOrder:
    submit_limit = getattr(connector, "submit_limit_order", None)
    if callable(submit_limit):
        try:
            return await submit_limit(
                symbol=symbol,
                client_order_index=client_order_index,
                base_amount=base_amount_i,
                price=price_i,
                is_ask=is_ask,
                post_only=post_only,
                reduce_only=reduce_only,
            )
        except TypeError:
            return await submit_limit(symbol, client_order_index, base_amount_i, price_i, is_ask)

    place_limit = getattr(connector, "place_limit", None)
    if not callable(place_limit):
        raise RuntimeError("connector missing limit order placement")

    tracker = connector.create_tracking_limit_order(
        client_order_index,
        symbol=symbol,
        is_ask=is_ask,
        price_i=price_i,
        size_i=base_amount_i,
    )
    try:
        await place_limit(
            symbol=symbol,
            client_order_index=client_order_index,
            base_amount=base_amount_i,
            price=price_i,
            is_ask=is_ask,
            post_only=post_only,
            reduce_only=reduce_only,
        )
    except TypeError:
        await place_limit(symbol, client_order_index, base_amount_i, price_i, is_ask)
    return tracker


async def _cancel_order(
    connector: Any,
    *,
    symbol: str,
    client_order_index: int,
    wait_secs: float,
) -> None:
    cancel_fns = (
        "cancel_by_client_id",
        "cancel_by_client_order_index",
    )
    for name in cancel_fns:
        fn = getattr(connector, name, None)
        if callable(fn):
            try:
                result = fn(symbol, client_order_index)
            except TypeError:
                result = fn(client_order_index)
            if asyncio.iscoroutine(result):
                try:
                    await asyncio.wait_for(result, timeout=wait_secs)
                except asyncio.TimeoutError:
                    pass
            return

    cancel_all = getattr(connector, "cancel_all", None)
    if callable(cancel_all):
        result = cancel_all()
        if asyncio.iscoroutine(result):
            try:
                await asyncio.wait_for(result, timeout=wait_secs)
            except asyncio.TimeoutError:
                pass
