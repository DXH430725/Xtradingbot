# Detail Architecture Reference

This document complements `ARCHITECTURE.md` with a drill-down of the main modules, their responsibilities, key functions, parameters, and internal dependencies. Use it as a quick lookup when extending or reviewing the codebase.

## Core Runtime

### `mm_bot/core/trading_core.py`
- **TradingCore**
  - `__init__(tick_size=1.0, debug=None, clock_factory=SimpleClock, logger=None)` – wires the execution clock, connector registry, and optional debug logging.
  - `add_connector(name, connector)` / `remove_connector(name)` – register or unregister connectors, invoking their lifecycle hooks.
  - `set_strategy(strategy)` – attach a strategy instance that implements `start`, `stop`, and `on_tick`.
  - `start()` – starts connectors, strategy, clock; clears `_stopped_event`.
  - `stop(cancel_orders=True)` – stops the clock/strategy, optionally cancels orders, runs connector shutdown, sets `_stopped_event`.
  - `shutdown(cancel_orders=True)` – calls `stop` then clears registries for a cold restart.
  - `wait_until_stopped()` – awaitable used by the runner to know when the strategy requested shutdown.
  - Dependencies: `mm_bot/core/clock.py.SimpleClock` for scheduling, strategy and connector interfaces for lifecycle hooks.

### `mm_bot/bin/runner.py`
- CLI entrypoint for all strategies.
- Key functions:
  - `_resolve_strategy_name(strategy_section, override)` – determines which strategy factory to load.
  - `_connector_configs(cfg, required)` – merges legacy and modern connector config sections.
  - `_build_core(general_cfg, strategy_name, debug_flag)` – instantiates `TradingCore` with tick size and debug mode.
  - `main(argv=None)` – parses CLI args, loads config, builds connectors (`registry.get_connector_spec`), prepares strategies, starts the core, waits on `core.wait_until_stopped()`, and exits with non-zero status if the strategy reported failure.
- Dependencies: `mm_bot/conf/config.load_config`, `mm_bot/runtime/registry`, `mm_bot/runtime/builders`.

## Execution Helpers

### `mm_bot/execution/orders.py`
- `OrderTracker` – state machine for individual orders; provides `.apply(update)`, `.wait_final(timeout)`, `.next_update(timeout)`, `.snapshot()`.
- `TrackingLimitOrder` / `TrackingMarketOrder` – lightweight wrappers that expose the tracker API to strategies/connectors.
- Enums/constants: `OrderState`, `FINAL_STATES`.

### `mm_bot/execution/tracking_limit.py`
- `place_tracking_limit_order(connector, symbol, base_amount_i, is_ask, interval_secs=10.0, timeout_secs=120.0, price_offset_ticks=0, cancel_wait_secs=2.0, post_only=False, reduce_only=0, logger=None)`
  - Re-posts a limit order at best bid/ask until `OrderState.FILLED`/`PARTIALLY_FILLED` or timeout.
  - Uses connector helpers: `submit_limit_order`/`place_limit`, `get_top_of_book`, `get_order_book`, `cancel_by_client_id` (optional), `cancel_all` fallback.
- `TrackingLimitTimeoutError` – raised when total timeout is exceeded.
- Internal helpers: `_get_price_size_decimals`, `_top_of_book`, `_submit_limit`, `_cancel_order`.

## Strategy Layer

### `mm_bot/strategy/smoke_test.py`
- **ConnectorSmokeTestStrategy** orchestrates connector diagnostics.
  - Constructor parameters: `connectors` (resolved connector map), `params` (`SmokeTestParams`).
  - `start(core)` – spawns a coroutine per connector and a monitor task.
  - `_exercise_connector(name, connector, cfg)` – calls `place_tracking_limit_order`, logs state transitions, submits reduce-only market exit, checks REST (`get_open_orders`) and websocket-cached positions, records success/failure.
  - `_cleanup_exposures()` – `cancel_all` + repeated market exits until positions are flat.
  - `_monitor_tests()` / `_finalize()` – gather results, stop the core, expose `overall_success`/`failure_reason` for the runner.
- Data classes:
  - `ConnectorTestConfig`: symbol, side, tracking interval/timeout, market exit timeout, price tick offsets, cancel wait.
  - `SmokeTestParams`: per-connector config map and pause between tests.
- Dependencies: `mm_bot/execution.tracking_limit`, `mm_bot/execution.orders`, connector REST/WS helper methods.

## Connector Layer Highlights

### `mm_bot/connector/base.py`
- Provides `BaseConnector` with order-state tracking (`_update_order_state`), listener registration, and event fan-out (`ConnectorEvent`).

### `mm_bot/connector/backpack/backpack_exchange.py`
- REST helpers: `get_market_info`, `get_top_of_book`, `submit_limit_order`, `submit_market_order`, `cancel_all`.
- WS handling updates `_positions_by_symbol` and order trackers.
- Dependencies: `aiohttp`, `websockets`, Ed25519 signing utilities.

### `mm_bot/connector/lighter/lighter_exchange.py`
- Maintains REST-derived market metadata (`_market_info_by_symbol`), websocket caches (orders/positions/trades), and signing flows for order submission.
- Key helpers for smoke test: `get_market_info`, `get_price_size_decimals`, `get_positions`, `submit_limit_order`, `submit_market_order`, `cancel_all`.
- Dependencies: Lighter SDK (`lighter-python`), rate limiter utilities.

### `mm_bot/connector/grvt/grvt_exchange.py`
- Integrates GRVT REST + websocket SDK, mirrors market metadata helpers, and surfaces submit/cancel APIs compatible with the execution layer tracking.
- Exposes `get_market_info`, `get_price_size_decimals`, `place_limit/market`, `submit_limit_order`, `submit_market_order`.
- Dependencies: `grvt-pysdk` (REST + WS clients), decimal handling for integer conversions.

## Configuration & Builders

- `mm_bot/conf/smoke_test*.yaml` – declarative configs for smoke tests. Each connector block supplies symbol, direction, tracking interval/timeout, market timeout, price offset, cancel wait.
- `mm_bot/runtime/builders.py`
  - `build_smoke_test_strategy(cfg, connectors, general)` – instantiates `ConnectorSmokeTestStrategy` with per-connector `ConnectorTestConfig` derived from configuration.
  - `prepare_smoke_test(connectors, strategy_cfg, general)` – starts websocket state streams before strategy execution with the relevant symbols.

## Operational Flow Summary
1. `runner.py` loads config, builds connectors via `builders.py`, and prepares websocket state.
2. `TradingCore.start()` begins connector/strategy lifecycles and ticks.
3. `ConnectorSmokeTestStrategy` launches a tracking-limit diagnostic task per connector and a monitor task.
4. Each connector task:
   - Resolves order sizing from `get_market_info`.
   - Calls `place_tracking_limit_order()` until filled or timeout.
   - Places a reduce-only market order to flatten the fill.
   - Cross-checks REST (`get_open_orders`) and websocket (`get_positions`) data to confirm no exposure remains and logs the order tracker history.
5. The monitor task aggregates results, runs a final cleanup (cancel all + flatten), and stops the core.
6. `runner.py` awaits `core.wait_until_stopped()` and exits with error context when the smoke test reports failure.

## Dependencies at a Glance
- External libraries: `aiohttp`, `websockets`, `nacl` (Backpack), `lighter-python`, `grvt-pysdk`, `asyncio` standard utilities.
- Internal utilities: `mm_bot/utils/throttler.py` for rate limiting, `mm_bot/execution/orders.py` for tracking, `mm_bot/logger/logger.py` for logging setup.

Keep this document updated when functions gain new parameters, modules are added, or dependencies change, so onboarding engineers can quickly navigate the codebase.
