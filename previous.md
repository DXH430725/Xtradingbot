> Summary

  - Rebuilt ConnectorSmokeTestStrategy to drive tracking-limit diagnostics via the new execution helper, auto-flatten
  fills, cross-check REST/WS state, and stop the core once all connectors finish.
  - Added mm_bot/execution/tracking_limit.py with place_tracking_limit_order()/TrackingLimitTimeoutError, updated the
  execution package exports, and refreshed builder/config plumbing (per-connector YAML files) for tracking intervals/
  timeouts.
  - Extended connectors (Backpack already supported; Lighter/GRVT now expose cached market metadata) and tightened the
  runner/core to exit automatically when the smoke test completes.
  - Updated architecture documentation (ARCHITECTURE.md, new DETAIL_ARCHITECTURE.md) plus agent.md with the new workflow
  and daily closeout checklist; committed and pushed 316d6d7 to origin/main.

  Validation

  - python -m compileall mm_bot/strategy/smoke_test.py mm_bot/runtime/builders.py mm_bot/conf
  - python -m compileall mm_bot/bin/runner.py mm_bot/core/trading_core.py mm_bot/connector/lighter/lighter_exchange.py
  mm_bot/connector/grvt/grvt_exchange.py mm_bot/execution/tracking_limit.py

  Next Steps

  - Run the updated smoke tests (mm_bot/conf/smoke_test*.yaml) to confirm live behaviour and adjust connector-specific
  tracking parameters as needed.

  python mm_bot/bin/runner.py --config mm_bot/conf/smoke_test_backpack.yaml