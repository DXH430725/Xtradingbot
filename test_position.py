"""Quick test script for position getter."""
import asyncio
import sys
from xbot.app.config import load_config
from xbot.core.trading_core import TradingCore
from xbot.connector.backpack import BackpackConnector

async def test_position():
    # Load config
    config = load_config("conf/backpack_smoke_test.yaml")

    # Create connector
    bp_config = config.connectors.get("backpack")
    connector = BackpackConnector(bp_config)

    # Start connector
    await connector.start()

    # Wait a bit for position updates
    await asyncio.sleep(3)

    # Get position
    position = connector.get_position("SOL")
    if position:
        print(f"✅ Position found:")
        print(f"   Symbol: {position.symbol}")
        print(f"   Qty: {position.qty}")
        print(f"   Notional: {position.notional:.4f}")
        print(f"   Realized PnL: {position.realized_pnl:.6f}")
        print(f"   Unrealized PnL: {position.unrealized_pnl:.6f}")
        print(f"   Is Long: {position.is_long()}")
        print(f"   Is Flat: {position.is_flat()}")
    else:
        print("❌ No position found for SOL")

    # Stop connector
    await connector.stop()
    await connector.close()

if __name__ == "__main__":
    asyncio.run(test_position())
