# XTradingBot 架构（精简版 / 当前生产栈）

本文聚焦当前线上使用的最小闭环：Core + LighterConnector + TrendLadder，并补充近期为“持仓与止盈不平衡”修复所做的关键改动。历史性的多交易所/扩展方案已移除，避免信息噪音。

## 一、总体概览

- 运行入口：`python mm_bot/bin/run_trend_ladder.py`
- 主要组件：
  - Core：`mm_bot/core/trading_core.py`（轻量时钟 + 生命周期容器）
  - Connector：`mm_bot/connector/lighter/*`（Lighter 交易所 REST/WS 封装）
  - Strategy：`mm_bot/strategy/trend_ladder.py`（生产策略 Trend Ladder）
  - Utils：`mm_bot/utils/throttler.py`（加权速率限制）
  - Logging/Config：`mm_bot/logger/*`、`mm_bot/conf/*`
  - Bin：`mm_bot/bin/*`（运行脚本与诊断脚本）
  - Monitor：`deploy/server.js` + `deploy/card.html`（可选监控卡片）、`deploy/telemetry_receiver.py`

系统以“一个 Core + 一个 Connector + 一个 Strategy”的最小闭环运行。Strategy 通过 Connector 获取盘口、账户与订单状态，并通过 Connector 下/撤单；Connector 维护 WS 缓存，并定期用 REST 对账增强一致性。

## 二、核心数据与事件流

- 市场信息：`get_top_of_book(symbol) -> (bid_i, ask_i, scale)`；`get_price_size_decimals(symbol)`。
- WS 缓存：`_active_orders_by_market`（打开单）、`_positions_by_market`（持仓）、`_trades_by_market`（成交）。
- REST 对账：每 ~60s 用 `account_active_orders` 对齐打开单，修正 WS 偶发不一致。
- 事件回调：`set_event_handlers(on_order_filled, on_order_cancelled, on_trade, on_position_update)`。
- 终态识别：订单状态包含 `filled/canceled/expired/rejected/failed` 时，移出打开缓存并仅触发一次回调。

## 三、Trend Ladder 策略（要点）

- 固定方向分批进场；entry 成交后按绝对价差放置 reduce-only TP。
- 局部止盈：收到 `on_trade` 时按净成交量补挂 reduce-only TP。
- 覆盖自检：每 tick 对比净仓位与 reduce-only 打开单，缺口 ≥ 最小手数则限频补齐。
- 重引价：entry 偏离最优价超过阈值（tick/绝对值）时撤旧挂新（带退避）。

内部跟踪：
- `_orders`：coi -> 元信息（`type/side/entry_price_i/flags`）。
- `_tp_cois`：活跃 TP 的 COI 集，用于节流与覆盖统计。

## 四、近期修复（不平衡根因相关）

1) 连接器错误归一化（place_limit/market）
- 位置：`mm_bot/connector/lighter/lighter_exchange.py`
- 行为：若返回对象 `ret is None` 或 `ret.code != 200`，将其视为错误（`err = 'non_ok_code:XXX'`），并且不登记 `_inflight_by_coi`。
- 影响：避免“API 实际失败但被当作成功”的假阳性，减少幽灵 TP 计数。

2) 订单终态识别更严格
- 位置：`_apply_orders_update`
- 行为：将 `expired/rejected/failed` 视作终态，触发 `on_order_cancelled` 并从打开缓存移除。

3) 策略 verify 失败清理
- 位置：`mm_bot/strategy/trend_ladder.py`
- 行为：TP 下单后异步校验，若 WS 与 REST 均不见该 COI，清理 `_tp_cois` 与 `_orders` 并打印警告。
- 影响：节流与覆盖统计回归真实，不再长时间误判“TP很多”。

## 五、运行与诊断

- 运行：`python mm_bot/bin/run_trend_ladder.py`
- 环境变量：
  - 通用：`XTB_SYMBOL`, `XTB_TICK_SIZE`, `XTB_LOG_DIR`, `XTB_LOG_LEVEL`
  - Lighter：`XTB_LIGHTER_BASE_URL`, `XTB_LIGHTER_KEYS_FILE`, `XTB_LIGHTER_ACCOUNT_INDEX`, `XTB_LIGHTER_RPM`
  - 策略：`XTB_TL_*`（如 `XTB_TL_QTY`, `XTB_TL_TP`, `XTB_TL_MAX_ORDERS`, `XTB_TL_REQUOTE_*`, `XTB_TL_TELEM_*`）

- 诊断脚本：
  - `python mm_bot/bin/diagnose_tp_mismatch.py`：对比 WS/REST 打开单与 reduce-only 覆盖。
  - `python mm_bot/bin/diagnose_tp_side_vs_ro.py`：比较“方向匹配打开单”与“reduce-only 打开单”的差异。

- 关键日志：
  - 连接器：`place_limit submit/result`（非 200 打印 `err=non_ok_code:*`）。
  - 策略：`tp submit intent` / `tp placed` / `tp verify`（verify 失败会清理本地 tracking）。
  - 覆盖检查：`imbalance detected` / `coverage ok`。

## 六、目录要点（速览）

- Core：`mm_bot/core/trading_core.py`
- Connector：`mm_bot/connector/lighter/`
- Strategy：`mm_bot/strategy/trend_ladder.py`
- Utils：`mm_bot/utils/throttler.py`
- Bin：`mm_bot/bin/`
- Deploy：`deploy/*`

## 七、后续工作（按需）

- COI 生成更稳健（时间戳 + 自增计数）。
- REST 对账：在无打开单的市场低频抽样对账（配额允许时）。
- 遥测指标新增：reduce-only 覆盖度/缺口。

