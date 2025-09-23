import asyncio

from mm_bot.connector.base import BaseConnector, ConnectorEventType
from mm_bot.execution.orders import OrderState


class DummyConnector(BaseConnector):
    def __init__(self):
        super().__init__("dummy", debug=True)


def test_order_tracker_transitions():
    async def runner():
        conn = DummyConnector()
        events = []
        conn.register_listener(lambda evt: events.append(evt))

        order = conn.create_tracking_limit_order(1, symbol="TEST", is_ask=False, price_i=100, size_i=10)
        conn._update_order_state(client_order_id=1, symbol="TEST", state=OrderState.SUBMITTING, info={"step": "submit"})
        conn._update_order_state(client_order_id=1, exchange_order_id="abc", status="open", symbol="TEST", info={"step": "open"})
        final_update = conn._update_order_state(
            client_order_id=1,
            exchange_order_id="abc",
            status="filled",
            symbol="TEST",
            info={"step": "filled"},
        )

        result = await order.wait_final(timeout=1)
        assert result.state == OrderState.FILLED
        assert final_update.state == OrderState.FILLED
        assert any(evt.type == ConnectorEventType.ORDER for evt in events)
        assert order.exchange_order_id == "abc"

    asyncio.run(runner())
