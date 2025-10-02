轻量化多交易所 API 策略框架 — 开发需求说明（对外）
0) 概览

目标：实现一个轻量、可调试、可扩展的多交易所策略框架，支持 5 家交易所现货/永续的下单与仓位/保证金跟踪，策略层一键切换交易所。

语言/环境：Python 3.10+（优先），asyncio 架构。

非功能性：单文件 ≤600 行（硬性 CI 约束）、高可观测性、单元+集成+端到端测试覆盖、简洁依赖。

1) 交易所接入范围

我方提供 5 家交易所的：API 文档 URL、参考文档 URL、测试/小额资金 API 密钥。

需优先支持的能力：

市价单（market）

限价单（limit）

追踪限价（tracking-limit：在对应方向 Top-of-Book 附近挂限价，先撤后挂，每 10s 调整直至成交/超时）

成交/仓位/保证金 跟踪：优先 WebSocket，REST 仅兜底（掉线/补偿）。

需处理：部分成交（partial fill）、撤单确认、异常/拒单、断线重连、时钟/时序一致性（WS 先到 vs REST 先到）。

2) 分层架构与解耦（必须）
2.1 目录建议（不强制命名，强制职责）
app/          # 启动与配置（CLI + YAML）
core/         # 时钟/生命周期
execution/    # 路由(薄) + 服务层(订单/仓位/风控/符号映射) + 状态模型 + tracking_limit
connector/    # 各交易所实现（独立文件；每个≤600行）
strategy/     # 策略基类 + 示例策略（可一键切换交易所）
utils/        # 日志/重试/时间/精度
tests/        # unit + integration + e2e

2.2 统一接口（强制）
# connector/interface.py
from typing import Protocol, Optional, Tuple, Dict, Any, List

class IConnector(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    # 规则&行情
    async def get_price_size_decimals(self, symbol: str) -> Tuple[int, int]: ...
    async def get_min_size_i(self, symbol: str) -> int: ...
    async def get_top_of_book(self, symbol: str) -> Tuple[Optional[int], Optional[int], int]: ...
    # 下单/撤单（必须带 client_order_index 幂等）
    async def submit_limit_order(self, *, symbol: str, client_order_index: int,
                                 base_amount: int, price: int, is_ask: bool,
                                 post_only: bool=False, reduce_only: int=0) -> str: ...
    async def submit_market_order(self, *, symbol: str, client_order_index: int,
                                  size_i: int, is_ask: bool, reduce_only: int=0) -> str: ...
    async def cancel_by_client_id(self, symbol: str, client_order_index: int) -> None: ...
    # 查询
    async def get_order(self, symbol: str, client_order_index: int) -> Dict[str, Any]: ...
    async def get_positions(self) -> List[Dict[str, Any]]: ...
    async def get_margin(self) -> Dict[str, Any]: ...  # 可选：若交易所提供

2.3 路由与服务（强制“薄路由 + 四服务”）

ExecutionRouter（薄）：不存状态，只分发到服务。

Symbol/MarketDataService：canonical ↔ venue 符号、tick/lot 精度、最小下单量。

OrderService：唯一订单通道（market/limit/cancel/reconcile），追踪限价仅调用 tracking_limit.place_tracking_limit_order（防止平行实现）。

PositionService：统一聚合仓位、净敞口；适配 position WS。

RiskService：前置校验（最小量、余额/保证金、净敞口上限），失败直接拒单。

Order 模型（单一事实源）：SUBMITTING→OPEN→PARTIALLY_FILLED→FILLED/CANCELLED/FAILED，内置 history/timeline。

3) 追踪限价定义（必须一致）

首挂：按 is_ask 方向在 对手价内侧 挂单（买单≤bid；卖单≥ask）；支持 price_offset_ticks。

循环：每 interval_secs=10 做一次 撤单→重挂；允许 max_attempts；timeout_secs。

退出：FILLED 或 TIMEOUT/达最大次数（强制撤掉最后一单）；报告 attempts 与价格序列。

处理部分成交：累计 filled_base_i；若 <99.99% 维持 PARTIALLY_FILLED；达到阈值转 FILLED。

4) 符号/精度/最小量（必须）

配置层只写 canonical（如 SOL），路由/服务负责映射为各场内符号（如 SOL_USDC_PERP）。

所有四条路径都必须走映射：limit/market/cancel/tracking-limit。

price/size 传 整数刻度（乘以 10^decimals），并做越界/≤0 校验，避免被交易所自动转市价。

5) WS/REST 双通道一致性（必须）

下单前已订阅 order/position 流；在 REST 提交前即在本地创建 Order，避免“WS 先到”丢单。

统一对账：WS 增量优先、REST 周期校验；遇状态竞态以撮合时间戳裁决。

自动重连：断线重连后 自动重订阅；补偿一次 REST 快照恢复状态。

6) 心跳包（必须）

当策略唤起任意连接器后，心跳协程随系统启动，并每 N 秒向指定 HTTP(s) 端点发送 JSON：

{
  "ts": 1730000000,
  "strategy": "smoke_test",
  "venue": "backpack",
  "positions": [{"symbol":"SOL_USDC_PERP","qty":0.06,"notional":13.159}],
  "margin": {"balance": 1234.56, "available": 1200.00}
}


支持重试/超时/失败回退；支持签名或 Bearer token（我们提供）。

8) 配置与启动（必须）

CLI 直达：python -m app.main --venue backpack --symbol SOL --mode tracking_limit --qty 0.01

YAML（进阶场景）：config.yaml → dataclass；CLI 同名参数覆盖 YAML。

不得使用复杂注册表/多层构造链；以路由+服务为唯一入口。

9) 日志与可观测性（必须）

结构化日志：trace_id, coi, engine_ts, ws_seq, cancel_ack_ts；INFO 简洁、DEBUG 包含 WS 原文。

订单 timeline 持久化：logs/orders/{venue}-{symbol}-{coi}.jsonl。

统一错误码与异常：下单拒绝/余额不足/精度错误/WS超时等。

10) 测试与验收（必须）
10.1 单元测试（unit）

test_order_model.py：状态机与超时/竞态（WS 先到/REST 先到）。

test_tracking_limit.py：先撤后挂逻辑，间隔误差 ≤300ms；超时后最后一单被撤销。

test_symbol_mapping.py：多 venue 的精度/最小量校验。

test_risk_service.py：净敞口/最小量/保证金拦截。

10.2 集成测试（integration）

假连接器回放：orderAccepted → orderFill(partial) → orderFill(final)、orderCancelled、positionOpened/Adjusted/Closed。

WS 断线重连自动恢复订阅 + REST 快照对账。

10.3 端到端（e2e）验收（强制）

启动所有连接器，在所有交易所同时以最小下单量执行：

tracking_limit 开仓直到成交（必要时价格偏移，避免立即吃单）；

随机停留 1–30s；

市价平仓；

全程跟踪 仓位 + 保证金（优先 WS）；

生成汇总报告（每个 venue 的 attempts、timeline、最终仓位=0 校验、保证金变化记录）。

验收标准：无未处理异常；每个 venue 至少有一次成功的 开仓+平仓；仓位归零；心跳包持续上报；日志完整。

11) 代码质量与 CI（必须）

单文件 ≤1000 行（CI 脚本强制检查，不达标即失败）。

类型检查：mypy --strict；风格：ruff + black。

覆盖率：execution/ 与 connector/ ≥80%。

唯一实现原则：tracking-limit 仅在 execution/tracking_limit.py；若出现第二实现，CI 失败。

12) 交付物（必须）

代码 + 测试 + 示例配置

README.md（启动、配置、测试说明）

docs/EXCHANGE_INTEGRATION.md（新增交易所的接入流程与模板）

docs/HEARTBEAT.md（心跳包协议与鉴权示例）

docs/STRATEGY_GUIDE.md（如何在策略层调用与切换交易所）

