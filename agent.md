Project: Simplified Single-Exchange Market-Making Bot

Scope and Goals
- Build a minimal, independent trading core (no Hummingbot dependency).
- Single exchange, single strategy (simple market making) to start.
- Event-driven updates; clear lifecycle controls; easy to test.
- Debug logging available but gated to avoid noisy runtime.

Architecture (target, from note.txt)
- mm_bot/
  - bin/ (startup scripts) — planned
  - conf/ (config, logging) — planned
  - core/
    - clock.py (tick scheduler) — implemented
    - event_bus.py (pub/sub) — planned
    - data_type.py (orders, trades) — planned
    - trading_core.py (main controller) — implemented
  - connector/
    - lighter/ (one exchange adapter; REST+WS+auth) — planned
  - strategy/
    - strategy_base.py — planned
    - market_making.py — planned
  - logger/
    - logger.py — planned (use std logging for now)
  - test/
    - unit tests for core/strategy/connector — started (core)
  - utils/
    - throttler.py — planned

What’s Implemented Now
- Core: mm_bot/core/trading_core.py
  - Uses SimpleClock from mm_bot/core/clock.py (async tick loop; pluggable via factory)
  - TradingCore lifecycle: start/stop/shutdown/status
  - Strategy integration: start/stop/on_tick
  - Connector integration: start/stop/cancel_all
  - Debug switch (constructor arg or env XTB_DEBUG). Helper dbg() only logs when enabled.
- Core: mm_bot/core/clock.py
  - Extracted SimpleClock into its own module
- Tests
  - test/test_simple_trading_core.py (3 passing)
  - mm_bot/test/test_clock.py (unit test for SimpleClock)
- Connector (lighter)
  - Added intent/result logging in place_limit() for COI/market/account context
  - Added is_coi_open() helper to verify presence in REST active orders by COI
- Strategy (Trend Ladder)
  - Added TP submit intent log (entry/target, post-only, reduce-only, current bid/ask)
  - Added delayed verification log: WS presence, REST presence, and WS status histogram for the COI
- Diagnostics
  - mm_bot/bin/diagnose_tp_mismatch.py now always closes client session on exit
  - mm_bot/bin/diagnose_tp_side_vs_ro.py compares side-matched vs reduce-only counts, and lists COIs that are side-matched but not reduce-only
- Tests: test/test_simple_trading_core.py
  - Validates ticks fire, lifecycle calls, order cancellation on stop, debug flag and status
  - 3 tests currently passing
- Report: reports/trading_core_analysis.md
  - Analysis of Hummingbot’s original trading_core.py for reference only.

How to Run Tests
- Core-only: `python -m pytest -q test\test_simple_trading_core.py`
- Clock test: `pytest -q mm_bot/test/test_clock.py`

Diagnostics and Ops
- Check WS vs REST opens for current symbol:
  - `python mm_bot/bin/diagnose_tp_mismatch.py`
- Compare “same-side” vs “reduce-only same-side” orders (helps explain coverage logs):
  - `python mm_bot/bin/diagnose_tp_side_vs_ro.py`
- Trend Ladder TP path logs you can search for:
  - `tp submit intent:` (shows entry/target and top-of-book)
  - `tp placed:` (COI assigned)
  - `tp verify:` (ws_open/rest_open and WS status histogram)
  - Connector logs around TP submission:
    - `place_limit submit:` (sym/mid/idx/cio/po/ro/price_i/base_i)
    - `place_limit result:` (tx_hash/err)

Conventions
- Keep modules independent of Hummingbot; only standard lib and our code.
- Minimal interfaces:
  - Strategy: `start(core)`, `stop()`, `async on_tick(now_ms)`
  - Connector: `start(core)`, `stop(core)`, optional `async cancel_all(timeout)`
- Debug logging: prefer `core.dbg("message")`; INFO-level for main lifecycle only.

Immediate Next Steps (TODO)
- Add `event_bus.py` and minimal `data_type.py` (Order/Fill structures).
- Scaffold `strategy/strategy_base.py` and `strategy/market_making.py` with simple quoting loop.
- Add config skeleton in `conf/` and refine bin runners.
- Extend tests to strategy and connector fakes; add error-path tests.

Notes for Future Work Sessions
- Maintain small, composable modules; keep runtime dependencies minimal.
- Keep debug logs guarded; expand only around lifecycle transitions and IO.
- Prefer DI (pass factories) to make components testable.
