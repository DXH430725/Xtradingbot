import pytest
from decimal import Decimal

from execution.position_service import PositionService, PositionSnapshot
from execution.risk_service import RiskLimits, RiskService, RiskViolationError
from execution.market_data_service import MarketDataService
from tests.stubs import StubConnector, SymbolMeta


@pytest.mark.asyncio
async def test_risk_checks_min_size_and_position_limits():
    connector = StubConnector(meta={
        "SOL_USDT": SymbolMeta(price_decimals=2, size_decimals=4, min_size_i=10, top_bid=100, top_ask=101),
    })
    market_data = MarketDataService(connector=connector, symbol_map={"SOL": "SOL_USDT"})
    positions = PositionService()
    limits = RiskLimits(max_position=Decimal("0.2"))
    risk = RiskService(market_data=market_data, position_service=positions, limits=limits)

    await risk.validate_order(symbol="SOL", size_i=20, is_ask=False)

    snapshot = PositionSnapshot(
        symbol="SOL",
        base_qty=Decimal("0.2"),
        quote_value=Decimal("10"),
        notional=Decimal("10"),
    )
    await positions.ingest(snapshot)

    with pytest.raises(RiskViolationError):
        await risk.validate_order(symbol="SOL", size_i=20, is_ask=False)

    # selling should reduce exposure
    await risk.validate_order(symbol="SOL", size_i=20, is_ask=True)
