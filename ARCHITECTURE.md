# XTradingBot Architecture

XTradingBot is organised as a three-layer stack that keeps strategy logic decoupled from exchange execution and connectivity concerns.

## 1. Strategy Layer
- Contains trading logic in `mm_bot/strategy/*`.
- Strategies inherit from `StrategyBase` and are life-cycle managed by `TradingCore`.
- Strategies obtain connectors through the runner registry and rely on execution helpers (e.g. tracking orders) exposed by the connector layer.
- The new smoke-test strategy (`ConnectorSmokeTestStrategy`) showcases the recommended interaction pattern: use the connector's `submit_limit_order`/`submit_market_order` helpers and await order completion through the tracking API instead of polling REST endpoints manually.

## 2. Execution Layer
- Located in `mm_bot/execution/`.
- `OrderTracker` maintains the order state machine (`NEW → SUBMITTING → OPEN → PARTIALLY_FILLED → FILLED/CANCELLED/FAILED`).
- `TrackingLimitOrder`/`TrackingMarketOrder` expose awaitable helpers for strategies so they can `await order.wait_final(...)` or tail incremental updates.
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
- The new `mm_bot/bin/runner.py` replaces the legacy `run_*.py` entrypoints.
- `mm_bot/runtime/builders.py` + `registry.py` resolve connectors and strategies declared in configuration files. Strategies can opt into dynamic connector resolution (e.g. `smoke_test` enumerates connectors listed under its configuration block).
- Legacy entrypoints are archived under `mm_bot/bin/legacy/` for reference.

## Debug & Logging
- Enable detailed connector debug logs by setting `general.debug: true` or the `XTB_DEBUG=1` environment variable. When active, `BaseConnector` prints every order transition together with key metadata.
- Per-strategy telemetry/logging options remain configurable through the existing YAML files.

