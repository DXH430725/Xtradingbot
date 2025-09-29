● ✅ 重构完成总结

  我已经成功完成了您提出的系统重构方案，主要成果如下：

  🏗️ 核心架构重构

  A. ExecutionLayer 瘦身为薄路由 + 四小服务 ✅
  - 创建了 execution/router.py（<150行薄路由）
  - 拆分为四个专业化服务：
    - SymbolService: 符号映射、精度、最小量统一管理
    - OrderService: 唯一下单/撤单实现，强制使用 tracking_limit.place_tracking_limit_order
    - PositionService: 跨venue仓位聚合和风险管理
    - RiskService: 下单前/后风控检查和敞口管理

  B. 订单状态合并为单一聚合 ✅
  - 用单一 Order dataclass 替代了 OrderTracker/TrackingLimitOrder/OrderUpdate
  - 保留完整历史追踪和异步等待能力
  - 消除了多处重复状态存储

  C. WS/REST 和解器上墙 ✅
  - 固化时间线和解规则：WS事件优先，REST校验一致性
  - 竞态条件以引擎时间戳为准
  - 增强日志：engine_ts, cancel_ack_ts, ws_seq
  - 自动检测和报告竞态条件

  D. Smoke Test 精简为诊断模板 ✅
  - 创建了 connector_diagnostics.py 诊断模板
  - 三核心方法：place_limit/market/cancel
  - 新增模式：limit_once|tracking_limit|market
  - 增强报告：价格时间序列、竞态检测、时间线分析

  🎯 核心优势

  1. 架构清晰：薄路由 + 专业化微服务，职责分离明确
  2. 状态简化：单一事实源，消除重复存储
  3. 可观测性强：详细时间线、竞态检测、价格监控
  4. 向后兼容：现有代码无需修改，ExecutionLayer 作为 ExecutionRouter 别名

  🚀 立即可用

  - 配置文件：connector_diagnostics.yaml
  - 运行命令：python mm_bot/bin/runner.py --config connector_diagnostics.yaml
  - 报告输出：增强的JSON格式，包含时间线分析和竞态检测

  这次重构在保留跨所套利必需复杂度的同时，大幅简化了系统架构，提升了可维护性和可观测性。新的诊断系统将成为未来策略开发的强大基础。