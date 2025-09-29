# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

XTradingBot is a unified, exchange-agnostic trading bot system that provides connector diagnostics and strategy sandbox functionality. The project is designed to be lightweight and independent from Hummingbot, with a focus on reusable execution helpers and event-driven strategies.

## Architecture

### Core Components

- **Trading Core (`mm_bot/core/trading_core.py`)**: Async lifecycle wrapper providing start/stop/shutdown semantics and event management
- **Runner (`mm_bot/bin/runner.py`)**: Unified CLI that resolves connectors/strategies via registry, manages websocket state, and handles exits
- **Execution Layer (`mm_bot/execution/`)**:
  - `orders.py`: `OrderTracker`, `TrackingLimitOrder`, `TrackingMarketOrder` implementations
  - `tracking_limit.py`: `place_tracking_limit_order()` helper with timeout handling for top-of-book chasing
- **Connectors (`mm_bot/connector/`)**: Exchange adapters for Backpack, Lighter, and GRVT unified under `BaseConnector`
  - Each exposes: `get_market_info`, `get_price_size_decimals`, REST/WS reconciliation helpers
  - Key files: `base.py` (base connector), individual connector implementations
- **Strategies (`mm_bot/strategy/`)**: Event-driven strategy implementations including diagnostics and hedge strategies
  - `smoke_test.py`: `ConnectorSmokeTestStrategy` for connector diagnostics
  - `liquidation_hedge.py`: Liquidation hedge strategy
  - `simple_bp_lighter_hedge.py`: Simple hedge between Backpack and Lighter

### Key Dependencies

The project uses several exchange-specific SDKs as submodules:
- `grvt-pysdk/`: GRVT Python SDK with dependencies like `eth-account`, `websockets`, `aiohttp`
- `lighter-python/`: Lighter exchange SDK with similar websocket/HTTP dependencies
- Main bot uses minimal dependencies focused on async operations and logging

### Configuration System

Configuration files in `mm_bot/conf/`:
- `connector_test.yaml`: Connector diagnostic configuration
- `liquidation_hedge.yaml`: Liquidation hedge strategy configuration
- `simple_hedge.yaml`: Simple hedge strategy configuration
- Configs define connectors (base_url, ws_url, keys_file, broker_id) and strategy parameters

## Development Commands

### Running Strategies

Main entry point via runner:
```bash
python mm_bot/bin/runner.py --config mm_bot/conf/smoke_test.yaml
```

Per-connector diagnostics:
```bash
# Backpack diagnostics
python mm_bot/bin/runner.py --config mm_bot/conf/smoke_test_backpack.yaml

# Lighter diagnostics
python mm_bot/bin/runner.py --config mm_bot/conf/smoke_test_lighter.yaml

# GRVT diagnostics
python mm_bot/bin/runner.py --config mm_bot/conf/smoke_test_grvt.yaml
```

Strategy execution:
```bash
# Connector testing
python mm_bot/bin/runner.py --config mm_bot/conf/connector_test.yaml

# Liquidation hedge
python mm_bot/bin/runner.py --config mm_bot/conf/liquidation_hedge.yaml
```

### SDK Development

For GRVT SDK (using uv):
```bash
cd grvt-pysdk/
uv run pytest tests --cov=src    # Run tests
uv run ruff check .              # Lint
uv run ruff format .             # Format
uv run mypy .                    # Type check
uv sync --all-extras --dev       # Install dependencies
```

For Lighter SDK (using Poetry):
```bash
cd lighter-python/
poetry install                   # Install dependencies
pytest                          # Run tests
flake8                          # Lint
mypy lighter                    # Type check
```

### Debugging

Enable verbose logging:
```bash
# Via config
general.debug: true

# Via environment
XTB_DEBUG=1 python mm_bot/bin/runner.py --config <config>
```

## Strategy Development Guidelines

### Strategy Interface

All strategies must implement:
- `start(core)`: Initialize strategy with trading core
- `stop()`: Clean shutdown
- `async on_tick(now_ms)`: Main event loop handler

### Connector Interface

All connectors implement `BaseConnector` and must provide:
- `get_market_info()`, `get_price_size_decimals()`: Market data helpers
- `submit_limit_order()`, `submit_market_order()`: Order placement
- `get_positions()`: Position information
- `start(core)`, `stop(core)`: Lifecycle management
- Optional: `async cancel_all()`: Emergency position flattening

### Tracking Orders

Use execution layer helpers for order management:
```python
from mm_bot.execution.tracking_limit import place_tracking_limit_order
from mm_bot.execution.orders import OrderTracker

# Place tracking limit order with timeout
order = await place_tracking_limit_order(connector, symbol, side, size, price, timeout_secs)

# Track order states
tracker = OrderTracker()
```

## Development Workflow

### Daily Development Cycle

1. Work on features/fixes in the codebase
2. Test using appropriate connector diagnostics
3. Update architecture documentation if structural changes made
4. Commit and push changes:
   ```bash
   git add <modified files>
   git commit -m "<description>"
   git push
   ```

### Key Files to Understand

- `mm_bot/bin/runner.py`: Main entrypoint and CLI parsing
- `mm_bot/core/trading_core.py`: Core async lifecycle management
- `mm_bot/connector/base.py`: Base connector interface
- `mm_bot/strategy/smoke_test.py`: Connector diagnostic implementation
- `mm_bot/execution/tracking_limit.py`: Order tracking utilities

## Architecture Principles

- **Exchange-agnostic**: Connectors abstract exchange-specific details
- **Event-driven**: Strategies respond to tick events rather than polling
- **Lifecycle-managed**: Clear start/stop semantics for all components
- **Testable**: Dependency injection enables isolated component testing
- **Minimal dependencies**: Avoid heavy frameworks, focus on core functionality
- **Composable modules**: Small, focused components that can be combined flexibly