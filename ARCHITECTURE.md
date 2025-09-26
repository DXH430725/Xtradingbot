# XTradingBot Architecture

XTradingBot is organised as a three-layer stack that keeps strategy logic decoupled from exchange execution and connectivity concerns.

## 1. Strategy Layer
- Contains trading logic in `mm_bot/strategy/*`.
- Strategies inherit from `StrategyBase` and are life-cycle managed by `TradingCore`.
- Strategies obtain connectors through the runner registry and rely on execution helpers (e.g. tracking orders) exposed by the execution layer.
- The smoke-test strategy (`ConnectorSmokeTestStrategy`) now drives a **tracking limit** diagnostic: it re-posts a limit order at top-of-book until filled, verifies order-state updates from both REST and websocket feeds, flattens the fill with a reduce-only market order, and confirms no residual exposure remains.

## 2. Execution Layer
- Located in `mm_bot/execution/`.
- `OrderTracker` maintains the order state machine (`NEW → SUBMITTING → OPEN → PARTIALLY_FILLED → FILLED/CANCELLED/FAILED`).
- `TrackingLimitOrder`/`TrackingMarketOrder` expose awaitable helpers so strategies can `await order.wait_final(...)` or stream incremental updates.
- `tracking_limit.py` adds `place_tracking_limit_order()`, a reusable helper that keeps replacing a limit order at the best bid/ask until it fills or times out, abstracting the loop that the smoke test (and future strategies) can reuse.
- Connectors inherit from `BaseConnector` to integrate tracking, fan out events, and provide consistent debug logging. The base automatically dispatches callbacks and logs every transition when debug mode is enabled.

## 3. Connector Layer
- Live in `mm_bot/connector/*` and now share the `BaseConnector` implementation.
- Responsibilities:
  - Start/stop exchange sessions (REST + WebSocket).
  - Map exchange-specific payloads to the common order state machine.
  - Expose higher level helpers (`submit_limit_order`, `submit_market_order`) that wrap raw REST/Signed calls and register tracking metadata.
  - Emit unified events (`ORDER`, `TRADE`, `POSITION`) to subscribed listeners.
- `BackpackConnector` and `LighterConnector` translate exchange statuses into state transitions while preserving existing functionality. `GrvtConnector` inherits the base skeleton and can be extended when the SDK is fully wired in.

## Runner & Registry
- The unified `mm_bot/bin/runner.py` replaces the legacy `run_*.py` entrypoints.
- `mm_bot/runtime/builders.py` + `registry.py` resolve connectors and strategies declared in configuration files. Strategies can opt into dynamic connector resolution (e.g. `smoke_test` enumerates connectors listed under its configuration block).
- `build_smoke_test_strategy()` now understands per-connector tracking intervals, timeouts, and cancel-wait settings so diagnostics can be tuned per venue.
- The runner waits for `TradingCore` to stop (signalled by the strategy) and exits with a non-zero code when the smoke test reports a failure.
- Legacy entrypoints are archived under `mm_bot/bin/legacy/` for reference.

## Connector Enhancements
- Connectors expose uniform metadata helpers (`get_market_info`, `get_price_size_decimals`, `get_top_of_book`) so the tracking helper can compute tick/size integers without duplicating logic.
- `BackpackConnector`, `LighterConnector`, and `GrvtConnector` surface both REST-derived and websocket-cached views of orders/positions, enabling the smoke test to cross-check state across transports.
- Websocket bootstrap happens inside `prepare_smoke_test` so the first market snapshot reflects live data before we submit the diagnostic orders.

## Current Focus
- Smoke-test infrastructure validates Backpack, Lighter, and GRVT connectivity end-to-end using the shared tracking-limit helper and per-connector configs.
- Execution helpers are being consolidated so production strategies can reuse the same primitives the diagnostics employ.

## Next Steps
1. Extend tracking diagnostics to cover reduce-only/partial-fill scenarios and surface richer telemetry.
2. Draft connector development documentation that explains required hooks, shared helpers, and testing expectations.
3. Begin onboarding the Aster exchange connector, reusing the shared `BaseConnector` instrumentation.

## Debug & Logging
- Enable detailed connector debug logs by setting `general.debug: true` or the `XTB_DEBUG=1` environment variable. When active, `BaseConnector` prints every order transition together with key metadata.
- Per-strategy telemetry/logging options remain configurable through the existing YAML files.
