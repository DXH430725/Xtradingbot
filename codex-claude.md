# Claude-Codex Handoff Notes

## Investigation: Tracking Limit Performance Issue in Backpack Test

### Problem Description
Backpack test experiencing unexpected performance with tracking limit orders. Suspected that `smoke_test.py` might not be using the proper tracking logic (`tracking_limit.place_tracking_limit_order`).

### Key Findings

#### 1. Code Flow Analysis

**smoke_test.py (line 168)**:
```python
tracker = await self.execution.limit_order(
    venue,
    canonical,
    base_amount_i=size_i,
    is_ask=is_ask,
    interval_secs=max(task.tracking_interval_secs, 1.0),
    timeout_secs=max(task.tracking_timeout_secs, 10.0),
    price_offset_ticks=task.price_offset_ticks,
    cancel_wait_secs=max(task.cancel_wait_secs, 0.5),
    reduce_only=0,
)
```

**ExecutionLayer.limit_order() (line 112-133)**:
```python
async def limit_order(
    self,
    venue: str,
    canonical_symbol: str,
    *,
    base_amount_i: int,
    is_ask: bool,
    **kwargs: Any,
) -> TrackingLimitOrder:
    connector = self.connectors[venue.lower()]
    venue_symbol = self.symbols.to_venue(canonical_symbol, venue, default=canonical_symbol)
    lock = await self._lock_for(venue)
    return await place_tracking_limit_order(  # <-- calls order_actions version
        connector,
        symbol=venue_symbol,
        base_amount_i=base_amount_i,
        is_ask=is_ask,
        coi_manager=self.coi_manager,
        lock=lock,
        logger=self.log,
        **kwargs,
    )
```

**order_actions.place_tracking_limit_order() (line 134-160)**:
```python
async def place_tracking_limit_order(
    connector: Any,
    *,
    symbol: str,
    base_amount_i: int,
    is_ask: bool,
    coi_manager: Optional[COIManager] = None,
    lock: Optional[asyncio.Lock] = None,
    label: Optional[str] = None,
    **kwargs: Any,
) -> TrackingLimitOrder:
    """Wrapper adding COI sequencing and optional locking around legacy limit helper."""

    # ...setup...

    return await legacy_tracking_limit(  # <-- calls tracking_limit.place_tracking_limit_order
        connector,
        symbol=symbol,
        base_amount_i=base_amount_i,
        is_ask=is_ask,
        **kwargs,
    )
```

#### 2. Architecture Verification

**CORRECT**: The code IS using the proper tracking logic:

1. `smoke_test.py` → `ExecutionLayer.limit_order()`
2. `ExecutionLayer.limit_order()` → `order_actions.place_tracking_limit_order()` (wrapper)
3. `order_actions.place_tracking_limit_order()` → `tracking_limit.place_tracking_limit_order()` (legacy/core implementation)

The architecture shows:
- `order_actions.place_tracking_limit_order()` is a wrapper that adds COI management and locking
- It imports the core tracking logic as `legacy_tracking_limit` from `tracking_limit.py`
- The actual tracking behavior (continuous re-posting at top of book) is implemented in `tracking_limit.place_tracking_limit_order()`

#### 3. Actual Issue Location

The tracking limit issue is **NOT** related to wrong function calls. The flow is correct. The issue might be:

1. **Connector Implementation**: The Backpack connector's `submit_limit_order`, `get_top_of_book`, or `cancel_*` methods
2. **Top of Book Logic**: The `_top_of_book()` function in `tracking_limit.py` (lines 158-178) fallback behavior
3. **Price Selection**: The `_select_price()` function (lines 188-211) offset calculation
4. **Market Data**: WebSocket feeds not providing proper book data for price discovery

### Recommended Next Steps

1. **Check Backpack Connector**: Review `mm_bot/connector/backpack/connector.py` for proper implementation of:
   - `submit_limit_order()` method
   - `get_top_of_book()` or `get_order_book()` methods
   - Order cancellation methods

2. **Enable Debug Logging**: Run with `XTB_DEBUG=1` to see tracking limit progress messages

3. **Examine Order Book Data**: Verify that Backpack connector is receiving proper market data via WebSocket

4. **Price Offset Logic**: Check if `price_offset_ticks` configuration is appropriate for Backpack's tick size

### Root Cause Found and Fixed

**Issue**: Backpack连接器的`cancel_by_client_id`方法需要两次API调用（先查询订单再撤单），但追踪限价的撤单等待时间只有2秒，导致撤单操作超时失败。

**Log Evidence**:
```
2025-09-29 18:17:15 INFO cancel_order start symbol=BTC_USDC_PERP coi=2499401393 wait_secs=2.0
2025-09-29 18:17:15 INFO cancel_order using method=cancel_by_client_id
2025-09-29 18:17:17 WARNING cancel_order timeout method=cancel_by_client_id
2025-09-29 18:17:17 INFO tracking_limit cancelled_order attempt=1 wait_secs=2.0 final_state=open
```

**Fixes Applied**:

1. **配置修改** (`mm_bot/conf/connector_test.yaml`):
   ```yaml
   cancel_wait_secs: 20.0  # 从默认2.0增加到20.0
   ```

2. **连接器优化** (`mm_bot/connector/backpack/rest.py`):
   - 首先尝试直接用clientId撤单（单次API调用）
   - 如果失败，回退到原来的方法（查询+撤单）
   - 减少撤单延迟，提高成功率

3. **调试日志增强** (`mm_bot/execution/tracking_limit.py`):
   - 添加详细的时间追踪日志
   - 显示订单状态变化过程
   - 帮助诊断类似问题

### Current Status
- ✅ **Root cause identified**: Backpack cancel timeout
- ✅ **Fixes implemented**: Increased timeout + optimized cancel method
- ✅ **Debug logging added**: For future troubleshooting
- 🧪 **Ready for testing**: Should now properly cancel and repost every 10 seconds

---
*Updated by Claude on 2024-09-29*