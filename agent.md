Project: XTradingBot – Thin Router Architecture & Tracking Diagnostics

## Current Scope
- Maintain the redesigned 18-file production footprint: thin `ExecutionRouter`, 3 execution services, unified order model, and a single tracking-limit implementation.
- Keep connectors exchange-specific but aligned to `xbot.connector.interface.IConnector`; Backpack is live, Mock is the reference harness, Lighter/GRVT are pending stubs.
- Provide smoke-test coverage through the new strategy pipeline (Strategy → Router → Services → Connector) and ensure architecture guardrails (`validate_system.py`) keep LOC/file limits intact.

## Implemented Components
- **Execution Core (`xbot/execution/`)**
  - `router.py`: Stateless dispatcher wiring connectors into `MarketDataService`, `OrderService`, and `RiskService`.
  - `order_model.py`: Unified `Order`, `OrderEvent`, `Position`, and `OrderState` dataclasses with async final-state waiting and timeline analytics.
  - `services/market_data_service.py`: Combines symbol mapping, metadata caching, collateral/position aggregation.
  - `services/order_service.py`: Central order placement/cancel/reconcile logic; `tracking` path forces the sole implementation in `tracking_limit.py`.
  - `tracking_limit.py`: Only tracking-limit helper; re-posts at top-of-book, leverages `Order` model, handles cancel/retry/timeout.
- **Core Orchestrator (`xbot/core/trading_core.py`)**: Boots connectors, registers strategy tick handler (`SimpleClock`), exposes `add_connector`, `register_symbol`, graceful stop/shutdown, and bulk cancel helper.
- **Connectors (`xbot/connector/`)**
  - `interface.py`: Protocol + typed error hierarchy used by every connector.
  - `mock_connector.py`: Auto-fill simulation implementing `IConnector` (latency/error knobs) for CI and demos.
  - `backpack.py`: Production connector with Ed25519 signing, REST/WS mix, live position cache (handles `account.positionUpdate`, `orderFill`), and Partial/Filled state transitions documented in `previous.md`.
- **Strategy Layer (`xbot/strategy/smoke_test.py`)**
  - `SmokeTestStrategy`: Short-circuit diagnostics supporting `tracking_limit`, `limit_once`, `market`, and `full_cycle` flows; reports spreads, events, and order timelines.
- **App & Config (`xbot/app/`)**
  - `main.py`: Unified CLI (`python -m xbot.app.main` or `python run_xbot.py`) supporting direct CLI args or YAML configs.
  - `config.py`: Dataclass-backed loader (`SystemConfig`, `CLIConfig`) with helpers for keyfile resolution and default config discovery.
- **Tests & Tooling**
  - `test_xbot.py`: Async runner for smoke-test suites (`--quick` shortcut).
  - `validate_system.py`: Ensures architecture limits (file count, LOC caps, required modules) stay green.

## Essential Commands
- Direct CLI smoke test (mock): `python run_xbot.py --venue mock --symbol BTC --mode tracking_limit --side buy`
- YAML-config launch: `python run_xbot.py --config xbot.yaml`
- Quick regression: `python test_xbot.py --quick`
- Full smoke battery: `python test_xbot.py`
- Architecture lint: `python validate_system.py`

## Daily Closeout Checklist
1. Refresh architecture docs (`ARCHITECTURE.md`, `DETAIL_ARCHITECTURE.md`) to mirror structural updates.
2. Stage and push via cached credentials:
   ```bash
   git add ARCHITECTURE.md DETAIL_ARCHITECTURE.md <other touched files>
   git commit -m "update architecture docs"
   git push
   ```
3. Capture a hand-off note summarising changes plus remaining TODOs (e.g., pending venue integrations, router/service gaps).

## Implementation Notes
- The router/services avoid shared caches; symbol translation must go through `MarketDataService` (or connector helpers) to keep connectors stateless.
- Tracking-limit diagnostics rely on `connector.get_price_size_decimals/get_top_of_book`; ensure new venues implement the `IConnector` contract end-to-end.
- Use `ExecutionRouter.register_symbol()` to map canonical symbols (`BTC`) to venue formats (`BTC_USDC_PERP`), otherwise order/meta requests will fail on real venues.
- `BackpackConnector` already wires `account.positionUpdate` → `Position` cache and handles partial fills (`orderFill` events) as recorded in `previous.md`; maintain parity when porting other venues.
- Keep files ASCII-only unless an external API forces otherwise; do not run `git update` or revert user changes without explicit instruction.
