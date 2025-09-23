# 工作记录（跨市场套利）

日期：2025-09-21

## 今日进度
- Backpack 连接器增加签名统一处理与私有 WS 订阅，能够实时接收 `account.orderUpdate` / `positionUpdate` 并对接策略缓存。
- 策略价差计算改为使用真实价格（按各 venue `scale` 归一化），避免精度差引发误判入场。
- 新增 `lighter_market_execution` 参数，支持 Lighter 侧直接市价执行、Backpack 保持跟踪限价，减少 taker 手续费偏差。
- `get_positions` 优先使用 WS 缓存，确保 `_ensure_balanced_positions` 基于最新仓位判断；若无缓存自动回退 REST。
- 调整测试脚本：禁止 Backpack 市价 fallback，只做一次限价尝试并记录私有推送，便于核对仓位变化。

## 待验证
- 使用 `XTB_CONFIG=mm_bot/conf/cross_arb_debug.yaml python mm_bot/bin/run_cross_arb.py` 观察完整一轮套利，确认双腿数量与WS反馈一致且无敞口。
- 根据资金情况调节 Backpack 限价容差或下单量，避免频繁返回 "Price is too far from the last active price"。

> 主要更新

  - 新增 mm_bot/strategy/hedge_ladder.py：以 Backpack 为主交易腿的梯式限价策略，成交后自动记录 entry_price 并计算
  hedge_price=entry*hedge_trigger_ratio。价格跌破触发线时，通过 Lighter 市价开出对冲仓位；回升后自动平掉。内置每秒 10 单的滑动窗口限流和滑点重试，保证
  对冲执行不超标且失败会补单。
  - 新增运行脚本 mm_bot/bin/run_hedge_ladder.py 与配置 mm_bot/conf/hedge_ladder.yaml，支持环境变量重写 Backpack/Lighter 连接、建仓参数、对冲阈值等；脚
  本在退出时会先停核心再显式调用 strategy.shutdown()，确保残余对冲仓位被平掉。
  - 更新 CODE_ARCH_REPORT.md，补充 HedgeLadder 架构说明、配置命令及目录索引，方便后续查阅。

  测试

  - python -m compileall mm_bot/strategy/hedge_ladder.py mm_bot/bin/run_hedge_ladder.py（语法校验）
  - 未跑真实撮合 / 回测；部署前需在沙盒或小仓位实际验证，尤其关注 Backpack 限价被拒及 Lighter 市价补单的表现。

  后续建议

  1. 在测试账户执行 python mm_bot/bin/run_hedge_ladder.py，确认阶梯建仓、TP、对冲触发/解除全链路无敞口。
  2. 视实际滑点情况调节 hedge_trigger_ratio、hedge_retry_delay 或允许 Backpack 市价 fallback，以降低 “Price is too far…” 拒单频次。

> Backpack 集成

- 强化 REST/WS 身份验证，以镜像实时客户端：Backpack 请求现在发送经纪商 ID + 大写标头，并在签名中重用 API 密钥 (mm_bot/connector/backpack/backpack_exchange.py:17-188)。添加了余额/抵押品/订单簿助手，以便更高层级可以匹配参考客户端 (mm_bot/connector/backpack/backpack_exchange.py:203-
305)，升级批量取消功能，在回退到每个订单之前先访问 /api/v1/orders (mm_bot/connector/backpack/backpack_exchange.py:477-510)，并扩展了私有 WS 订阅，以便我们签署相同的 account.orderUpdate.{symbol} 通道 (mm_bot/connector/backpack/backpack_exchange.py:600-631)。
- 创建了一个异步永续做市商策略，该策略与 Yan 的逻辑相似，但与 TradingCore 的 tick 循环相符。它会加载
市场限价、跟踪库存、应用库存偏差、取消/重新发布仅发布网格，并在突破限价时使用仅减仓的市场
退出机制 (mm_bot/strategy/backpack_perp_market_maker.py:12-358)。事件挂钩会保持最新仓位
与 WS 更新同步。
- 添加了专用运行器和配置，以便您可以像跨套利路径一样启动它：run_backpack_perp_mm.py 连接了
连接器和新参数 (mm_bot/bin/run_backpack_perp_mm.py:1-137)，backpack_perp_mm.yaml 则提供了我们熟悉的
价差/最大订单/仓位旋钮 (mm_bot/conf/backpack_perp_mm.yaml:1-24)。

后续步骤

- 将 XTB_CONFIG 指向 mm_bot/conf/backpack_perp_mm.yaml 并运行 python mm_bot/bin/run_backpack_perp_mm.py；从
较小的 order_quantity 开始，并在扩展之前确认成交/仓位遥测。
- 如果您依赖动态调整规模（余额/抵押品），请将新的连接器助手挂接到策略的调整规模
例程中。
- 满意后，考虑围绕 BackpackPerpMarketMakerStrategy 添加轻量级测试（模拟连接器响应），
以保护 tick 循环。

 - mm_bot/bin/runner.py:12-193 replaces the run_* scripts with a unified CLI: it loads configs, slices legacy connector
  blocks, resolves strategy specs via the new registry, boots connectors/strategies, and honours the global debug flag.
  - mm_bot/runtime/config_utils.py:9-85 and mm_bot/runtime/registry.py:16-93 add shared config parsing utilities plus a
  formal Connector/Strategy registry that wires builders, optional prepare hooks, and dynamic connector resolution.
  - mm_bot/runtime/builders.py:420-500 registers the new smoke-test strategy and resolver, while extending builders to
  construct connector configs and factories consistently.
  - mm_bot/execution/orders.py:1-98 introduces the reusable order tracking primitives (OrderTracker, TrackingLimitOrder,
  TrackingMarketOrder) that implement the shared state machine.
  - mm_bot/connector/base.py:32-200 adds the BaseConnector with unified listener fan-out, status-to-state mapping, debug
  logging, and tracking-aware order submission helpers.
  - mm_bot/connector/backpack/backpack_exchange.py:330-438 now routes REST/WS flows through the BaseConnector (state
  updates, new submit_limit_order/submit_market_order APIs, and failure handling).
  - mm_bot/connector/lighter/lighter_exchange.py:636-738 & 710-758 mirrors the same tracking integration, reconciles WS/
  account events back into the shared state machine, and adds submit helpers; cancel paths now notify the tracker too.
  - mm_bot/connector/grvt/grvt_exchange.py:1-120 now subclasses the base so future GRVT wiring can plug into the shared
  machinery without further churn.
  - mm_bot/strategy/smoke_test.py:11-184 adds ConnectorSmokeTestStrategy, a configurable connector smoke-test that
  places a limit order, waits for a terminal state, and flattens with a market order using the new tracking APIs.
  - docs/ARCHITECTURE.md:1-33 documents the refreshed three-layer architecture, executor responsibilities, and runner/
  registry flow so the new structure is discoverable.
  - mm_bot/test/test_order_tracking.py:1-35 adds a unit test that exercises the BaseConnector order state machine and
  ensures listeners see the expected signal.

  Tests:

  - PYTHONPATH=. pytest mm_bot/test/test_order_tracking.py