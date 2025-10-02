# XTradingBot

A lightweight multi-venue execution framework focused on async Python 3.10+, implementing the architectural and behavioural requirements described in `totaldesign.md`. The framework currently supports Backpack, Lighter, and GRVT connectors together with a venue-agnostic execution layer, tracking‑limit orchestration, and strategy scaffolding.

## Features
- **Layered design**: `app/` CLI, `core/` lifecycle utilities, `execution/` services and models, `connector/` adapters, `strategy/` templates, `utils/` helpers.
- **Unified connector interface** with idempotent client order indices, integer price/size pipelines, and venue symbol mapping.
- **Tracking‑limit engine** implementing the mandated cancel‑then-replace loop with partial fill accounting and timeout handling.
- **Risk controls** covering minimum size, exposure caps, and quote reference lookups.
- **Heartbeat service** periodically reports state to a configurable HTTP endpoint.
- **Async friendly tests** for the order model, tracking limit engine, symbol mapping, and risk service logic.

## Getting Started

### Install dependencies
```bash
pip install -r requirements.txt
```
The connectors rely on official venue SDKs (`bpx-py`, `lighter-sdk`, `grvt-pysdk`). Each requires valid API credentials; sample formats are provided in the repository root (`Backpack_key.txt`, `Lighter_key.txt`, `Grvt_key.txt`).

### Running a strategy
```bash
python -m app.main \
  --venue backpack \
  --symbol SOL_USDC \
  --mode tracking_limit \
  --qty 0.1 \
  --side buy \
  --price-offset-ticks -1
```
CLI parameters override any optional YAML/JSON configuration passed via `--config`. The strategy runs until completion (for `tracking_limit`, it opens via the tracking loop and closes with a market order).

### Heartbeat
Activate the heartbeat publisher by supplying a config file, e.g.
```yaml
venue: backpack
symbol: SOL_USDC
qty: 0.1
mode: tracking_limit
heartbeat:
  url: "https://heartbeat.local/ping"
  interval_secs: 15
  timeout_secs: 3
  token: "super-secret"
```
The runner merges CLI arguments with the file, so `--symbol` or `--qty` continue to override.

### Tests
```bash
pytest
```
The test suite relies on pure in-memory stubs; no live network calls are issued.

## Directory Layout
```
app/         CLI entrypoint and configuration loader
connector/   Venue adapters (Backpack, Lighter, GRVT)
core/        Lifecycle, clock, heartbeat utilities
execution/   Market data, orders, risk, tracking-limit logic
strategy/    Base and example strategies
utils/       Logging helpers and id generators
tests/       Async unit tests exercising core components
```

## Known Gaps / TODOs
- Full websocket streaming and reconciliation loops are pending; current connectors rely on synchronous SDK calls.
- Additional risk parameters (quote value limits, margin checks) can be wired in via `RiskLimits`.
- Production hardening (retry/backoff, connector-specific error typing) should be added before live trading.

Refer to the documents under `docs/` for exchange extension guidelines, heartbeat payload details, and strategy integration examples.
