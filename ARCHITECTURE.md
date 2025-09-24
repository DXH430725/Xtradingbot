# XTradingBot Architecture

XTradingBot is organised as a three-layer stack that keeps strategy logic decoupled from exchange execution and connectivity concerns.

## 1. Strategy Layer
- Contains trading logic in `mm_bot/strategy/*`.
- Strategies inherit from `StrategyBase` and are life-cycle managed by `TradingCore`.
- Strategies obtain connectors through the runner registry and rely on execution helpers (e.g. tracking orders) exposed by the connector layer.
- The redesigned smoke-test strategy (`ConnectorSmokeTestStrategy`) now anchors its limit prices to both the live order book and the connector-provided ticker, retries once with an aggressive price if an exchange rejects the initial quote, and logs bid/ask/last-price context for easier diagnosis.

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
- The unified `mm_bot/bin/runner.py` replaces the legacy `run_*.py` entrypoints.
- `mm_bot/runtime/builders.py` + `registry.py` resolve connectors and strategies declared in configuration files. Strategies can opt into dynamic connector resolution (e.g. `smoke_test` enumerates connectors listed under its configuration block).
- The builder now preserves explicit timeout/ retry settings from the config, wires smoke-test specific defaults (ticker-aware pricing, websocket bootstrap), and invokes optional `prepare_*` hooks so connectors can start their websocket feeds before strategy work begins.
- Legacy entrypoints are archived under `mm_bot/bin/legacy/` for reference.

## Connector Enhancements
- All connectors inherit common shutdown semantics via `TradingCore`, which now awaits async `stop`/`close` hooks to prevent lingering network sessions.
- `BackpackConnector` exposes `get_last_price`, de-duplicates order execution responses, and ensures websocket state is stopped before closing the underlying `aiohttp` session.
- Websocket bootstrap happens inside `prepare_smoke_test` so the first market snapshot reflects live data before we submit the diagnostic orders.

## Current Focus
- Smoke test infrastructure validates Backpack connectivity end-to-end with order retries and rich logging.
- Iterative improvements are being staged to support additional connectors (e.g. Lighter) without duplicating logic.

## Next Steps
1. Extend the smoke-test strategy and builders to cover the Lighter connector with the same retry/ticker safeguards.
2. Draft connector development documentation that explains required hooks, shared helpers, and testing expectations.
3. Begin onboarding the Aster exchange connector, reusing the shared `BaseConnector` instrumentation.

## Debug & Logging
- Enable detailed connector debug logs by setting `general.debug: true` or the `XTB_DEBUG=1` environment variable. When active, `BaseConnector` prints every order transition together with key metadata.
- Per-strategy telemetry/logging options remain configurable through the existing YAML files.
