# XTradingBot 代码结构与依赖全景报告

> 生成时间：2025-09-22 23:17:27
> 仓库根目录：`/mnt/d/DXHAUTO/XTradingBot`

## 1. 项目概览
- **目标**：为 Backpack、Lighter、Grvt 等交易所提供统一的做市/套利策略执行框架，核心代码位于 `mm_bot` 目录。
- **组成**：Python 主体 (`mm_bot`)、部署脚本 (`deploy`)、第三方 SDK (`lighter-python`、`lighter-go`、`grvt-pysdk`) 以及若干密钥、文档和实验想法。
- **运行方式**：通过 `mm_bot/bin` 下的 Async 入口脚本加载配置、启动 `TradingCore`，注册所需的连接器和策略，运行事件循环。
- **数据依赖**：
  - 本地密钥文件（如 `Backpack_key.txt`、`Lighter_key.txt`）。
  - 配置文件（YAML/JSON），环境变量覆盖关键运行参数（如 `XTB_CONFIG`、`XTB_BACKPACK_BASE_URL`）。
  - 第三方 REST/WebSocket API：Backpack、Lighter、Grvt。

## 2. 顶层目录结构（节选）
- `mm_bot/`：核心交易框架、策略、连接器、配置、工具、测试。
- `deploy/`：Node.js + Python 组合的部署/遥测工具（`server.js`、`telemetry_receiver.py`）。
- `lighter-python/`：自动生成的 Lighter 交易所 Python SDK（OpenAPI 客户端、数据模型、交易签名器）。
- `lighter-go/`：Go 语言 SDK，用于签名与交易类型定义。
- `grvt-pysdk/`：Grvt 交易所 Python SDK 与测试用例。
- `lighter-tool/`：环境初始化脚本。
- `build/`：跨平台 signer 构建脚本。
- `papers/`、`点子/`：研究论文、策略想法文档。
- 根目录散布若干密钥、历史报告、命令记录（如 `CODE_ARCH_REPORT.md`、`comand.txt`）。

> 注：`deploy/node_modules`、`lighter-python`、`grvt-pysdk` 等目录包含大量自动生成或第三方库文件，未在本文逐个列出函数，视为外部依赖。

## 3. mm_bot 子项目详解

### 3.1 核心运行时 (`mm_bot/core`)
- `trading_core.py`
  - **功能**：管理单策略事件循环，负责连接器生命周期、时钟调度、订单取消、状态查询。
  - **主要类/函数**：
    - `TradingCore.__init__(tick_size, debug, clock_factory, logger)`：配置 tick 间隔、日志、调度器。
    - `add_connector(name, connector)` / `remove_connector(name)`：注册/移除连接器实例。
    - `set_strategy(strategy)`：挂载策略对象。
    - `async start()` / `async stop(cancel_orders)` / `async shutdown(cancel_orders)`：启动/停止时钟、策略、连接器。
    - `status()`：返回运行状态信息（是否在跑、连接器列表、运行时长等）。
  - **数据依赖**：连接器需实现 `start/stop/cancel_all`；策略需实现 `start(core)`、`stop()`、`on_tick(now_ms)`。

- `clock.py`
  - **功能**：基于 `asyncio` 的简单时钟，周期性调用注册的异步处理器。
  - **主要类/函数**：
    - `SimpleClock.__init__(tick_size, logger)`：设定 tick 频率。
    - `add_tick_handler(handler)`：注册异步回调。
    - `_run()`、`start()`、`async stop()`：内部循环、启动、停止。
  - **数据依赖**：依赖 `asyncio.create_task` 创建后台任务。

### 3.2 日志系统 (`mm_bot/logger`)
- `logger.py`
  - `default_logging_dict(log_dir)`：根据环境变量生成 console + rotating file 配置。
  - `setup_logging(config_path)`：优先加载 YAML/JSON 配置，否则回退默认配置；依赖 `logging.config.dictConfig`。

### 3.3 配置管理 (`mm_bot/conf`)
- `config.py`
  - `load_config(path=None)`：按优先级加载 YAML/JSON 配置；支持环境变量 `XTB_CONFIG`。返回 `Dict[str, Any]`。
- `*.yaml`：策略示例配置（`backpack_perp_mm.yaml`、`cross_arb.yaml` 等），定义 general、connector、strategy 参数。

### 3.4 工具 (`mm_bot/utils`)
- `throttler.py`
  - `lighter_default_weights()`：返回 Lighter API 限流权重字典。
  - `RateLimiter`
    - `__init__(capacity_per_minute, weights, burst)`：令牌桶限流器。
    - `_refill()`：按时间补充令牌。
    - `async acquire(endpoint_key)`：异步等待令牌，控制请求速率。

### 3.5 连接器 (`mm_bot/connector`)
- `interfaces.py`
  - 定义 `IConnector` 协议，约束交易连接器必须实现的生命周期、行情、下单、撤单、账户查询、延迟测试等接口。

- `backpack/backpack_exchange.py`
  - **配置与生命周期**：`BackpackConfig` 数据类持有 API 地址、WS 地址、限流参数；`BackpackConnector.start()` 读取密钥文件、初始化 `aiohttp.ClientSession`、签名器。
  - **主要函数/方法**：
    - `load_backpack_keys(path)`：解析本地密钥（API key/secret）。
    - API 辅助函数：`_ensure_markets()`、`list_symbols()`、`get_market_id()`、`get_top_of_book()`、`get_price_size_decimals()`。
    - 签名：`_auth_headers(instruction, params)`。
    - 交易：`place_limit()`、`place_market()`、`cancel_all()`、`cancel_by_client_id()`、`cancel_order()`。
    - 账户：`get_account_overview()`、`get_positions()`。
    - WS 状态：`start_ws_state(symbols)`、`_ws_run()`（监听 fills/positions）。
  - **依赖**：`aiohttp`、`websockets`、`nacl.signing`、自研 `RateLimiter`。

- `lighter/lighter_exchange.py`
  - **功能**：通过官方 `lighter-python` SDK 处理 REST/WS，并包装关键交易接口。
  - **主要函数/方法**：
    - `LighterConfig`、`LighterConnector.start()`：加载密钥、初始化 SDK (`lighter.ApiClient`、`AccountApi` 等)。
    - `_ensure_account_index()`、`_ensure_signer()`、`_ensure_markets()`：延迟加载账号、签名器、市场映射。
    - `set_event_handlers(...)`：注册订单、成交、仓位回调。
    - `async start_ws_state(symbols)` / `async stop_ws_state()`：启动账户/行情 WebSocket。
    - 交易接口：`place_limit()`、`place_market()`、`cancel_all()`、`cancel_by_client_order_index()`、`cancel_order()`。
    - 行情/账户：`get_top_of_book()`、`get_order_book()`、`get_positions()`、`get_open_orders()`。
    - 限流：复用 `RateLimiter` 控制 API 调用。
  - **依赖**：`lighter-python` 模块、`eth_account`、`websockets`、`asyncio`。

- `lighter/lighter_auth.py`
  - `load_keys_from_file(path)`：解析 Lighter 本地密钥文件，返回 `(api_key_index, api_private, api_public, eth_private)`。

- `lighter/lighter_ws.py`
  - 封装 Lighter WebSocket 行程：`connect_private_ws()`、`_handle_private_message()`，维护订单、仓位缓存。

- `grvt/grvt_exchange.py`
  - 与 Grvt 交易所交互的占位实现，暴露 `GrvtConfig`、`GrvtConnector`，主要对接 `grvt-pysdk`。

### 3.6 策略 (`mm_bot/strategy`)
- `strategy_base.py`
  - 定义抽象基类 `StrategyBase`，约束 `start(core)`、`stop()`、`async on_tick(now_ms)`。

- `backpack_perp_market_maker.py`
  - `PerpMarketMakerParams`：做市参数（symbol、spread、订单数量、仓位限制等）。
  - `BackpackPerpMarketMakerStrategy`
    - 生命周期：`start(core)` 注册 Backpack 事件；`stop()` 取消回调。
    - 主循环：`async on_tick(now_ms)` -> `_run_cycle()`。
    - 初始化：`_ensure_initialized()` 拉取市场信息、设置精度。
    - 逻辑：`_get_net_position()`、`_manage_position()`、`_calculate_prices()`、`_resolve_order_quantity()`、`_cancel_existing()`、`_place_side_orders()`。
    - 数据缓存：持有最新仓位、下单计数器、成交统计。

- `cross_market_arbitrage.py`
  - `CrossArbParams`：跨市场套利参数（价差阈值、持仓上限、止盈/止损、冷却时间等）。
  - `CrossMarketArbitrageStrategy`
    - `start(core)`：创建 `OrderTracker`, `TrackingLimitExecutor`，注册 Backpack/Lighter 回调。
    - `async on_tick(now_ms)`：节流执行 `_tick_loop()`（包含 `_discover_pairs()`、`_check_spread()`、`_enter_position()`、`_monitor_positions()`）。
    - 订单回调：`_on_backpack_order_event`、`_on_lighter_order_event`、`_on_*_trade`、`_on_*_position`。
    - 撤单/风控：`_cancel_backpack_order()`、`_cancel_lighter_order()`、`_flatten_all_positions()`。
    - 依赖子模块：`arb/order_exec.py`、`arb/pairing.py`。

- `geometric_grid.py`
  - `GeometricGridParams` / `GeometricGridStrategy`：使用几何级数生成网格价格，依赖 Lighter/Grvt 连接器，对接 `TradingCore`。

- `hedge_ladder.py`
  - `HedgeLadderParams` / `HedgeLadderStrategy`：多层级对冲策略，支持多腿组合、数量补偿。

- `trend_ladder.py`
  - `TrendLadderStrategy`：基于趋势的阶梯单策略，包含 `_analyze_trend()`、`_adjust_orders()`。

- `as_model.py`
  - `ASParams`：Avellaneda-Stoikov 模型参数。
  - `AvellanedaStoikovStrategy`
    - `start(core)`：连接 Backpack/Lighter 行情。
    - `async on_tick(now_ms)`：通过 `_ensure_initialized()`、`_refresh_market_state()`、`_place_quotes()` 实现库存控制。

- `arb/order_exec.py`
  - `LegOrder`、`LegExecutionResult`、`OrderTracker`、`TrackingLimitExecutor`：多腿订单跟踪、限价执行器、WS 事件解析（`parse_backpack_event`、`parse_lighter_event`）。

- `arb/pairing.py`
  - `discover_pairs(backpack, lighter, symbol_filters)`：通过 REST/WS 数据发现跨市场交易对。
  - `pick_common_sizes(backpack, lighter, bp_sym, lg_sym)`：基于最小数量、精度计算两市场可交易规模。

### 3.7 可执行入口 (`mm_bot/bin`)
每个脚本通过 `asyncio.run(main())` 启动不同策略，核心步骤一致：读取配置 -> 初始化日志 -> 创建 `TradingCore` + 连接器 -> 启动策略 -> 常驻运行。
- `run_backpack_perp_mm.py`：Backpack 永续做市。
- `run_cross_arb.py`：Backpack vs Lighter 跨市场套利。
- `run_grid_geometric.py`：几何网格策略。
- `run_hedge_ladder.py` / `run_hedge_wash.py`：阶梯对冲/洗单策略。
- `run_trend_ladder.py`：趋势阶梯策略。
- `run_as_model.py`：Avellaneda-Stoikov 策略。

### 3.8 测试 (`mm_bot/test`)
- `test_clock.py`：验证 `SimpleClock` Tick 与停止行为。
- `test_*_connector.py`：集成测试脚本（异步 `main()`），用以手动验证各交易所连接器的 REST/WS 功能。

## 4. 其他代码与资源
- `deploy/`
  - `server.js`：Express + WebSocket 服务，转发遥测数据。
  - `telemetry_receiver.py`：Python 端接收/处理遥测。
  - `card.html`：前端控制台。
- `lighter-python/`
  - OpenAPI 自动生成的客户端与数据模型（`lighter/api/*.py`、`lighter/models/*.py`），封装 REST 调用与签名。
  - `lighter/signers/`：预编译的签名动态库。
- `lighter-go/`
  - Go 语言版签名器与交易结构定义（`types/txtypes/*.go`）。
- `grvt-pysdk/`
  - Grvt 交易所 Python SDK，提供密钥处理、WS、REST 调用及大量单元测试。
- `lighter-tool/system_setup.py`：安装依赖、设置 signer 的工具脚本。
- `build/`：Signer 构建脚本（Linux/Windows）。
- `papers/`：限价订单簿、做市相关论文。
- `点子/`：策略草案与说明。

## 5. 代码依赖关系总结
- **核心流**：`mm_bot/bin/*.py` -> `TradingCore` -> 注册若干 `Connector` (Backpack/Lighter/Grvt) -> 绑定 `Strategy` -> `Strategy.on_tick` 调用 `Connector` 提供的行情/交易接口。
- **连接器依赖**：
  - `BackpackConnector` 依赖 `aiohttp`、`websockets`、`RateLimiter`，签名使用 `nacl.signing`；策略通过其 REST/WS 方法获取行情、下单。
  - `LighterConnector` 依赖 `lighter-python` SDK、`RateLimiter`、`websockets`，需本地 `lighter` 密钥和 Ethereum 私钥。
  - `GrvtConnector` 依赖 `grvt-pysdk`。
- **策略依赖**：各策略组合多个连接器，使用 `OrderTracker`、`TrackingLimitExecutor` 实现多腿同步；`arb.pairing` 与 `arb.order_exec` 提供共享逻辑。
- **限流与异步**：所有外部 API 调用经 `RateLimiter` 控制；`asyncio` 协程贯穿核心时钟、连接器、策略执行。
- **配置与密钥**：`mm_bot/conf/*.yaml` 定义默认参数；`config.py` + 环境变量提供覆盖；密钥文件是连接器运行前提。

## 6. 数据来源与配置要点
- **Backpack**：`Backpack_key.txt`（包含 `api key`、`api secret`）；REST API (`/api/v1/*`)，WS (`wss://ws.backpack.exchange`)。
- **Lighter**：`Lighter_key.txt`（API 密钥 + 以太坊私钥）；`lighter-python` 会自动生成签名并访问 `AccountApi`、`OrderApi`。
- **Grvt**：`grvt-pysdk` 支持的密钥格式；WS/HTTP 通过 SDK 实现。
- **日志**：默认写入 `logs/bot.log`，可通过 `mm_bot/conf/logging.yaml` 或环境变量调整。
- **环境变量覆盖**：`XTB_*` 前缀控制 tick 大小、密钥路径、RPC URL、策略参数（如 `XTB_BP_MM_SPREAD`）。

## 7. 附录：mm_bot 模块函数/方法清单

> 以下列表直接从源代码抽取，列出 `mm_bot` 目录内所有模块级函数、类方法及依赖，用于排查「vibe coding」导致的职责混杂问题。

$(cat mm_bot_module_overview.md)

# MM Bot Python Module Inventory

## `mm_bot/__init__.py`
> mm_bot package root for simplified bot components.

## `mm_bot/bin/run_as_model.py`
**Imports / Dependencies:** asyncio, logging, mm_bot.conf.config:load_config, mm_bot.connector.grvt.grvt_exchange:GrvtConfig, mm_bot.connector.grvt.grvt_exchange:GrvtConnector, mm_bot.connector.lighter.lighter_exchange:LighterConfig, mm_bot.connector.lighter.lighter_exchange:LighterConnector, mm_bot.core.trading_core:TradingCore, mm_bot.logger.logger:setup_logging, mm_bot.strategy.as_model:ASParams, mm_bot.strategy.as_model:AvellanedaStoikovStrategy, os, sys
**Module-Level Functions:**
 - async `main()`

## `mm_bot/bin/run_backpack_perp_mm.py`
**Imports / Dependencies:** asyncio, logging, mm_bot.conf.config:load_config, mm_bot.connector.backpack.backpack_exchange:BackpackConfig, mm_bot.connector.backpack.backpack_exchange:BackpackConnector, mm_bot.core.trading_core:TradingCore, mm_bot.logger.logger:setup_logging, mm_bot.strategy.backpack_perp_market_maker:BackpackPerpMarketMakerStrategy, mm_bot.strategy.backpack_perp_market_maker:PerpMarketMakerParams, os, sys
**Module-Level Functions:**
 - `_as_bool(value, default)`
 - `_as_float(value, default)`
 - `_as_int(value, default)`
 - async `main()`

## `mm_bot/bin/run_cross_arb.py`
**Imports / Dependencies:** asyncio, logging, mm_bot.conf.config:load_config, mm_bot.connector.backpack.backpack_exchange:BackpackConfig, mm_bot.connector.backpack.backpack_exchange:BackpackConnector, mm_bot.connector.lighter.lighter_exchange:LighterConfig, mm_bot.connector.lighter.lighter_exchange:LighterConnector, mm_bot.core.trading_core:TradingCore, mm_bot.logger.logger:setup_logging, mm_bot.strategy.cross_market_arbitrage:CrossArbParams, mm_bot.strategy.cross_market_arbitrage:CrossMarketArbitrageStrategy, os, sys
**Module-Level Functions:**
 - `_as_bool(value, default)`
 - async `main()`

## `mm_bot/bin/run_grid_geometric.py`
**Imports / Dependencies:** asyncio, logging, mm_bot.conf.config:load_config, mm_bot.connector.grvt.grvt_exchange:GrvtConfig, mm_bot.connector.grvt.grvt_exchange:GrvtConnector, mm_bot.connector.lighter.lighter_exchange:LighterConfig, mm_bot.connector.lighter.lighter_exchange:LighterConnector, mm_bot.core.trading_core:TradingCore, mm_bot.logger.logger:setup_logging, mm_bot.strategy.geometric_grid:GeometricGridParams, mm_bot.strategy.geometric_grid:GeometricGridStrategy, os, sys
**Module-Level Functions:**
 - async `main()`

## `mm_bot/bin/run_hedge_ladder.py`
**Imports / Dependencies:** asyncio, logging, mm_bot.conf.config:load_config, mm_bot.connector.backpack.backpack_exchange:BackpackConfig, mm_bot.connector.backpack.backpack_exchange:BackpackConnector, mm_bot.connector.lighter.lighter_exchange:LighterConfig, mm_bot.connector.lighter.lighter_exchange:LighterConnector, mm_bot.core.trading_core:TradingCore, mm_bot.logger.logger:setup_logging, mm_bot.strategy.hedge_ladder:HedgeLadderParams, mm_bot.strategy.hedge_ladder:HedgeLadderStrategy, os, sys
**Module-Level Functions:**
 - `_as_bool(value, default)`
 - `_as_float(value, default)`
 - `_as_int(value, default)`
 - async `main()`

## `mm_bot/bin/run_hedge_wash.py`
**Imports / Dependencies:** asyncio, dataclasses:dataclass, math, mm_bot.connector.backpack.backpack_exchange:BackpackConfig, mm_bot.connector.backpack.backpack_exchange:BackpackConnector, mm_bot.connector.lighter.lighter_exchange:LighterConfig, mm_bot.connector.lighter.lighter_exchange:LighterConnector, os, pathlib:Path, random, sys, time, typing:Any, typing:Callable, typing:Dict, typing:List, typing:Optional, typing:Tuple
**Module-Level Functions:**
 - `_norm_sym_bp(sym)`
 - `_norm_sym_lg(sym)`
 - `_delta_tolerance(delta)`
 - `_extract_order_id(ret)`
 - `load_hedge_config(path)`
 - `_try_float(val)`
 - `_iter_backpack_positions(payload)`
 - `_iter_lighter_positions(lighter, payload)`
 - async `bootstrap_backpack_positions(backpack, tracker)`
 - async `bootstrap_lighter_positions(lighter, tracker)`
 - async `wait_position_delta(tracker, symbol, previous, expected_delta, venue_name, timeout)`
 - async `discover_pairs(lighter, backpack)`
 - async `pick_size_common(backpack, lighter, bp_sym, lg_sym)`
 - async `main()`
**Classes:**
- `OrderSizeMeta` (bases: object)
- `CrossVenueSize` (bases: object)
- `HedgeStrategyConfig` (bases: object)
- `HedgeConfigBundle` (bases: object)
- `WSPositionTracker` (bases: object)
  - Small helper to await position changes surfaced via WS callbacks.
  - `__init__(self, name)`
  - `prime(self, symbol, value, raw)`
  - `update(self, symbol, value, raw)`
  - `snapshot(self, symbol)`
  - async `wait_for(self, symbol, predicate, timeout)`

## `mm_bot/bin/run_trend_ladder.py`
**Imports / Dependencies:** asyncio, logging, mm_bot.conf.config:load_config, mm_bot.connector.backpack.backpack_exchange:BackpackConfig, mm_bot.connector.backpack.backpack_exchange:BackpackConnector, mm_bot.core.trading_core:TradingCore, mm_bot.logger.logger:setup_logging, mm_bot.strategy.trend_ladder:TrendAdaptiveLadderStrategy, mm_bot.strategy.trend_ladder:TrendLadderParams, os, sys
**Module-Level Functions:**
 - async `main()`

## `mm_bot/conf/config.py`
**Imports / Dependencies:** json, os, typing:Any, typing:Dict, typing:Optional
**Module-Level Functions:**
 - `load_config(path)`: Load bot configuration from YAML or JSON.
Priority: explicit path -> env XTB_CONFIG -> default mm_bot/conf/bot.yaml.
If both YAML and JSON fail, return empty dict.

## `mm_bot/connector/__init__.py`

## `mm_bot/connector/backpack/backpack_exchange.py`
**Imports / Dependencies:** aiohttp, asyncio, base64, dataclasses:dataclass, json, logging, mm_bot.utils.throttler:RateLimiter, mm_bot.utils.throttler:lighter_default_weights, nacl.encoding:RawEncoder, nacl.signing:SigningKey, time, typing:Any, typing:Callable, typing:Dict, typing:List, typing:Optional, typing:Tuple, websockets
**Module-Level Functions:**
 - `load_backpack_keys(path)`
**Classes:**
- `BackpackConfig` (bases: object)
- `BackpackConnector` (bases: object)
  - `__init__(self, config)`
  - `start(self, core)`
  - async `close(self)`
  - `set_event_handlers(self)`
  - async `_ensure_markets(self)`
  - async `list_symbols(self)`
  - async `get_market_id(self, symbol)`
  - async `get_price_size_decimals(self, symbol)`
  - `_auth_headers(self, instruction, params)`
  - async `best_effort_latency_ms(self)`
  - async `get_account_overview(self)`
  - async `get_balances(self)`
  - async `get_collateral(self)`
  - async `get_positions(self)`
  - async `get_open_orders(self, symbol)`
  - async `get_market_info(self, symbol)`
  - async `get_order_book(self, symbol, depth)`
  - async `get_top_of_book(self, symbol)`
  - async `place_limit(self, symbol, client_order_index, base_amount, price, is_ask, post_only, reduce_only)`
  - async `place_market(self, symbol, client_order_index, base_amount, is_ask, reduce_only)`
  - async `cancel_order(self, order_index, symbol)`
  - async `get_order(self, symbol, order_id, client_id)`
  - async `cancel_all(self, symbol)`
  - async `cancel_by_client_id(self, symbol, client_id)`
  - async `start_ws_state(self, symbols)`
  - async `stop_ws_state(self)`
  - async `_ws_loop(self, symbols)`
  - `_private_ws_subscriptions(self, symbols)`
  - `_handle_private_order_event(self, payload)`
  - `_handle_private_position_event(self, payload)`

## `mm_bot/connector/grvt/grvt_exchange.py`
**Imports / Dependencies:** asyncio, dataclasses:dataclass, decimal:Decimal, os, sys, time, typing:Any, typing:Callable, typing:Dict, typing:List, typing:Optional, typing:Tuple
**Module-Level Functions:**
 - `_ensure_grvt_on_path(root)`
**Classes:**
- `GrvtConfig` (bases: object)
- `GrvtConnector` (bases: object)
  - Skeleton connector for GRVT SDK with a Lighter-compatible interface surface.
Fill in real REST/WS calls using grvt-pysdk.
  - `__init__(self, config, debug)`
  - `start(self, core)`
  - `stop(self, core)`
  - async `close(self)`
  - `set_event_handlers(self)`
  - async `start_ws_state(self)`
  - async `stop_ws_state(self)`
  - async `_ws_state_loop(self)`
  - async `_rest_reconcile_loop(self)`: Every ~10s reconcile WS caches with REST: open orders and positions.
Keeps _active_orders_by_symbol and _positions_by_symbol in sync with server state.
  - async `list_symbols(self)`
  - async `get_market_id(self, symbol)`
  - async `get_price_size_decimals(self, symbol)`
  - async `get_min_order_size_i(self, symbol)`
  - async `get_top_of_book(self, symbol)`
  - async `get_account_overview(self)`
  - async `get_open_orders(self, symbol)`
  - async `get_positions(self)`
  - async `place_limit(self, symbol, client_order_index, base_amount, price, is_ask, post_only, reduce_only)`
  - async `place_market(self, symbol, client_order_index, base_amount, is_ask, reduce_only)`
  - async `cancel_order(self, order_index, market_index)`
  - async `cancel_all(self)`
  - async `best_effort_latency_ms(self)`
  - `_load_keys(self)`
  - async `_ensure_markets(self)`
  - async `_on_ws_position(self, message)`
  - async `_on_ws_order(self, message)`
  - async `_on_ws_fill(self, message)`

## `mm_bot/connector/interfaces.py`
**Imports / Dependencies:** __future__:annotations, asyncio, typing:Any, typing:Callable, typing:Dict, typing:List, typing:Optional, typing:Protocol, typing:Tuple
**Classes:**
- `IConnector` (bases: Protocol)
  - `start(self, core)`
  - `stop(self, core)`
  - async `close(self)`
  - `set_event_handlers(self)`
  - async `start_ws_state(self)`
  - async `stop_ws_state(self)`
  - async `list_symbols(self)`
  - async `get_market_id(self, symbol)`
  - async `get_price_size_decimals(self, symbol)`
  - async `get_min_order_size_i(self, symbol)`
  - async `get_top_of_book(self, symbol)`
  - async `get_account_overview(self)`
  - async `get_open_orders(self, symbol)`
  - async `get_positions(self)`
  - async `place_limit(self, symbol, client_order_index, base_amount, price, is_ask, post_only, reduce_only)`
  - async `place_market(self, symbol, client_order_index, base_amount, is_ask, reduce_only)`
  - async `cancel_order(self, order_index, market_index)`
  - async `cancel_all(self)`
  - async `best_effort_latency_ms(self)`

## `mm_bot/connector/lighter/__init__.py`

## `mm_bot/connector/lighter/lighter_auth.py`
**Imports / Dependencies:** os, typing:Optional, typing:Tuple
**Module-Level Functions:**
 - `load_keys_from_file(path)`: Parse a simple key file with lines like:
  api key index: 2
  public key: <hex>
  private key: <hex>
  eth private key: 0x...
Returns: (api_key_index, api_private_key, api_public_key, eth_private_key)
 - `env_or(value, env_key)`

## `mm_bot/connector/lighter/lighter_exchange.py`
**Imports / Dependencies:** asyncio, collections:deque, dataclasses:dataclass, eth_account:Account, json, lighter, lighter_auth:load_keys_from_file, logging, mm_bot.utils.throttler:RateLimiter, mm_bot.utils.throttler:lighter_default_weights, os, sys, time, typing:Any, typing:Callable, typing:Deque, typing:Dict, typing:List, typing:Optional, typing:Tuple, websockets
**Module-Level Functions:**
 - `_ensure_lighter_on_path(root)`
**Classes:**
- `LighterConfig` (bases: object)
- `LighterConnector` (bases: object)
  - `__init__(self, config, debug)`
  - `start(self, core)`
  - `stop(self, core)`
  - async `close(self)`
  - async `_ensure_account_index(self)`
  - async `_ensure_signer(self)`
  - async `_ensure_markets(self)`
  - async `list_symbols(self)`
  - async `get_price_size_decimals(self, symbol)`: Return (price_decimals, size_decimals) for a given symbol.
  - async `get_min_order_size_i(self, symbol)`: Return minimal base size in integer units for a symbol.
  - async `get_market_id(self, symbol)`
  - async `best_effort_latency_ms(self)`
  - async `get_account_overview(self)`
  - `_mk_ws(self, market_ids, accounts, host)`
  - async `start_ws_order_book(self, symbols, on_update)`
  - async `start_ws_account(self, on_update)`
  - async `stop_ws(self)`
  - `set_event_handlers(self)`
  - async `start_ws_state(self)`
  - async `stop_ws_state(self)`
  - async `_ws_state_loop(self)`
  - async `_rest_reconcile_loop(self)`: Every 60s reconcile WS open-order cache with REST accountActiveOrders.
Only checks markets that currently have WS-known open orders to limit weight.
  - async `_run_state_ws_once(self)`
  - `_handle_state_message(self, data)`
  - `_apply_orders_update(self, orders_dict)`
  - `_apply_trades_update(self, trades_dict)`
  - `_apply_positions_update(self, positions_dict)`
  - `_handle_account_update(self, account_id, payload)`
  - async `place_limit(self, symbol, client_order_index, base_amount, price, is_ask, post_only, reduce_only)`
  - async `is_coi_open(self, client_order_index, market_index)`: Best-effort REST check whether an order (by client_order_index) appears in account active orders.
Returns True if present; False if not found; None on error.
  - async `place_market(self, symbol, client_order_index, base_amount, is_ask, reduce_only)`
  - async `cancel_all(self)`
  - async `cancel_order(self, order_index, market_index)`
  - async `cancel_by_client_order_index(self, client_order_index, symbol)`
  - async `is_order_open(self, order_index, market_index)`: Best-effort check via REST whether an order is still open.
Returns True if present in account active orders; False if not found; None on error.
  - async `get_open_orders(self, symbol)`
  - async `get_positions(self)`
  - async `get_best_bid(self, symbol)`
  - async `get_best_ask(self, symbol)`
  - async `get_top_of_book(self, symbol)`: Return (best_bid_i, best_ask_i, scale) where scale is 10**decimals detected
from the price string in orderBookOrders. This ensures consistent scaling
with how we convert prices to integers elsewhere.
  - async `_scales(self, symbol)`
  - async `place_limit_order(self, symbol, side, price, size, post_only, reduce_only)`
  - async `place_market_order(self, symbol, side, size, reduce_only)`

## `mm_bot/connector/lighter/lighter_ws.py`
**Imports / Dependencies:** asyncio, json, typing:Callable, typing:Dict, typing:List, typing:Optional
**Classes:**
- `LighterWS` (bases: object)
  - Thin wrapper around lighter.WsClient with async run loop management.
  - `__init__(self, order_book_ids, account_ids, on_order_book_update, on_account_update, host)`
  - `start(self)`
  - async `_run(self)`
  - async `stop(self)`

## `mm_bot/core/__init__.py`
> Core components for the simplified trading bot.
**Imports / Dependencies:** clock:SimpleClock, trading_core:TradingCore

## `mm_bot/core/clock.py`
**Imports / Dependencies:** asyncio, logging, time, typing:Awaitable, typing:Callable, typing:List, typing:Optional
**Module-Level Functions:**
 - `_get_logger()`
**Classes:**
- `SimpleClock` (bases: object)
  - Minimal asyncio-based clock that periodically calls registered async tick handlers
with the current wall time in milliseconds.
  - `__init__(self, tick_size, logger)`
  - `add_tick_handler(self, handler)`
  - async `_run(self)`
  - `start(self)`
  - async `stop(self)`

## `mm_bot/core/trading_core.py`
**Imports / Dependencies:** asyncio, clock:SimpleClock, logging, os, time, typing:Any, typing:Callable, typing:Dict, typing:Optional
**Module-Level Functions:**
 - `_get_logger()`
**Classes:**
- `TradingCore` (bases: object)
  - A simplified, standalone trading core tailored for a single-exchange, single-strategy MVP.

Responsibilities:
- Manage a single clock and a set of connectors
- Host one strategy (pluggable)
- Provide lifecycle controls (start, stop, shutdown)
- Optional debug output gated by a switch
- No dependency on Hummingbot internals
  - `__init__(self, tick_size, debug, clock_factory, logger)`
  - `dbg(self, msg, exc_info)`
  - `_resolve_debug_flag(self, arg_value)`
  - `add_connector(self, name, connector)`: Register a connector. A connector may optionally implement:
- start(core)
- stop(core)
- cancel_all(timeout: float)
- ready (bool)
  - `remove_connector(self, name)`
  - `set_strategy(self, strategy)`: Set the active strategy. Strategy should implement:
- start(core)
- stop()
- on_tick(now_ms: float) -> awaitable
  - async `start(self)`
  - async `stop(self, cancel_orders)`
  - async `shutdown(self, cancel_orders)`
  - `status(self)`

## `mm_bot/logger/logger.py`
**Imports / Dependencies:** logging, logging.config, os, typing:Optional
**Module-Level Functions:**
 - `default_logging_dict(log_dir)`
 - `setup_logging(config_path)`: Initialize logging using a YAML/JSON config file if provided;
otherwise use a sensible default with console and rotating file.
Env overrides: XTB_LOG_DIR, XTB_LOG_LEVEL, XTB_FILE_LOG_LEVEL, XTB_ROOT_LOG_LEVEL

## `mm_bot/strategy/arb/__init__.py`
> Utilities and components shared across arbitrage strategies.

## `mm_bot/strategy/arb/order_exec.py`
> Order execution helpers for arbitrage strategies.
**Imports / Dependencies:** __future__:annotations, asyncio, dataclasses:dataclass, dataclasses:field, logging, typing:Any, typing:Callable, typing:Dict, typing:Optional, typing:Tuple
**Module-Level Functions:**
 - `parse_backpack_event(payload)`
 - `parse_lighter_event(payload)`
**Classes:**
- `OrderCompletion` (bases: object)
  - `is_terminal(self)`
- `_OrderContext` (bases: object)
- `OrderTracker` (bases: object)
  - Track per-venue order lifecycle via WS event callbacks.
  - `__init__(self, venue, parser)`
  - `register(self, client_order_index)`
  - `handle_event(self, payload)`
  - `fail(self, client_order_index, reason)`
  - `timeout(self, client_order_index)`
  - `clear(self)`
- `LegOrder` (bases: object)
- `LegExecutionResult` (bases: object)
- `TrackingLimitExecutor` (bases: object)
  - Execute tracking limit orders with automatic re-post and optional market fallback.
  - `__init__(self)`
  - async `run(self, leg)`

## `mm_bot/strategy/arb/pairing.py`
> Pair discovery and sizing helpers shared by cross-market arbitrage strategies.
**Imports / Dependencies:** __future__:annotations, typing:Any, typing:Dict, typing:List, typing:Optional, typing:Tuple
**Module-Level Functions:**
 - `normalize_backpack_symbol(sym)`: Return (base, quote, is_perp) for a Backpack symbol.
 - `normalize_lighter_symbol(sym)`: Return (base, quote, is_perp) for a Lighter symbol.
 - async `discover_pairs(lighter, backpack)`: Return overlapping Backpack/Lighter markets along with discovery diagnostics.
 - async `pick_common_sizes(backpack, lighter, bp_sym, lg_sym)`: Pick venue-specific minimal sizes that align both legs in base units.

## `mm_bot/strategy/as_model.py`
**Imports / Dependencies:** asyncio, collections:deque, dataclasses:dataclass, logging, math, mm_bot.connector.interfaces:IConnector, mm_bot.strategy.strategy_base:StrategyBase, time, typing:Any, typing:Deque, typing:Dict, typing:List, typing:Optional, typing:Tuple
**Classes:**
- `ASParams` (bases: object)
- `AvellanedaStoikovStrategy` (bases: StrategyBase)
  - Avellaneda–Stoikov market making strategy.

- Estimates short-term volatility from rolling mid samples
- Computes reservation price r_t and optimal half-spread delta_t
- Quotes bid/ask around r_t as post-only orders
- Cancels prior working orders before placing new quotes (simple replace policy)
- Inventory from connector positions; basic skew via reservation price
  - `__init__(self, connector, symbol, params)`
  - `start(self, core)`
  - `stop(self)`
  - async `_ensure_ready(self)`
  - async `_current_mid_and_scale(self)`
  - `_update_vol_window(self, mid_i, now_ms)`
  - `_sigma_abs_per_sec(self)`
  - async `_find_open_for_side(self, is_ask)`: Return list of our open orders for the side (best-effort).
We identify by symbol and side; connector returns 'is_ask' and integer 'price'.
  - async `_update_quote_side(self, target_price_i, is_ask)`: Conditionally update a side with double-buffering and side limits.
- If existing top order price within requote_ticks, keep; else place new then cancel old.
- Limit max 2 live orders per side.
Returns client_order_index of placed order or None if unchanged/failed.
  - async `_cancel_prev_quotes(self)`
  - async `_place_post_only(self, price_i, is_ask)`
  - async `_inventory_base(self)`
  - async `on_tick(self, now_ms)`
  - async `_collect_telemetry(self)`
  - async `_telemetry_loop(self)`

## `mm_bot/strategy/backpack_perp_market_maker.py`
**Imports / Dependencies:** asyncio, dataclasses:dataclass, logging, math, mm_bot.connector.backpack.backpack_exchange:BackpackConnector, strategy_base:StrategyBase, time, typing:Any, typing:Dict, typing:List, typing:Optional, typing:Tuple
**Classes:**
- `PerpMarketMakerParams` (bases: object)
- `BackpackPerpMarketMakerStrategy` (bases: StrategyBase)
  - `__init__(self, connector, params, logger)`
  - `start(self, core)`
  - `stop(self)`
  - async `on_tick(self, now_ms)`
  - async `_run_cycle(self, now_ms)`
  - async `_ensure_initialized(self)`
  - async `_calculate_prices(self, net_position)`
  - async `_cancel_existing(self)`
  - async `_place_side_orders(self, prices)`
  - async `_manage_position(self, net_position)`
  - async `_close_position(self, qty, net_position)`
  - async `_get_net_position(self)`
  - `_resolve_order_quantity(self)`
  - `_sanitize_quantity(self, qty)`
  - `_round_price(self, price)`
  - `_price_to_int(self, price)`
  - `_quantity_to_int(self, quantity)`
  - `_next_client_order_index(self)`
  - `_handle_order_event(self, payload)`
  - `_handle_trade_event(self, payload)`
  - `_handle_position_update(self, payload)`

## `mm_bot/strategy/cross_market_arbitrage.py`
**Imports / Dependencies:** arb.order_exec:LegExecutionResult, arb.order_exec:LegOrder, arb.order_exec:OrderTracker, arb.order_exec:TrackingLimitExecutor, arb.order_exec:parse_backpack_event, arb.order_exec:parse_lighter_event, arb.pairing:discover_pairs, arb.pairing:pick_common_sizes, asyncio, dataclasses:dataclass, logging, strategy_base:StrategyBase, time, typing:Any, typing:Dict, typing:List, typing:Optional, typing:Tuple
**Classes:**
- `CrossArbParams` (bases: object)
- `CrossMarketArbitrageStrategy` (bases: StrategyBase)
  - `__init__(self, lighter_connector, backpack_connector, params, symbol_filters, logger)`
  - `start(self, core)`
  - `stop(self)`
  - async `_cancel_backpack_order(self, client_order_index, symbol)`
  - async `_cancel_lighter_order(self, client_order_index, symbol)`
  - `_on_backpack_order_event(self, payload)`
  - `_on_lighter_order_event(self, payload)`
  - `_on_backpack_trade(self, payload)`
  - `_on_lighter_trade(self, payload)`
  - `_on_backpack_position(self, payload)`
  - `_on_lighter_position(self, payload)`
  - async `_ensure_pair_subscriptions(self)`
  - async `_stop_core_after_debug(self)`
  - `_maybe_finish_debug_cycle(self)`
  - async `_ensure_pairs(self)`
  - `_within_maintenance(self)`
  - `_pre_maint_window(self)`
  - async `_maybe_circuit_break(self)`
  - `_next_client_order_index(self)`
  - async `_register_delta_failure(self, reason)`
  - async `_flatten_all_positions(self)`
  - async `_close_position(self, key, st)`
  - `_build_entry_legs(self, direction, bp_sym, lg_sym, size_bp_i, size_lg_i)`
  - async `_place_market_leg(self, leg, size_i)`
  - async `_rebalance_after_entry_failure(self, legs, results, reason)`
  - async `_compute_size_info(self, bp_sym, lg_sym)`
  - async `_gather_opportunities(self)`
  - async `_execute_entry(self, opportunity, now_ms)`
  - async `_execute_lighter_market_leg(self, leg)`
  - async `_enter_if_opportunity(self, now_ms)`
  - async `_ensure_balanced_positions(self)`
  - async `_maybe_exit_positions(self, now_ms)`
  - async `on_tick(self, now_ms)`

## `mm_bot/strategy/geometric_grid.py`
**Imports / Dependencies:** asyncio, dataclasses:dataclass, logging, math, mm_bot.strategy.strategy_base:StrategyBase, typing:Any, typing:Dict, typing:List, typing:Optional, typing:Tuple
**Classes:**
- `GeometricGridParams` (bases: object)
- `GeometricGridStrategy` (bases: StrategyBase)
  - `__init__(self, connector, symbol, params)`
  - `start(self, core)`
  - `stop(self)`
  - `_on_filled(self, info)`
  - `_on_cancelled(self, info)`
  - async `_ensure_ready(self)`
  - `_build_grid_prices(self)`
  - `_budget_size_i(self, price_i, per_side_slots)`
  - `_extract_order_price_i(self, od)`
  - async `_sync_open_orders_cache(self)`
  - async `_current_position_i(self)`
  - `_coi(self)`
  - async `_place_limit(self, price_i, is_ask, size_i)`
  - async `_ensure_grid_orders(self)`
  - `_select_initial_levels(self, mid_i)`
  - async `_handle_fill_followups(self, price_i, is_ask)`
  - async `on_tick(self, now_ms)`

## `mm_bot/strategy/hedge_ladder.py`
**Imports / Dependencies:** asyncio, collections:deque, dataclasses:dataclass, logging, mm_bot.strategy.strategy_base:StrategyBase, time, typing:Any, typing:Dict, typing:Optional
**Classes:**
- `HedgeLadderParams` (bases: object)
- `HedgeLadderStrategy` (bases: StrategyBase)
  - `__init__(self, backpack_connector, lighter_connector, params, logger)`
  - `start(self, core)`
  - `stop(self)`
  - async `_ensure_ready(self)`
  - `_next_coi(self)`
  - `_on_backpack_position(self, payload)`
  - `_on_lighter_position(self, payload)`
  - `_on_lighter_event(self, payload)`
  - `_on_backpack_filled(self, payload)`
  - `_on_backpack_cancelled(self, payload)`
  - async `_place_take_profit(self, entry_coi, entry_price_i, size_i)`
  - async `_maybe_place_entry(self, now_ms)`
  - async `_ensure_hedges(self, last_price)`
  - async `_open_hedge(self, entry_coi, record)`
  - async `_ensure_hedge_closed(self, entry_coi, record)`
  - async `_execute_lighter_market_order(self, size_i)`
  - async `_respect_hedge_rate_limit(self)`
  - async `on_tick(self, now_ms)`
  - async `shutdown(self)`

## `mm_bot/strategy/strategy_base.py`
**Imports / Dependencies:** typing:Any
**Classes:**
- `StrategyBase` (bases: object)
  - `start(self, core)`
  - `stop(self)`
  - async `on_tick(self, now_ms)`

## `mm_bot/strategy/trend_ladder.py`
**Imports / Dependencies:** asyncio, collections:deque, dataclasses:dataclass, logging, math, mm_bot.connector.backpack.backpack_exchange:BackpackConnector, mm_bot.strategy.strategy_base:StrategyBase, time, typing:Any, typing:Deque, typing:Dict, typing:List, typing:Optional, typing:Tuple
**Classes:**
- `TrendLadderParams` (bases: object)
- `TrendAdaptiveLadderStrategy` (bases: StrategyBase)
  - `__init__(self, connector, symbol, params)`
  - async `_place_limit_with_retry(self, base_amount_i, price_i, is_ask, post_only, reduce_only, retries, retry_delay)`
  - `start(self, core)`
  - `stop(self)`
  - `_on_filled(self, info)`
  - `_on_cancelled(self, info)`
  - async `_ensure_ready(self)`
  - `_order_id_of(self, obj)`
  - `_client_order_index_of(self, obj)`
  - `_order_is_reduce_only(self, obj)`
  - `_order_is_post_only(self, obj)`
  - `_order_side_label(self, obj)`
  - `_order_price_i(self, obj)`
  - `_order_amount_i(self, obj)`
  - `_quantize_price_i(self, price_i, is_ask)`
  - `_quantize_size_i(self, size_i, prefer_up)`
  - `_has_pending_entry(self, side)`
  - async `_hydrate_existing_orders(self)`
  - `_trade_amount_i(self, t)`
  - `_trade_is_ask(self, t)`
  - `_trade_coi(self, t)`
  - async `_handle_trade(self, t)`
  - `_on_price(self, price_i, now_ms)`
  - `_ema(self, values, length)`
  - `_atr(self, bars, length)`
  - `_compute_slope(self)`
  - `_net_position_base(self)`
  - async `_collect_telemetry(self)`
  - async `_telemetry_loop(self)`
  - async `_cancel_all_entries_and_tps(self)`
  - async `_place_flush_tp(self, bid, ask)`: Place a single reduce-only TP near best to flatten current net position.
  - async `_place_entry(self, side, best_bid, best_ask)`
  - `_desired_entry_price(self, side, best_bid, best_ask)`
  - async `_requote_stale_entries(self, best_bid, best_ask)`
  - async `_place_tp_for_entry(self, entry_price_i, side)`
  - `_sum_open_coverage_i(self, need_sell, opens)`: Sum integer base units of open orders that would reduce the position.
If need_sell=True, count asks; else count bids. Prefer explicit base_amount; fallback to size/amount.
Returns: (total_i, count_matched, count_total)
  - async `_tp_coverage_check_and_correct(self, bid, ask)`: Check coverage between net position and open reduce-only side coverage.
If deficit >= one lot, place reduce-only TPs to correct, rate-limited.
Returns True if any correction orders were placed.
  - async `_cancel_side_entries(self, side)`
  - `_calculate_wait_time(self)`: Return 0 to place now, 1 to wait a tick based on active close orders.
  - async `on_tick(self, now_ms)`

## `mm_bot/test/test_backpack_connector.py`
**Imports / Dependencies:** asyncio, mm_bot.connector.backpack.backpack_exchange:BackpackConfig, mm_bot.connector.backpack.backpack_exchange:BackpackConnector, os, sys, time
**Module-Level Functions:**
 - async `main()`

## `mm_bot/test/test_clock.py`
**Imports / Dependencies:** asyncio, mm_bot.core.clock:SimpleClock, os, pytest, sys, time
**Module-Level Functions:**
 - `test_simple_clock_ticks_and_stops()`

## `mm_bot/test/test_connector_ws_full.py`
**Imports / Dependencies:** asyncio, logging, mm_bot.connector.lighter.lighter_exchange:LighterConnector, os, sys, time, typing:Dict, typing:List
**Module-Level Functions:**
 - async `main()`

## `mm_bot/test/test_grvt_connector.py`
**Imports / Dependencies:** asyncio, logging, mm_bot.connector.grvt.grvt_exchange:GrvtConfig, mm_bot.connector.grvt.grvt_exchange:GrvtConnector, mm_bot.logger.logger:setup_logging, os, sys
**Module-Level Functions:**
 - async `main()`

## `mm_bot/test/test_lighter_connector.py`
**Imports / Dependencies:** asyncio, json, logging, mm_bot.connector.lighter.lighter_exchange:LighterConfig, mm_bot.connector.lighter.lighter_exchange:LighterConnector, os, os.path, platform, sys, time
**Module-Level Functions:**
 - async `main()`

## `mm_bot/utils/throttler.py`
**Imports / Dependencies:** asyncio, collections:defaultdict, time, typing:Dict, typing:Optional
**Module-Level Functions:**
 - `lighter_default_weights()`: Weights adapted from lighter_rate_limit.txt.

Keys are endpoint hints; use these when calling acquire().
For non-listed endpoints, default to weight=1.
**Classes:**
- `RateLimiter` (bases: object)
  - Simple token-bucket rate limiter with weighted requests.

- capacity: total tokens in a 60s window
- weights: per-endpoint weight mapping (defaults to 1)
- burst: optional instantaneous burst allowance (defaults to capacity)
  - `__init__(self, capacity_per_minute, weights, burst)`
  - `_refill(self)`
  - async `acquire(self, endpoint_key)`
