import pytest
from decimal import Decimal

from execution.market_data_service import MarketDataService
from tests.stubs import StubConnector, SymbolMeta


@pytest.mark.asyncio
async def test_symbol_translation_and_quantization():
    connector = StubConnector(meta={
        "SOL_USDT": SymbolMeta(price_decimals=2, size_decimals=4, min_size_i=10, top_bid=100, top_ask=101),
    })
    service = MarketDataService(connector=connector, symbol_map={"SOL": "SOL_USDT"})

    assert service.resolve_symbol("sol") == "SOL_USDT"

    price_i = await service.to_price_i("SOL", Decimal("23.45"))
    assert price_i == 2345

    size_i = await service.to_size_i("SOL", Decimal("0.1234"))
    assert size_i == 1234

    with pytest.raises(ValueError):
        await service.ensure_min_size("SOL", 5)

    await service.ensure_min_size("SOL", 20)

    bid, ask, scale = await service.get_top_of_book("SOL")
    assert (bid, ask, scale) == (100, 101, 100)
