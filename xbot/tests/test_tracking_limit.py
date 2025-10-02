import asyncio
import pytest

from execution.market_data_service import MarketDataService
from execution.tracking_limit import TrackingLimitEngine, TrackingAttempt
from execution.models import OrderEvent, OrderState
from tests.stubs import StubConnector, SymbolMeta


class FakeOrder:
    def __init__(self, attempt: int) -> None:
        self.client_order_index = attempt
        self._final_event: OrderEvent | None = None
        self.state = OrderState.OPEN
        self._attempt = attempt
        self._timeout_emitted = False

    async def wait_final(self, timeout: float | None = None) -> OrderEvent:
        if self._final_event is None:
            if self._attempt == 1 and not self._timeout_emitted:
                self._timeout_emitted = True
                await asyncio.sleep(0)
                raise asyncio.TimeoutError()
            raise RuntimeError("final event not set")
        self.state = self._final_event.state
        return self._final_event

    def set_final(self, event: OrderEvent) -> None:
        self._final_event = event
        self.state = event.state

    def snapshot(self) -> OrderEvent | None:
        return self._final_event


class FakeOrderService:
    def __init__(self) -> None:
        self.attempts = 0
        self.orders: list[FakeOrder] = []
        self.cancelled: list[int] = []

    async def submit_limit(self, **kwargs) -> FakeOrder:
        self.attempts += 1
        order = FakeOrder(self.attempts)
        if self.attempts >= 2:
            order.set_final(
                OrderEvent(state=OrderState.FILLED, info={"filled_base_i": kwargs["size_i"]})
            )
        self.orders.append(order)
        return order

    async def cancel(self, symbol: str, client_order_index: int) -> None:
        self.cancelled.append(client_order_index)
        for order in self.orders:
            if order.client_order_index == client_order_index:
                order.set_final(
                    OrderEvent(state=OrderState.CANCELLED, info={"filled_base_i": 0})
                )
                break


@pytest.mark.asyncio
async def test_tracking_limit_retries_until_filled():
    connector = StubConnector(meta={
        "SOL_USDT": SymbolMeta(price_decimals=2, size_decimals=0, min_size_i=1, top_bid=100, top_ask=101),
    })
    market_data = MarketDataService(connector=connector, symbol_map={"SOL": "SOL_USDT"})
    engine = TrackingLimitEngine(market_data=market_data, default_interval_secs=0.01, cancel_wait_secs=0.01)
    service = FakeOrderService()

    result = await engine.place(
        order_service=service,
        connector=connector,
        symbol="SOL",
        base_amount_i=100,
        is_ask=False,
        interval_secs=0.01,
        timeout_secs=1.0,
    )
    assert result.filled_base_i == 100
    assert result.attempts_count == 2
    assert service.cancelled == [1]
    assert isinstance(result.attempts[0], TrackingAttempt)
