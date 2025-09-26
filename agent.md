Project: XTradingBot – Unified Connector Diagnostics & Strategy Sandbox

## Current Scope
- Maintain a lightweight, exchange-agnostic trading core without Hummingbot dependencies.
- Provide reusable execution helpers (tracking limit, order tracking) that production strategies and diagnostics can share.
- Keep strategy implementations event-driven, explicitly lifecycle-managed, and straightforward to test.

## Implemented Components
- **Core (`mm_bot/core/trading_core.py`)**: Async lifecycle wrapper with stop/shutdown semantics and a `wait_until_stopped()` signal consumed by the runner.
- **Runner (`mm_bot/bin/runner.py`)**: Unified CLI that resolves connectors/strategies via the registry, prepares websocket state, and exits non-zero when strategies report failure.
- **Execution Layer (`mm_bot/execution/`)**:
  - `orders.py`: `OrderTracker`, `TrackingLimitOrder`, `TrackingMarketOrder`.
  - `tracking_limit.py`: `place_tracking_limit_order()` helper + `TrackingLimitTimeoutError` for top-of-book chasing.
- **Connectors**: Backpack, Lighter, and GRVT adapters unified under `BaseConnector`, each exposing `get_market_info`, `get_price_size_decimals`, and REST/WS reconciliation helpers.
- **Diagnostics**: `ConnectorSmokeTestStrategy` issues tracking-limit diagnostics, verifies REST vs WS state, flattens fills with reduce-only market orders, and asserts the order-state machine is consistent.
- **Configs**: `mm_bot/conf/smoke_test*.yaml` provide per-connector tracking parameters (interval, timeout, cancel wait, price offsets).

## Essential Commands
- Run full multi-connector smoke test:
  - `python mm_bot/bin/runner.py --config mm_bot/conf/smoke_test.yaml`
- Run per-connector diagnostics:
  - Backpack: `python mm_bot/bin/runner.py --config mm_bot/conf/smoke_test_backpack.yaml`
  - Lighter: `python mm_bot/bin/runner.py --config mm_bot/conf/smoke_test_lighter.yaml`
  - GRVT: `python mm_bot/bin/runner.py --config mm_bot/conf/smoke_test_grvt.yaml`

## Daily Closeout Procedure
Whenever you finish the day’s tasks:
1. Refresh `ARCHITECTURE.md` and `DETAIL_ARCHITECTURE.md` with any structural/code changes made during the session.
2. Stage and push updates using the cached GitHub credentials:
   ```bash
   git add ARCHITECTURE.md DETAIL_ARCHITECTURE.md <other touched files>
   git commit -m "update architecture docs"
   git push
   ```
3. Leave a short summary in the hand-off note describing the changes and remaining open items.

## Conventions & Notes
- Keep modules independent of Hummingbot and minimise third-party dependencies.
- Strategies implement `start(core)`, `stop()`, `async on_tick(now_ms)`; connectors expose `start(core)`, `stop(core)`, optional async `cancel_all`.
- Enable verbose connector logs via `general.debug: true` or `XTB_DEBUG=1`.
- Tracking limit diagnostics assume connectors implement `get_market_info`, `get_price_size_decimals`, `submit_limit_order`/`submit_market_order`, and `get_positions`.
- Maintain small, composable modules; prefer dependency injection to keep components testable.
- 除非用户明确要求，严禁使用 `git update` 从本地状态回滚或覆盖仓库内容。
