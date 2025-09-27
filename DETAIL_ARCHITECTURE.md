# Detailed Architecture Reference (2025-09)

This reference complements `ARCHITECTURE.md` with per-module responsibilities, function lists, and runtime dependencies. It reflects the latest tri-venue hedge workflow, telemetry stack, and diagnostics tooling.

## Core Runtime

### `mm_bot/core/trading_core.py`
- **TradingCore**
  - `__init__(tick_size=1.0, debug=None, clock_factory=SimpleClock, logger=None)` – build execution clock, register connector/strategy containers.
  - `add_connector(name, connector)` / `remove_connector(name)` – manage connector lifecycle hooks.
  - `set_strategy(strategy)` – attach strategy instance implementing `start`, `stop`, `on_tick`.
  - `start()` – start connectors + strategy + clock; clears `_stopped_event`.
  - `stop(cancel_orders=True)` / `shutdown(cancel_orders=True)` – stop components, optionally cancel orders, release resources.
  - `wait_until_stopped()` – await strategy-driven shutdown.
- Dependencies: `mm_bot/core/clock.SimpleClock`, strategy/connector interfaces, asyncio.

### `mm_bot/bin/runner.py`
- CLI entry point; loads configuration, resolves connectors/strategies via registry, prepares connectors if required (e.g. smoke-test), builds `TradingCore`, starts it, and waits for shutdown.
- Key helpers: `_resolve_strategy_name`, `_connector_configs`, `_build_core`.

## Execution Helpers

### `mm_bot/execution/orders.py`
- `OrderTracker` implements order-state machine (`NEW → SUBMITTING → ... → FILLED/CANCELLED/FAILED`).
- `TrackingMarketOrder` / `TrackingLimitOrder` wrap trackers and expose async helpers (`wait_final`, `next_update`, `snapshot`).

### `mm_bot/execution/tracking_limit.py`
- `place_tracking_limit_order()` – chasing helper used by smoke-test diagnostics.
- `TrackingLimitTimeoutError` – timeout exception when the order fails to fill within the configured window.

### Market-order wrappers
- Strategies now mostly interact with connector-level `submit_market_order()` (Backpack, Lighter) but add local wrappers (`_submit_backpack_market`, `_submit_lighter_market`) for COI capping, serialisation, nonce refresh, and retry.

## Strategy Layer

### `mm_bot/strategy/liquidation_hedge.py`
- **LiquidationHedgeStrategy(backpack_connector, lighter1_connector, lighter2_connector, params)**
  - Parameters (`LiquidationHedgeParams`): symbols (`backpack_symbol`, `lighter_symbol`), leverage, direction；随机等待 (`wait_min_secs`, `wait_max_secs`)，重试与滑点配置（Backpack/Lighter），心跳（`telemetry_enabled`, `telemetry_config_path`, `telemetry_interval_secs`），Telegram (`telegram_enabled`, `telegram_keys_path`)。
  - `_execute_cycle()` – orchestrates Backpack 市价建仓 → lighter1 市价对冲 → 随机等待 → lighter2 入场 → Backpack 市价再平衡 → 监控。
  - `_submit_backpack_market()` – Backpack 市价封装：COI 上限 2^32-1、串行锁、失败重试，返回 `TrackingMarketOrder`。
  - `_submit_lighter_market()` – lighter 市价封装：COI 2^48-1、记录 API key/nonce，命中 `21104` 时异步 `hard_refresh_nonce`。
  - `_confirm_position()` – 基于 baseline 和 expected_sign 判断仓位变化（阈值 = size × 20%），输出详细日志。
  - `_rebalance_backpack()` – 市价调整 BP 至 `-(L1+L2)`，失败后触发 `_emergency_exit`。
  - Telemetry：`_load_telemetry_config` → `_telemetry_loop`（调用 `_collect_telemetry_payloads` 构造 `{role:'BP/L1/L2', position_base, balance_total, latency}` 并 `POST /ingest/:botName`）。
  - Telegram：`_notify_start/_notify_finished/_emergency_exit` 通过 `sendMessage` 推送状态。
  - `_emergency_exit()` – 平掉所有 venue，记录 PnL 并通知。

### `mm_bot/strategy/lighter_min_order.py`
- 诊断策略；`LighterMinOrderParams` 支持多目标 `targets`（connector、direction、reduce_only、size_multiplier）。
- `_fire_order()` – 生成 COI，打印 COI/API key/nonce，提交市价单并等待；若超过尝试次数仍失败，记录原因。
- `_nonce_snapshot` / `_refresh_nonce` – 直接访问 signer 的 nonce manager 以调试 `21104`。

### `mm_bot/strategy/smoke_test.py`
- 仍保留 `ConnectorSmokeTestStrategy`（限价+reduce-only 市价诊断）。与 liquidation hedge 并行维护，无需改动。

## Connector Layer

### `mm_bot/connector/backpack/backpack_exchange.py`
- REST helpers：`submit_market_order`、`submit_limit_order`、`cancel_all`。
- 市价单返回 `executedQuantity/ExecutedQuoteQuantity`，策略用于 `_resolve_market_filled`。
- COI 必须在 uint32 范围；策略按 venue 区分 COI 上限。

### `mm_bot/connector/lighter/lighter_exchange.py`
- 采用 lighter 官方 signer SDK。
- `submit_market_order` 支持 `max_slippage`；策略记录 `nonce`/`api_key_index`。
- `get_positions()` 返回 `{sign, position}`；策略乘积得到有符号仓位。
- `nonce_manager` 采用 API 模式，`hard_refresh_nonce` 用于 `21104` 的恢复。

### `mm_bot/connector/grvt/grvt_exchange.py`
- 维持与旧版一致：REST/WS SDK + BaseConnector 事件分发。

## Telemetry & Dashboard

### Config: `mm_bot/conf/telemetry.json`
- `base_url` – heartbeat server 地址。
- `token` – 可选鉴权 token，对应 HTTP `x-auth-token`。
- `group` – 对冲组标识（用于 UI 分组）。
- `bots` – 各角色的默认 bot name（BP/L1/L2）。

### `deploy/server.js`
- `POST /ingest/:botName` – 保存最近心跳，写入内存 Map。
- `GET /api/status` – 返回 `{now, bots:[{name, online, last_update_ms_ago, payload}]}`。
- `POST /admin/prune?older_ms=` – 清理不活跃 bot（沿用 `/api/status` 的 2×interval 规则）。

### `deploy/card.html`
- 顶部按钮：刷新 / 清理离线 / 设置Token。
- 支持按 `payload.group` & `payload.role (BP/L1/L2)` 分组展示，未分组 fallback 为原单卡。
- 2 秒自动刷新，Token 存储在 `sessionStorage`。

## Builders & Configs

- `mm_bot/runtime/builders.py`
  - `build_liquidation_hedge_strategy` – 解析三 connector 与所有参数（telemetry、telegram、重试…）。
  - `build_lighter_min_order_strategy` – 支持多个 Lighter 目标；自动收集所有名称以构建 connectors。
- `mm_bot/conf/liquidation_hedge.yaml`
  - 现在包含 telemetry/telegram 配置项。
- `mm_bot/conf/lighter_min_order.yaml`
  - 示例：lighter1 买、lighter2 卖最小量，并记录调试信息。

## Server Diagnostics & Scripts

- Quick scripts（位于根目录）可直接构造 connector:
  - 抓取仓位：`tmp_show_positions.py` – 验证 `{sign, position}` 组合。
  - Backpack 市价测试示例：
    ```python
    async def send_order():
        conn = build_backpack_connector(...)
        tracker = await conn.submit_market_order(symbol="ETH_USDC_PERP", client_order_index=123456, base_amount=1, is_ask=False, reduce_only=0)
        await tracker.wait_final(timeout=30)
        print(tracker.state, tracker.snapshot().info)
    ```

## Current Caveats / Investigation Items
- Backpack 市价再平衡仍可能失败（即使 lighter1/lighter2 对冲成功），需检查实际成交量/最小单位/余额限制。
- lighter2 监控过程中仓位归零会触发紧急退出，需要确认是否由 Lighter 风控自动平仓。

## Debug & Logging
- `general.debug: true` 或 `XTB_DEBUG=1` 开启 connectors 的详细日志。
- `LiquidationHedgeStrategy` 在市价提交、nonce 刷新、仓位确认、心跳发送、Telegram 推送时均会打印 INFO/WARN 日志。

