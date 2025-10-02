# Strategy Integration Guide

Strategies interact with the execution layer through `strategy.base.Strategy` and the thin `ExecutionRouter` facade.

## Building a Strategy
1. Subclass `strategy.base.Strategy`.
2. Use `self.router.market_data` to convert human-readable prices/sizes into integer ticks.
3. Submit orders through `self.router.submit_limit`, `self.router.submit_market`, or `self.router.tracking_limit`.
4. Use `self.router.positions` and `self.router.risk` for post-trade bookkeeping or guardrails.
5. Leverage `self.clock` (a `WallClock` wrapper) for sleeps and timers to keep tests deterministic.

Example snippet:
```python
from strategy.base import Strategy, StrategyConfig

class MomentumStrategy(Strategy):
    async def start(self) -> None:
        await super().start()
        size_i = await self.router.market_data.to_size_i(self.config.symbol, self.config.qty)
        await self.router.submit_market(symbol=self.config.symbol, is_ask=False, size_i=size_i)
        await self.clock.sleep(5)
        await self.router.submit_market(symbol=self.config.symbol, is_ask=True, size_i=size_i, reduce_only=1)
```

## Switching Venues
- Supply `--venue lighter`/`--venue grvt` at launch; the factory instantiates the matching connector and symbol map.
- Ensure the canonical symbol resolves via configuration (e.g. `SOL`→`SOL_USDT` for Lighter, `SOL_USDT_Perp` for GRVT).
- Venue-specific behaviour is encapsulated inside connectors, so strategies remain unchanged when swapping venues.

## Tracking-Limit Workflow
- Call `router.tracking_limit` with integer quantities. The service handles retries, cancellations, and final fills.
- Inspect `TrackingLimitOrder.attempts` for diagnostics (e.g. logging the price path).

## Testing Strategies
- Replace the live connector with `tests.stubs.StubConnector` or a purpose-built simulator and use `pytest.mark.asyncio` to drive the coroutine.
- Inject fake market data via `MarketDataService` overrides to simulate fills and stress edge cases.

## Cleanup
After live tests remember to flat positions manually or use a dedicated post-run routine. The sample `TrackingLimitStrategy` issues a closing market order automatically, but risk engines do not enforce flatness.
