0. 设计目标与原则（面向跨所套利）

功能不减，复杂度可证降

保留：订单状态机、WS+REST 双通道、策略↔执行层解耦、符号映射、风控与紧急平仓

统一：单一订单模型、单一路由 + 四服务（符号/订单/仓位/风控）

简化：配置路径（CLI 直达 + YAML 进阶）、去注册表硬依赖、去多层 builder 链

可调试：强可观测性（时序线、事件ID、撮合与撤单时间戳）、一键 Smoke Test

约束：任意单文件 ≤ 600 行（CI 强制），避免再次“野蛮生长”

1. 目标目录结构（≤ 18 个文件，核心 < 2k LOC）
mm_bot/
├── app/
│   ├── main.py                            # 统一入口（CLI+YAML），<300行
│   └── config.py                          # dataclass 配置解析，<250行
├── core/
│   ├── clock.py                           # 简易时钟，<200行
│   └── trading_core.py                    # 启停与事件编排，<300行
├── execution/
│   ├── router.py                          # 薄路由（无状态），<200行
│   ├── order_model.py                     # Order/OrderEvent/OrderState，<300行
│   ├── services/
│   │   ├── symbol_service.py              # 映射/精度/最小量，<300行
│   │   ├── order_service.py               # 下单/撤单/对账（唯一实现），<600行
│   │   ├── position_service.py            # 仓位聚合，<300行
│   │   └── risk_service.py                # 前置/后置风控，<300行
│   └── tracking_limit.py                  # 追踪限价唯一实现（沿用/精简），<400行
├── connector/
│   ├── interface.py                       # IConnector 协议，<200行
│   ├── backpack_connector.py              # <600行
│   ├── lighter_connector.py               # <600行
│   └── grvt_connector.py                  # <600行
├── strategy/
│   ├── base.py                            # StrategyBase，<200行
│   ├── smoke_test.py                      # 诊断模板（短链路），<400行
│   └── liquidation_hedge.py               # 示例策略，<600行
├── utils/
│   ├── logging.py                         # 结构化日志/trace_id，<200行
│   └── async_tools.py                     # gather/timeout/retry，<200行
└── tests/
    ├── unit/                              # 单元测试
    ├── integration/                       # 连接器/服务集成
    └── e2e/                               # 端到端（含模拟所）


说明：

旧的 execution/layer.py、runtime/registry.py、复杂 builders 统一下线或“过渡期兼容壳”。

tracking_limit.py 作为唯一追踪限价实现，所有路径强制调用（杜绝平行版本）。

每个连接器独立单文件，硬性 ≤600 行；超出功能做到 utils 或 service。

2. 关键接口设计（Claude 可直接实现）
2.1 连接器接口（稳定面向执行层）
# mm_bot/connector/interface.py
from typing import Protocol, Optional, Tuple, Any, Dict, List

class IConnector(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    # 市场与规则
    async def get_price_size_decimals(self, symbol: str) -> Tuple[int, int]: ...
    async def get_min_size_i(self, symbol: str) -> int: ...
    async def get_top_of_book(self, symbol: str) -> Tuple[Optional[int], Optional[int], int]: ...
    async def get_order_book(self, symbol: str, depth: int = 5) -> Dict[str, Any]: ...

    # 下单/撤单（client_order_index 必填以便统一对账）
    async def submit_limit_order(self, *, symbol: str, client_order_index: int,
                                 base_amount: int, price: int, is_ask: bool,
                                 post_only: bool = False, reduce_only: int = 0) -> str: ...
    async def submit_market_order(self, *, symbol: str, client_order_index: int,
                                  size_i: int, is_ask: bool, reduce_only: int = 0) -> str: ...
    async def cancel_by_client_id(self, symbol: str, client_order_index: int) -> None: ...

    # 状态/仓位
    async def get_order(self, symbol: str, client_order_index: int) -> Dict[str, Any]: ...
    async def get_positions(self) -> List[Dict[str, Any]]: ...

2.2 订单模型（单一事实源）
# mm_bot/execution/order_model.py
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict

class OrderState(Enum):
    NEW="NEW"; SUBMITTING="SUBMITTING"; OPEN="OPEN"; PARTIALLY_FILLED="PARTIALLY_FILLED"
    FILLED="FILLED"; CANCELLED="CANCELLED"; FAILED="FAILED"

@dataclass
class OrderEvent:
    ts: float
    src: str          # "ws" | "rest" | "local"
    type: str         # "ack" | "fill" | "cancel_ack" | "reject" | ...
    info: Dict

@dataclass
class Order:
    coi: int
    venue: str
    symbol: str
    side: str         # "buy" | "sell"
    state: OrderState
    filled_base_i: int = 0
    last_event_ts: float = 0.0
    history: List[OrderEvent] = field(default_factory=list)

    def append(self, ev: OrderEvent) -> None: ...
    async def wait_final(self, timeout: float | None) -> OrderState: ...

2.3 路由与四服务
# mm_bot/execution/router.py
class ExecutionRouter:
    def __init__(self, services): self.sv = services  # {'symbol','order','pos','risk'}
    async def limit_order(self, venue, symbol, **kw): return await self.sv['order'].limit_order(venue, symbol, **kw)
    async def market_order(self, venue, symbol, **kw): return await self.sv['order'].market_order(venue, symbol, **kw)
    async def cancel(self, venue, symbol, coi):        return await self.sv['order'].cancel(venue, symbol, coi)
    async def position(self, venue=None):              return await self.sv['pos'].get_positions(venue)


symbol_service：map_symbol() / get_decimals() / get_min_size_i()

order_service（核心）：统一 limit/market/cancel/reconcile()；追踪限价只调用 tracking_limit.place_tracking_limit_order()

position_service：聚合仓位、净敞口

risk_service：下单前额度/最小量/净敞口阈等检查，失败即阻断

3. 可调试性与观测性（“时间线第一公民”）

结构化日志：统一字段 trace_id, coi, engine_ts, ws_seq, cancel_ack_ts，默认 INFO，诊断时 DEBUG

时间线写入：所有订单写入 logs/orders/{venue}-{symbol}-{coi}.jsonl（事件按 ts 排序）

Smoke Test 一键命令：

python -m mm_bot.app.main --venue lighter --symbol BTC --mode tracking_limit \
  --qty 1000 --interval 10 --timeout 120 --price-offset 2 --debug


输出 attempts、价格序列、撤单/重挂时间、最终状态

故障熔断：单位时间异常 N 次自动 cancel_all+退出（RiskService 里实现）

4. 配置与启动（双通道）

CLI 直达：日常开发与回归主路径（更少耦合）

YAML 进阶：多 venue/多策略组合时启用，config.py 直接 from_yaml → dataclass，避免“YAML→Dict→Registry→Builders→Params→Strategy”的多跳

示例：

# config.yaml
general: { debug: true, tick_ms: 500 }
connectors:
  lighter: { keys_file: Lighter_key.txt, base_url: "..." }
  backpack: { keys_file: Backpack_key.txt, base_url: "..." }
strategy:
  name: "liquidation_hedge"
  params: { base_symbol: "BTC", hedge_symbol: "BTCPERP", max_spread_bps: 8 }

5. 测试与验收（交付即质量）
5.1 单元测试（unit/）

test_order_model.py：状态机/等待超时/事件时间线

test_symbol_service.py：映射、精度、最小量

test_risk_service.py：阈值/可用保证金/最小量拒单

test_tracking_limit.py：先撤后挂，尝试次数与间隔校验（interval 误差 ≤ 300ms）

5.2 集成测试（integration/）

test_order_service_mock_connector.py：用假连接器回放 WS/REST 竞态（先 FILLED 后 CANCELLED → 以撮合回执为准）

test_position_service_multi_venues.py：跨所仓位聚合

test_router_thinness.py：静态分析路由不含状态缓存/锁（防“回胖”）

5.3 端到端（e2e/）

test_smoke_cli_no_yaml.py：纯 CLI 跑通，生成日志与价格序列

test_dual_connector_parallel_cancel.py：A/B 同时撤单，耗时近似 max(TA, TB)+ε

test_liquidation_hedge_nominal.py：小资金穿仓保护、紧急平仓

5.4 质量门槛（CI 必过）

行数限制：脚本统计任何 .py 文件 > 600 行 → 失败

类型检查：mypy 严格模式

风格：ruff + black

覆盖率：核心 execution/ 与 connector/ ≥ 80%

长链路回归：tracking_limit、symbol 映射、router 三条关键路径的快测套件

7. 复杂度与变更治理（防“回胖”）

文件 ≤600 行（CI 强制）；函数 ≤ 80 行；类 ≤ 400 行

禁止循环依赖（CI import graph 检查）

唯一实现原则：追踪限价仅在 execution/tracking_limit.py，若发现第二实现 → CI 失败

性能守则：

追踪限价重挂周期 observed ≤ configured + 300ms

策略退出 ≤ 5s 完成撤单与 WS 关闭

PR 清单：新增/修改的公共接口需附带：示例用法 + 单元 + 集成 + e2e（若影响关键路径）

8. Claude/Codex 开发指令模板（复制即用）

任务 1：建立新路由与四服务骨架

新增 execution/router.py（薄路由）与 services/* 四文件空实现

把旧 ExecutionLayer 的逻辑拆分到四服务，路由只负责转发

添加最小单元测试，确保 import/接口签名正确

任务 2：迁移追踪限价唯一实现

保留 execution/tracking_limit.py，在 order_service.py 的 limit_order(..., tracking=True) 中只调用它

添加 test_tracking_limit.py 验证“先撤后挂/超时/attempts/interval 误差”

任务 3：统一订单模型

新建 order_model.py，替换全仓库旧的 OrderTracker/TrackingLimitOrder/OrderUpdate 引用

写 test_order_model.py 验证状态机 + wait_final

任务 4：连接器对齐 IConnector

对 backpack/lighter/grvt 适配接口，删除冗余回调地狱与多层缓存

加 integration 测试：下单→撤单→WS/REST 对账

任务 5：Smoke Test 改造

strategy/smoke_test.py 改为短链路：Strategy → Router(OrderService)；追踪限价显式走唯一实现

输出价格序列/attempts/时间线到日志

任务 6：配置双通道与 CI 规则

app/main.py：CLI 直达；app/config.py：YAML→dataclass

CI：行数/类型/风格/覆盖率/关键回归加入

终点状态（验收标准）

CLI 一条命令即可跑策略（无需 YAML）

关键路径总 LOC < 2,000；任意文件 < 600 行（CI 保障）

三层验证全绿：单元/集成/e2e；关键回归（追踪限价、符号映射、路由薄化）稳定

旧层退场：不再被策略引用，仅保留迁移文档