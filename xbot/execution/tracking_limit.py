"""Tracking limit order implementation - THE ONLY IMPLEMENTATION.

This is the sole implementation for tracking limit orders in the system.
All other tracking limit implementations are forbidden.
Target: < 400 lines.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional, Tuple

from .order_model import Order, OrderEvent, OrderState


class TrackingLimitResult:
    """Result from tracking limit order execution."""

    def __init__(self, order: Order):
        self.order = order
        self.attempts = 0

    @property
    def state(self) -> str:
        return self.order.state.value

    @property
    def client_order_id(self) -> Optional[int]:
        return self.order.coi

    @property
    def filled_base_i(self) -> int:
        return self.order.filled_base_i

    async def wait_final(self, timeout: Optional[float] = None) -> str:
        """Wait for order to reach final state."""
        final_state = await self.order.wait_final(timeout=timeout)
        return final_state.value


class TrackingLimitTimeoutError(TimeoutError):
    """Raised when a tracking limit order fails to fill before the timeout."""


# SimpleTrackingResult is replaced by Order model


async def place_tracking_limit_order(
    connector: Any,
    *,
    symbol: str,
    coi: Optional[int] = None,
    size_i: int,
    price_i: int,
    is_ask: bool,
    interval_secs: float = 10.0,
    timeout_secs: float = 120.0,
    price_offset_ticks: int = 0,
    cancel_wait_secs: float = 2.0,
    post_only: bool = False,
    reduce_only: int = 0,
    logger: Optional[Any] = None,
    max_attempts: Optional[int] = None,
    coi_provider: Optional[Callable[[], int]] = None,
    **kwargs
) -> TrackingLimitResult:
    """Place tracking limit order - ONLY IMPLEMENTATION.

    Continuously re-post a limit order at the top of book until filled.

    Args:
        connector: Exchange connector
        symbol: Trading symbol
        coi: Client order index (if None, auto-generated)
        size_i: Size in integer format
        price_i: Price in integer format (can be adjusted based on book)
        is_ask: True for sell, False for buy
        interval_secs: Refresh interval
        timeout_secs: Maximum total time
        price_offset_ticks: Offset from top of book
        cancel_wait_secs: Cancel grace period
        post_only: Post-only flag
        reduce_only: Reduce-only flag
        logger: Optional logger
        max_attempts: Maximum attempts
        coi_provider: COI provider function

    Returns:
        TrackingLimitResult

    Raises:
        TrackingLimitTimeoutError: On timeout
        RuntimeError: On configuration errors
    """
    if size_i <= 0:
        raise ValueError("size_i must be positive")

    # Generate COI if not provided
    if coi is None:
        if coi_provider:
            coi = coi_provider()
        else:
            coi = int(time.time() * 1000) % 1_000_000 or 1

    # Get price/size decimals for the symbol
    try:
        price_dec, size_dec = await _get_price_size_decimals(connector, symbol)
        price_scale = 10 ** price_dec
    except Exception as e:
        if logger:
            logger.error(f"Failed to get decimals for {symbol}: {e}")
        price_scale = 100  # Default fallback

    start_ts = time.monotonic()
    attempts = 0

    # Create order using new model
    order = Order(
        coi=coi,
        venue="unknown",  # Will be set by connector
        symbol=symbol,
        side="sell" if is_ask else "buy",
        size_i=size_i,
        price_i=price_i,
        is_limit=True,
        post_only=post_only,
        reduce_only=reduce_only
    )

    result = TrackingLimitResult(order)

    if logger:
        logger.info(
            f"Tracking limit start: {symbol} coi={coi} size_i={size_i} "
            f"price_i={price_i} is_ask={is_ask} timeout={timeout_secs}s"
        )

    while True:
        attempts += 1
        result.attempts = attempts

        remaining = timeout_secs - (time.monotonic() - start_ts)
        if remaining <= 0:
            order.update_state(OrderState.FAILED, "timeout")
            raise TrackingLimitTimeoutError("Tracking limit timeout reached")

        # Get current top of book
        try:
            bid_i, ask_i, scale = await _get_top_of_book(connector, symbol, price_scale)

            # Adjust price based on book and offset
            if price_offset_ticks > 0:
                adjusted_price_i = _select_price(bid_i, ask_i, scale, price_offset_ticks, is_ask)
            else:
                adjusted_price_i = price_i

            if logger:
                logger.info(
                    f"Attempt {attempts}: bid_i={bid_i} ask_i={ask_i} "
                    f"adjusted_price_i={adjusted_price_i}"
                )

        except Exception as e:
            if logger:
                logger.error(f"Failed to get top of book: {e}")
            adjusted_price_i = price_i

        # Submit order
        try:
            order_result = await _submit_limit_order(
                connector=connector,
                symbol=symbol,
                coi=coi,
                size_i=size_i,
                price_i=adjusted_price_i,
                is_ask=is_ask,
                post_only=post_only,
                reduce_only=reduce_only
            )

            if logger:
                logger.info(f"Submitted order attempt {attempts}, coi={coi}")

        except Exception as e:
            if logger:
                logger.error(f"Failed to submit order: {e}")
            order.update_state(OrderState.FAILED, "submit_error")
            return result

        # Wait for fill or timeout
        wait_time = min(interval_secs, remaining)
        wait_start = time.monotonic()

        try:
            # Check if we have order tracking from connector
            if hasattr(order_result, 'wait_final'):
                final_state = await order_result.wait_final(timeout=wait_time)

                if final_state == "FILLED":
                    order.update_state(OrderState.FILLED, "connector")
                    if hasattr(order_result, 'filled_base_i'):
                        order.filled_base_i = order_result.filled_base_i
                    if logger:
                        logger.info(f"Order filled on attempt {attempts}")
                    return result

                if final_state in ["FAILED", "REJECTED"]:
                    order.update_state(OrderState.FAILED, "connector")
                    if logger:
                        logger.info(f"Order failed on attempt {attempts}: {final_state}")
                    return result

            else:
                # Fallback: just wait
                await asyncio.sleep(wait_time)
                order.update_state(OrderState.OPEN, "assumed")

        except asyncio.TimeoutError:
            order.update_state(OrderState.OPEN, "wait_timeout")
            if logger:
                logger.info(f"Wait timeout on attempt {attempts}")

        # Check if we should cancel and retry
        wait_actual = time.monotonic() - wait_start

        if order.state in [OrderState.OPEN, OrderState.SUBMITTING]:
            if logger:
                logger.info(f"Cancelling order attempt {attempts} after {wait_actual:.1f}s")

            try:
                await _cancel_order(connector, symbol, coi, cancel_wait_secs, logger)
                order.update_state(OrderState.CANCELLED, "manual_cancel")
            except Exception as e:
                if logger:
                    logger.error(f"Cancel failed: {e}")

        # Check attempt limits
        if max_attempts and attempts >= max_attempts:
            order.update_state(OrderState.FAILED, "max_attempts")
            if logger:
                logger.info(f"Max attempts ({max_attempts}) reached")
            return result

        # Update COI for next attempt
        if coi_provider:
            coi = coi_provider()
        else:
            coi = (coi + 1) % 1_000_000 or 1
        order.coi = coi

        if logger:
            logger.info(f"Starting attempt {attempts + 1} with coi={coi}")

    return result


async def _get_price_size_decimals(connector: Any, symbol: str) -> Tuple[int, int]:
    """Get price and size decimal places for symbol."""
    func = getattr(connector, "get_price_size_decimals", None)
    if not callable(func):
        raise RuntimeError("Connector missing get_price_size_decimals")
    return await func(symbol)


async def _get_top_of_book(
    connector: Any,
    symbol: str,
    fallback_scale: int
) -> Tuple[Optional[int], Optional[int], int]:
    """Get top of book prices."""
    # Try get_top_of_book first
    getter = getattr(connector, "get_top_of_book", None)
    if callable(getter):
        try:
            bid_i, ask_i, scale = await getter(symbol)
            return bid_i, ask_i, scale
        except Exception:
            pass

    # Fallback to order book
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
    """Convert price to integer format."""
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
    """Select appropriate price based on top of book and offset."""
    offset = max(offset_ticks, 0)
    fallback = max(int(25_000 * scale), 1)

    if is_ask:
        # For sells, start from ask or bid
        base = ask_i if ask_i is not None else bid_i
        if base is None:
            return fallback
        price = base + offset
        # Ensure we're above the bid
        if bid_i is not None:
            price = max(price, bid_i + 1)
        return max(price, 1)
    else:
        # For buys, start from bid or ask
        base = bid_i if bid_i is not None else ask_i
        if base is None:
            return fallback
        price = max(base - offset, 1)
        # Ensure we're below the ask
        if ask_i is not None:
            price = min(price, max(ask_i - 1, 1))
        return max(price, 1)


async def _submit_limit_order(
    connector: Any,
    *,
    symbol: str,
    coi: int,
    size_i: int,
    price_i: int,
    is_ask: bool,
    post_only: bool,
    reduce_only: int,
) -> Any:
    """Submit limit order using connector."""
    # Try new interface first
    submit_limit = getattr(connector, "submit_limit_order", None)
    if callable(submit_limit):
        try:
            return await submit_limit(
                symbol=symbol,
                client_order_index=coi,
                base_amount=size_i,
                price=price_i,
                is_ask=is_ask,
                post_only=post_only,
                reduce_only=reduce_only,
            )
        except TypeError:
            # Fallback to positional args
            return await submit_limit(symbol, coi, size_i, price_i, is_ask)

    # Try old interface
    place_limit = getattr(connector, "place_limit", None)
    if callable(place_limit):
        # Create Order object
        order = Order(
            coi=coi,
            venue="unknown",
            symbol=symbol,
            side="sell" if is_ask else "buy",
            size_i=size_i,
            price_i=price_i,
            state=OrderState.NEW,
            is_limit=True,
            post_only=post_only,
            reduce_only=reduce_only
        )

        try:
            await place_limit(
                symbol=symbol,
                client_order_index=coi,
                base_amount=size_i,
                price=price_i,
                is_ask=is_ask,
                post_only=post_only,
                reduce_only=reduce_only,
            )
            order.update_state(OrderState.SUBMITTING, "place_limit")
        except TypeError:
            await place_limit(symbol, coi, size_i, price_i, is_ask)
            order.update_state(OrderState.SUBMITTING, "place_limit")
        except Exception:
            order.update_state(OrderState.FAILED, "place_limit_error")

        return order

    # Try connector's create_tracking_limit_order if available
    create_tracking = getattr(connector, "create_tracking_limit_order", None)
    if callable(create_tracking):
        return create_tracking(
            coi,
            symbol=symbol,
            is_ask=is_ask,
            price_i=price_i,
            size_i=size_i,
        )

    # Fallback: create simple Order object
    order = Order(
        coi=coi,
        venue="unknown",
        symbol=symbol,
        side="sell" if is_ask else "buy",
        size_i=size_i,
        price_i=price_i,
        state=OrderState.SUBMITTING,
        is_limit=True,
        post_only=post_only,
        reduce_only=reduce_only
    )

    return order


async def _cancel_order(
    connector: Any,
    symbol: str,
    coi: int,
    wait_secs: float,
    logger: Optional[Any] = None,
) -> None:
    """Cancel order by client order index."""
    if logger:
        logger.info(f"Cancelling order: {symbol} coi={coi}")

    # Try various cancel methods
    cancel_methods = [
        "cancel_by_client_id",
        "cancel_by_client_order_index",
        "cancel_order"
    ]

    for method_name in cancel_methods:
        method = getattr(connector, method_name, None)
        if callable(method):
            try:
                # Try with symbol first
                try:
                    result = method(symbol, coi)
                except TypeError:
                    # Fallback to just COI
                    result = method(coi)

                if asyncio.iscoroutine(result):
                    await asyncio.wait_for(result, timeout=wait_secs)

                if logger:
                    logger.info(f"Cancelled using {method_name}")
                return

            except Exception as e:
                if logger:
                    logger.warning(f"Cancel method {method_name} failed: {e}")

    # Last resort: cancel all
    cancel_all = getattr(connector, "cancel_all", None)
    if callable(cancel_all):
        try:
            result = cancel_all()
            if asyncio.iscoroutine(result):
                await asyncio.wait_for(result, timeout=wait_secs)
            if logger:
                logger.info("Cancelled using cancel_all")
        except Exception as e:
            if logger:
                logger.warning(f"Cancel all failed: {e}")
    else:
        if logger:
            logger.warning("No cancel methods available on connector")