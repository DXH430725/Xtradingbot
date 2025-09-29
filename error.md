## 2025-09-28 connector_test start_ws_state TypeError
- **Issue**: `connector_test` strategy attempted `start_ws_state([symbol])` on Lighter connector whose signature accepts no arguments, raising `TypeError: ... takes 1 positional argument but 2 were given`.
- **Fix**: Updated `ConnectorTestStrategy._prepare` to invoke `start_ws_state` with symbol list when supported and automatically fall back to no-arg call otherwise (`mm_bot/strategy/smoke_test.py`).
- **Status**: Resolved.
## 2025-09-28 connector_test unwind + WS bootstrap issues
- **Issue**: Connector test used `start_ws_state([symbol])` directly and ran `execution.unwind_all` across every connector, causing `TypeError` on connectors with zero-arg signatures and `ValueError: Market info unavailable for TEST:0` when unwinding unrelated venues.
- **Fix**: `_prepare` now adapts to connector signatures when starting websockets, and `ConnectorTestStrategy` tracks venues per canonical so unwind/cleanup only touches relevant connectors. `ExecutionLayer.unwind_all` accepts an optional venue list to scope emergency unwinds accordingly.
- **Status**: Resolved; connector_test runs without raising the previous exceptions.
## 2025-09-28 connector_test missing report state
- **Issue**: `_report` dictionary was not initialised before use, causing `AttributeError` during task execution.
- **Fix**: ConnectorTestStrategy now initialises `_report` and log directory in `__init__`, ensuring structured reporting is available for every task (`mm_bot/strategy/smoke_test.py`).
- **Status**: Resolved.
## 2025-09-28 connector_test reduce-only probe start_ws_state TypeError
- **Issue**: `connector_test` strategy attempted `start_ws_state([symbol])` on Lighter connector whose signature accepts no arguments, raising `TypeError: ... takes 1 positional argument but 2 were given`.
- **Fix**: `_prepare` now adapts to connector signatures when starting websockets, and `ConnectorTestStrategy` now tracks venues per canonical so unwind/cleanup only touches relevant connectors. `ExecutionLayer.unwind_all` accepts an optional venue list to scope emergency unwinds accordingly.
- **Status**: Resolved; connector_test runs without raising the previous exceptions.
## 2025-09-28 connector_test market-order probe bug
- **Issue**: reduce-only probe inside connector_test invokes `create_tracking_market_order` with `size_i`, but BaseConnector implementation did not accept that keyword, causing `TypeError` and crashing the strategy.
- **Fix**: extended `BaseConnector.create_tracking_market_order` to accept and store an optional `size_i` so strategies can stash intended size while reusing the tracking primitives (`mm_bot/connector/base.py`).
- **Status**: Resolved.
