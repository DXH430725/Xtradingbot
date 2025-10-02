> - Implemented fresh multi-layer framework under xbot/ (app/core/execution/connector/strategy/utils) with async order
  model, unified router, and tracking‑limit orchestration (execution/models.py, execution/order_service.py, execution/
  tracking_limit.py).
  - Added production-ready connectors for Backpack, Lighter, and GRVT using their official SDKs (connector/backpack.py,
  connector/lighter.py, connector/grvt.py, connector/factory.py); integer price/size handling, metadata caching, and
  idempotent COI flows included.
  - Introduced heartbeat publisher and lifecycle utilities (core/heartbeat.py, core/lifecycle.py), config loader updates
  (app/config.py), CLI runner with strategy wiring (app/main.py), and sample strategies (strategy/market.py, strategy/
  tracking_limit.py).
  - Documented usage and integration paths (README.md, docs/EXCHANGE_INTEGRATION.md, docs/HEARTBEAT.md, docs/
  STRATEGY_GUIDE.md) and added requirement pinning (requirements.txt).
  - Created async unit tests with reusable stubs covering order state, tracking limit behaviour, symbol mapping, and
  risk checks (tests/test_*, tests/stubs.py).

  Tests: pytest

  1. Populate venue-specific YAML/JSON configs (including symbol map + heartbeat endpoint) and verify end-to-end against
  sandbox keys.