import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from mm_bot.execution.orders import OrderState, TrackingMarketOrder
from mm_bot.strategy.strategy_base import StrategyBase

COI_LIMIT = 281_474_976_710_655  # 2**48 - 1
POSITION_EPS = 1e-9


@dataclass
class LighterMinOrderTarget:
    connector: str
    direction: str = "buy"
    reduce_only: bool = True
    size_multiplier: float = 1.0


@dataclass
class LighterMinOrderParams:
    symbol: str = "ETH"
    direction: str = "buy"  # legacy single-target field
    reduce_only: bool = True
    size_multiplier: float = 1.0
    max_attempts: int = 3
    retry_delay_secs: float = 0.5
    max_slippage: float = 0.001
    targets: Optional[List[LighterMinOrderTarget]] = None


@dataclass
class _TargetContext:
    name: str
    connector: Any
    direction: str
    reduce_only: bool
    size_multiplier: float
    coi_seed: int


class LighterMinOrderStrategy(StrategyBase):
    """Fire minimal market orders on one or more Lighter connectors for diagnostics."""

    def __init__(
        self,
        connectors: Dict[str, Any],
        params: Optional[LighterMinOrderParams] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if not connectors:
            raise ValueError("at least one lighter connector is required")
        self._connectors = connectors
        self.params = params or LighterMinOrderParams()
        self.log = logger or logging.getLogger("mm_bot.strategy.lighter_min_order")

        self._targets: List[_TargetContext] = self._build_targets()
        if not self._targets:
            raise ValueError("no valid lighter targets resolved")

        self._core: Optional[Any] = None
        self._task: Optional[asyncio.Task] = None
        self._active: bool = True
        self.overall_success: bool = False
        self.failure_reason: Optional[str] = None

    def _build_targets(self) -> List[_TargetContext]:
        targets_cfg = self.params.targets
        targets: List[_TargetContext] = []

        def _make(direction: str, reduce_only: bool, size_multiplier: float, name: str, connector: Any) -> _TargetContext:
            seed = int(time.time() * 1000) % COI_LIMIT or 1
            return _TargetContext(
                name=name,
                connector=connector,
                direction=direction.lower(),
                reduce_only=reduce_only,
                size_multiplier=size_multiplier,
                coi_seed=seed,
            )

        if targets_cfg:
            for entry in targets_cfg:
                name = entry.connector.lower()
                connector = self._connectors.get(name)
                if connector is None:
                    self.log.error("target connector '%s' not available", name)
                    continue
                targets.append(
                    _make(
                        direction=entry.direction,
                        reduce_only=entry.reduce_only,
                        size_multiplier=entry.size_multiplier,
                        name=name,
                        connector=connector,
                    )
                )
        else:
            # legacy single-target path
            if len(self._connectors) == 1:
                name, connector = next(iter(self._connectors.items()))
            else:
                name = "lighter"
                connector = self._connectors.get(name) or next(iter(self._connectors.values()))
            targets.append(
                _make(
                    direction=self.params.direction,
                    reduce_only=self.params.reduce_only,
                    size_multiplier=self.params.size_multiplier,
                    name=name,
                    connector=connector,
                )
            )
        return targets

    def start(self, core: Any) -> None:
        if self._task is not None and not self._task.done():
            return
        self._core = core
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._run(), name="lighter_min_order.run")

    def stop(self) -> None:
        self._active = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def on_tick(self, now_ms: float) -> None:
        await asyncio.sleep(0)

    async def _run(self) -> None:
        try:
            await self._ensure_markets()
            success = True
            errors: List[str] = []
            for target in self._targets:
                ok = await self._fire_order(target)
                success = success and ok
                if not ok and self.failure_reason:
                    errors.append(f"{target.name}:{self.failure_reason}")
            self.overall_success = success
            if not success and errors:
                self.failure_reason = ", ".join(errors)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure_reason = str(exc)
            self.log.exception("lighter min order strategy crashed")
        finally:
            self._active = False
            if self._core is not None:
                try:
                    asyncio.create_task(self._core.stop(cancel_orders=False))
                except Exception:
                    pass

    async def _ensure_markets(self) -> None:
        for target in self._targets:
            await target.connector.start_ws_state()
            await target.connector._ensure_markets()

    async def _fire_order(self, target: _TargetContext) -> bool:
        symbol = self.params.symbol
        min_size_i = max(1, await target.connector.get_min_order_size_i(symbol))
        size_i = int(round(min_size_i * max(target.size_multiplier, 1.0)))
        is_ask = target.direction == "sell"
        reduce_only = 1 if target.reduce_only else 0
        max_slippage = max(float(self.params.max_slippage or 0.0), 0.0)

        attempts = max(int(self.params.max_attempts or 1), 1)
        retry_delay = max(float(self.params.retry_delay_secs or 0.0), 0.0)
        baseline_pos = await self._get_position(target.connector, symbol)
        api_key_index = self._api_key_index(target.connector)

        for attempt in range(1, attempts + 1):
            try:
                coi = self._next_coi(target)
                nonce_state = self._nonce_snapshot(target.connector)
                self.log.debug(
                    "%s submit attempt=%s coi=%s api_key=%s nonce_state=%s size_i=%s direction=%s reduce_only=%s",
                    target.name,
                    attempt,
                    coi,
                    api_key_index,
                    nonce_state,
                    size_i,
                    target.direction,
                    reduce_only,
                )
                tracker: TrackingMarketOrder = await target.connector.submit_market_order(
                    symbol=symbol,
                    client_order_index=coi,
                    base_amount=size_i,
                    is_ask=is_ask,
                    reduce_only=reduce_only,
                    max_slippage=max_slippage if max_slippage > 0 else None,
                )
            except Exception as exc:
                self.failure_reason = f"{target.name}:submission_failed:{exc}"
                self.log.error("%s submit market failed attempt=%s error=%s", target.name, attempt, exc)
                if attempt < attempts and retry_delay:
                    await asyncio.sleep(retry_delay)
                continue

            try:
                await tracker.wait_final(timeout=30.0)
            except Exception as exc:
                self.failure_reason = f"{target.name}:wait_failed:{exc}"
                self.log.error("%s wait_final failed attempt=%s error=%s", target.name, attempt, exc)
                if attempt < attempts and retry_delay:
                    await asyncio.sleep(retry_delay)
                continue

            snapshot = tracker.snapshot()
            info = snapshot.info if snapshot else None
            info_short = self._shorten_info(info)
            nonce_state_after = self._nonce_snapshot(target.connector)

            if tracker.state == OrderState.FILLED:
                size_scale = await self._size_scale(target.connector, symbol)
                fill_size = self._filled_size(tracker, info, size_scale)
                if fill_size <= POSITION_EPS:
                    current_pos = await self._get_position(target.connector, symbol)
                    fill_size = abs(current_pos - baseline_pos)
                requested = size_i / float(size_scale)
                self.log.info(
                    "%s order filled requested=%.8f filled=%.8f api_key=%s nonce_state=%s info=%s",
                    target.name,
                    requested,
                    fill_size,
                    api_key_index,
                    nonce_state_after,
                    info_short,
                )
                return True

            reason = getattr(tracker, "last_error", None) or getattr(tracker, "reject_reason", None)
            if reason is None and info:
                reason = info.get("error") or info.get("message")
            state_val = getattr(tracker.state, "value", tracker.state)
            self.failure_reason = f"{target.name}:state={state_val} reason={reason}"
            self.log.warning(
                "%s attempt=%s ended state=%s reason=%s api_key=%s nonce_state=%s info=%s",
                target.name,
                attempt,
                state_val,
                reason,
                api_key_index,
                nonce_state_after,
                info_short,
            )
            info_code = ""
            if isinstance(info, dict):
                info_code = str(info.get("code") or "")
            reason_text = str(reason or "").lower()
            if "invalid nonce" in reason_text or info_code == "21104":
                await self._refresh_nonce(target.connector, api_key_index)
                if attempt < attempts and retry_delay:
                    await asyncio.sleep(retry_delay * max(1, attempt))
                continue
            if attempt < attempts and retry_delay:
                await asyncio.sleep(retry_delay)
        return False

    async def _size_scale(self, connector: Any, symbol: str) -> int:
        _, size_dec = await connector.get_price_size_decimals(symbol)
        return 10 ** size_dec

    def _filled_size(self, tracker: TrackingMarketOrder, info: Optional[Dict[str, Any]], size_scale: int) -> float:
        snap = tracker.snapshot()
        if snap and snap.filled_base is not None:
            try:
                return float(snap.filled_base)
            except Exception:
                pass
        inner = getattr(tracker, "_tracker", None)
        if inner is not None:
            for past in reversed(inner.history):
                filled = getattr(past, "filled_base", None)
                if filled is not None:
                    try:
                        return float(filled)
                    except Exception:
                        continue
        if info and isinstance(info, dict):
            for key in ("filledBase", "filled_base", "baseExecuted", "base_filled"):
                if info.get(key) is not None:
                    try:
                        return float(info[key])
                    except Exception:
                        continue
        return 0.0

    def _next_coi(self, target: _TargetContext) -> int:
        target.coi_seed += 1
        if target.coi_seed > COI_LIMIT:
            target.coi_seed = 1
        return target.coi_seed

    def _shorten_info(self, info: Optional[Dict[str, Any]]) -> Optional[str]:
        if not info:
            return None
        try:
            return str(info)[:200]
        except Exception:
            return None

    async def _get_position(self, connector: Any, symbol: str) -> float:
        try:
            positions = await connector.get_positions()
        except Exception:
            return 0.0
        key = self._symbol_key(symbol)
        for entry in positions:
            sym = self._symbol_key(entry.get("symbol"))
            if sym == key:
                try:
                    return float(entry.get("position") or entry.get("netQuantity") or 0.0)
                except Exception:
                    return 0.0
        return 0.0

    def _api_key_index(self, connector: Any) -> Optional[int]:
        signer = getattr(connector, "signer", None)
        for attr in ("api_key_index", "apiKeyIndex", "api_key_id"):
            value = getattr(signer, attr, None)
            if value is not None:
                try:
                    return int(value)
                except Exception:
                    return None
        value = getattr(connector, "_api_key_index", None)
        if value is not None:
            try:
                return int(value)
            except Exception:
                return None
        return None

    def _nonce_snapshot(self, connector: Any) -> Optional[str]:
        signer = getattr(connector, "signer", None)
        manager = getattr(signer, "nonce_manager", None)
        mapping = getattr(manager, "nonce", None)
        if isinstance(mapping, dict) and mapping:
            try:
                return ",".join([f"{k}:{mapping[k]}" for k in sorted(mapping)])
            except Exception:
                return str(mapping)
        return None

    async def _refresh_nonce(self, connector: Any, api_key_index: Optional[int]) -> None:
        signer = getattr(connector, "signer", None)
        manager = getattr(signer, "nonce_manager", None)
        if manager is None or api_key_index is None:
            return
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, manager.hard_refresh_nonce, api_key_index)
            self.log.debug(
                "%s refreshed nonce api_key=%s nonce_state=%s",
                getattr(connector, "name", "lighter"),
                api_key_index,
                self._nonce_snapshot(connector),
            )
        except Exception as exc:
            self.log.warning(
                "%s nonce refresh failed api_key=%s error=%s",
                getattr(connector, "name", "lighter"),
                api_key_index,
                exc,
            )

    @staticmethod
    def _symbol_key(symbol: Any) -> str:
        text = str(symbol or "").upper()
        return "".join(ch for ch in text if ch.isalnum())


__all__ = ["LighterMinOrderStrategy", "LighterMinOrderParams", "LighterMinOrderTarget"]
