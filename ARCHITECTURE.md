# XTradingBot Architecture (2025-09 update)

XTradingBot follows a three-layer architecture – **Strategy**, **Execution**, and **Connector** – orchestrated by the unified runner. Recent work introduced a production-ready liquidation-hedge strategy, richer diagnostics, and a telemetry stack to monitor multi-account deployments.

## 1. Strategy Layer

### Core Principles
- Strategies live in `mm_bot/strategy/*`, inherit from `StrategyBase`, and are lifecycle-managed by `TradingCore` (start/stop/on_tick).
- Each strategy receives named connectors from the runner registry and interacts with them through the execution helpers (`submit_market_order`, trackers, etc.).

### Production Strategy: `LiquidationHedgeStrategy`
- Location: `mm_bot/strategy/liquidation_hedge.py`.
- Purpose: run a three-venue liquidation hedge between Backpack and two Lighter accounts.
- Flow per cycle:
  1. **Backpack entry** – now uses market orders; COI capped at 2^32-1 to satisfy Backpack limits.
  2. **Lighter1 hedge** – submit market order, wait for position delta > 20% of the requested size, and log API key / nonce state.
  3. **Random wait** – uniform delay between `wait_min_secs` and `wait_max_secs` to stagger L2 entry.
  4. **Lighter2 entry** – same market + confirmation logic.
  5. **Backpack rebalance** – market order to drive `bp ≈ -(l1 + l2)`; failure triggers an emergency exit.
  6. **Monitoring** – heartbeat-style monitoring of all three venues; if any side returns to ~0 or timeout is reached we flatten and alert.
- Telemetry & notifications:
  - Reads `mm_bot/conf/telemetry.json` (base_url/token/group/bot names) to post heartbeats to the self-hosted dashboard.
  - Uses `tg_key.txt` to send Telegram messages on start/finish/emergency exit.
- Nonce/COI handling: Lighter uses 48-bit COIs + hard-refresh on `21104`; Backpack uses 32-bit COIs; all submissions are serialised per venue lock.

### Diagnostics: `LighterMinOrderStrategy`
- Added in `mm_bot/strategy/lighter_min_order.py`.
- Submits minimal market orders to lighter1 and lighter2, logging COI, API key, nonce state, and the raw response for troubleshooting (`21104`, partial fills, etc.).

### Other Strategies
- The smoke test (`ConnectorSmokeTestStrategy`) remains available for connection diagnostics.
- Legacy strategies (trend ladder, geometric grid, AS model) are still present in the repo.

## 2. Execution Layer
- Located in `mm_bot/execution/*`.
- `OrderTracker` keeps per-order state; `TrackingMarketOrder`/`TrackingLimitOrder` expose awaitable helpers.
- `tracking_limit.py` still powers the smoke test; production strategies now primarily use connector-provided `submit_market_order` wrappers with local serialisation, COI limits, nonce refresh, and detailed logging.

## 3. Connector Layer
- All connectors inherit from `BaseConnector` for consistent order tracking and event fan-out.
- **BackpackConnector** – `submit_market_order()` exposes REST market execution (fills include `executedQuantity`); COI limited to 2^32.
- **LighterConnector** – uses the official signer SDK; nonce manager switched to API mode; `get_positions()` provides `{sign, position}` so strategies can compute `sign * position`.
- Each connector exports helpers (`get_market_info`, `get_price_size_decimals`) so strategies can convert float amounts to integer units.

## Runner & Builders
- Unified runner: `mm_bot/bin/runner.py`.
- `mm_bot/runtime/builders.py`:
  - `build_liquidation_hedge_strategy` now requires connectors `backpack`, `lighter1`, `lighter2` and forwards telemetry / notification settings.
  - `build_lighter_min_order_strategy` accepts a list of targets (`connector`, `direction`, `reduce_only`, `size_multiplier`).
- Registry (`mm_bot/runtime/registry.py`) resolves connectors/strategies declared in configuration files.

## Telemetry & Dashboard
- Config: `mm_bot/conf/telemetry.json` (base URL, token, group, per-role bot names).
- Server: `deploy/server.js`
  - `POST /ingest/:botName` ingests heartbeats.
  - `GET /api/status` returns online/offline states.
  - `POST /admin/prune` removes stale bots (same timeout rule as `/api/status`), honouring `x-auth-token` when configured.
- Dashboard: `deploy/card.html`
  - Buttons for refresh / prune / set token.
  - Grouped cards for BP / L1 / L2, single-card fallback for legacy bots.
  - Auto-refresh every 2 seconds.

## Known Behaviour / Current Issues
- Backpack rebalance may still log "unable to reach target" when floating precision or venue limits prevent flattening to exact zero.
- If lighter2 position suddenly drops to zero while BP 调整仍在进行，监控线程会触发紧急退场（原因=`lighter2_zero`）。需要确认 Lighter 是否触发了 reduce-only 或风控。

## Debug Tips
- Set `general.debug: true` or `XTB_DEBUG=1` to enable detailed connector logs.
- `LiquidationHedgeStrategy` logs INFO/WARN for market submissions, nonce refreshes, position confirmations, telemetry posts, and Telegram notifications.

## Next Steps
1. Investigate Backpack market rebalance failures（检查成交量/最小单位/保证金限制）。
2. Enhance telemetry dashboard with execution warnings or alerts for heartbeat gaps。
3. Consider richer Telegram notifications (per-role PnL, heartbeat misses, etc.).
