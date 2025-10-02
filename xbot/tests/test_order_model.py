import asyncio
import pytest

from execution.models import Order, OrderEvent, OrderState


@pytest.mark.asyncio
async def test_order_reaches_final_state():
    order = Order(venue="test", symbol="SOL", client_order_index=1, is_ask=False)
    await order.apply_update(OrderEvent(state=OrderState.SUBMITTING))
    await order.apply_update(OrderEvent(state=OrderState.OPEN))

    final_event = OrderEvent(state=OrderState.FILLED, info={"filled_base_i": 100})
    waiter = asyncio.create_task(order.wait_final())
    await order.apply_update(final_event)

    resolved = await waiter
    assert resolved.state is OrderState.FILLED
    assert order.state is OrderState.FILLED
    assert order.history[-1].info["filled_base_i"] == 100


@pytest.mark.asyncio
async def test_order_waits_for_timeout():
    order = Order(venue="test", symbol="SOL", client_order_index=2, is_ask=True)
    await order.apply_update(OrderEvent(state=OrderState.SUBMITTING))

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(order.wait_final(), timeout=0.05)

    await order.apply_update(OrderEvent(state=OrderState.CANCELLED))
    final = await order.wait_final()
    assert final.state is OrderState.CANCELLED
