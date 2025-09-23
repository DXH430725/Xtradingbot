import asyncio
import math
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mm_bot.connector.lighter.lighter_exchange import LighterConnector, LighterConfig
from mm_bot.connector.backpack.backpack_exchange import BackpackConnector, BackpackConfig


def _norm_sym_bp(sym: str) -> Tuple[str, str, bool]:
    s = sym.upper().strip()
    perp = False
    for sep in ("_", "-", "/"):
        if sep in s:
            parts = s.split(sep)
            if parts[-1] in ("PERP", "FUT", "SWAP"):
                perp = True
                parts = parts[:-1]
            if len(parts) >= 2:
                return parts[0], parts[1], perp or ("PERP" in s)
    if s.endswith("PERP"):
        perp = True
        s = s[:-4]
    if s.endswith("USDC"):
        base = s[:-4]
        return base, "USDC", perp or ("PERP" in sym.upper())
    return s, "", perp or ("PERP" in sym.upper())


def _norm_sym_lg(sym: str) -> Tuple[str, str, bool]:
    s = sym.upper().strip()
    for sep in ("-", "_", "/"):
        if sep in s:
            parts = s.split(sep)
            if len(parts) >= 2:
                base = parts[0]
                quote = parts[1]
                perp = (len(parts) >= 3 and parts[2] in ("PERP", "FUT", "SWAP")) or ("PERP" in s)
                return base, quote, perp
    # fallback: assume BASEUSDC style
    if s.endswith("USDC"):
        base = s[:-4]
        return base, "USDC", ("PERP" in s)
    # Lighter symbols often omit quote; default to USDC perp
    return s, "USDC", True


@dataclass
class OrderSizeMeta:
    size_i: int
    size_float: float
    price_decimals: int
    size_decimals: int


@dataclass
class CrossVenueSize:
    backpack: OrderSizeMeta
    lighter: OrderSizeMeta


@dataclass
class HedgeStrategyConfig:
    hold_seconds_min: int = 600
    hold_seconds_max: int = 900
    cooldown_seconds_min: int = 0
    cooldown_seconds_max: int = 300
    backpack_fill_timeout: float = 180.0
    lighter_fill_timeout: float = 90.0
    reprice_after_seconds: float = 5.0
    max_reprice_attempts: int = 1


@dataclass
class HedgeConfigBundle:
    lighter: LighterConfig
    backpack: BackpackConfig
    strategy: HedgeStrategyConfig


def _delta_tolerance(delta: float) -> float:
    return max(abs(delta) * 0.1, 1e-9)


def _extract_order_id(ret: Any) -> Optional[str]:
    if ret is None:
        return None
    if isinstance(ret, dict):
        for key in ("id", "orderId", "order_id"):
            if key in ret and ret[key] is not None:
                return str(ret[key])
    oid = getattr(ret, "id", None)
    if oid is not None:
        return str(oid)
    return None


def load_hedge_config(path: Optional[str] = None) -> HedgeConfigBundle:
    default_strategy = HedgeStrategyConfig()
    bundle = HedgeConfigBundle(
        lighter=LighterConfig(),
        backpack=BackpackConfig(),
        strategy=HedgeStrategyConfig(),
    )

    candidate = path or os.getenv("XTB_HEDGE_CONFIG")
    if candidate:
        cfg_path = Path(candidate).expanduser()
    else:
        cfg_path = Path(__file__).resolve().parent.parent / "conf" / "hedge_wash.yaml"

    if not cfg_path.exists():
        return bundle

    try:
        import yaml  # type: ignore
    except Exception:
        print("pyyaml not available; using default hedge config")
        return bundle

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        print(f"failed to load hedge config {cfg_path}: {exc}")
        return bundle

    lighter_cfg = data.get("lighter") or {}
    for key, value in lighter_cfg.items():
        if key in bundle.lighter.__dataclass_fields__:
            setattr(bundle.lighter, key, value)

    backpack_cfg = data.get("backpack") or {}
    for key, value in backpack_cfg.items():
        if key in bundle.backpack.__dataclass_fields__:
            setattr(bundle.backpack, key, value)

    strategy_cfg = data.get("strategy") or {}
    for key, value in strategy_cfg.items():
        if key in bundle.strategy.__dataclass_fields__:
            setattr(bundle.strategy, key, value)

    def _as_int(value: Any, fallback: int) -> int:
        try:
            return int(value)
        except Exception:
            return fallback

    def _as_float(value: Any, fallback: float) -> float:
        try:
            return float(value)
        except Exception:
            return fallback

    strat = bundle.strategy
    strat.hold_seconds_min = _as_int(strat.hold_seconds_min, default_strategy.hold_seconds_min)
    strat.hold_seconds_max = _as_int(strat.hold_seconds_max, default_strategy.hold_seconds_max)
    strat.cooldown_seconds_min = _as_int(strat.cooldown_seconds_min, default_strategy.cooldown_seconds_min)
    strat.cooldown_seconds_max = _as_int(strat.cooldown_seconds_max, default_strategy.cooldown_seconds_max)
    strat.backpack_fill_timeout = _as_float(strat.backpack_fill_timeout, default_strategy.backpack_fill_timeout)
    strat.lighter_fill_timeout = _as_float(strat.lighter_fill_timeout, default_strategy.lighter_fill_timeout)
    strat.reprice_after_seconds = _as_float(strat.reprice_after_seconds, default_strategy.reprice_after_seconds)
    strat.max_reprice_attempts = _as_int(strat.max_reprice_attempts, default_strategy.max_reprice_attempts)

    if strat.hold_seconds_min > strat.hold_seconds_max:
        strat.hold_seconds_min, strat.hold_seconds_max = strat.hold_seconds_max, strat.hold_seconds_min
    if strat.cooldown_seconds_min > strat.cooldown_seconds_max:
        strat.cooldown_seconds_min, strat.cooldown_seconds_max = strat.cooldown_seconds_max, strat.cooldown_seconds_min

    strat.hold_seconds_min = max(0, strat.hold_seconds_min)
    strat.hold_seconds_max = max(strat.hold_seconds_min, strat.hold_seconds_max)
    strat.cooldown_seconds_min = max(0, strat.cooldown_seconds_min)
    strat.cooldown_seconds_max = max(strat.cooldown_seconds_min, strat.cooldown_seconds_max)

    if strat.backpack_fill_timeout <= 0:
        strat.backpack_fill_timeout = default_strategy.backpack_fill_timeout
    if strat.lighter_fill_timeout <= 0:
        strat.lighter_fill_timeout = default_strategy.lighter_fill_timeout
    if strat.reprice_after_seconds <= 0:
        strat.reprice_after_seconds = default_strategy.reprice_after_seconds
    strat.max_reprice_attempts = max(0, strat.max_reprice_attempts)

    return bundle


class WSPositionTracker:
    """Small helper to await position changes surfaced via WS callbacks."""

    def __init__(self, name: str):
        self.name = name
        self._states: Dict[str, Dict[str, Any]] = {}

    def prime(self, symbol: str, value: Optional[float], raw: Optional[Dict[str, Any]] = None) -> None:
        if symbol not in self._states:
            self._states[symbol] = {"value": value, "raw": raw, "seq": 0, "event": asyncio.Event()}
        else:
            state = self._states[symbol]
            state["value"] = value
            state["raw"] = raw

    def update(self, symbol: Optional[str], value: Optional[float], raw: Optional[Dict[str, Any]] = None) -> None:
        if not symbol or value is None:
            return
        state = self._states.get(symbol)
        if state is None:
            state = {"value": value, "raw": raw, "seq": 1, "event": asyncio.Event()}
            self._states[symbol] = state
        else:
            state["value"] = value
            state["raw"] = raw
            state["seq"] += 1
        state["event"].set()

    def snapshot(self, symbol: str) -> Tuple[Optional[float], int, Optional[Dict[str, Any]]]:
        state = self._states.get(symbol)
        if not state:
            return None, 0, None
        return state.get("value"), state.get("seq", 0), state.get("raw")

    async def wait_for(
        self,
        symbol: str,
        predicate: Callable[[Optional[float], Optional[Dict[str, Any]]], bool],
        timeout: float,
    ) -> Tuple[Optional[float], Optional[Dict[str, Any]]]:
        state = self._states.setdefault(symbol, {"value": None, "raw": None, "seq": 0, "event": asyncio.Event()})
        while True:
            value = state.get("value")
            raw = state.get("raw")
            if predicate(value, raw):
                return value, raw
            event = state["event"]
            try:
                await asyncio.wait_for(event.wait(), timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(f"{self.name} position wait timeout for {symbol}") from exc
            event.clear()


def _try_float(val: Any) -> Optional[float]:
    try:
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str) and not val:
            return None
        return float(val)
    except Exception:
        return None


def _iter_backpack_positions(payload: Dict[str, Any]) -> List[Tuple[str, Optional[float], Dict[str, Any]]]:
    out: List[Tuple[str, Optional[float], Dict[str, Any]]] = []

    def handle(entry: Dict[str, Any]) -> None:
        if not isinstance(entry, dict):
            return
        symbol = entry.get("symbol") or entry.get("market") or entry.get("instrument")
        if not symbol:
            return
        value: Optional[float] = None
        for key in (
            "position",
            "positionSize",
            "positionQty",
            "size",
            "quantity",
            "netSize",
            "basePosition",
        ):
            cand = entry.get(key)
            value = _try_float(cand)
            if value is not None:
                break
        if value is None:
            return
        side = entry.get("positionSide") or entry.get("side") or entry.get("direction")
        if isinstance(side, str):
            sl = side.lower()
            if "short" in sl or sl.startswith("sell"):
                value = -abs(value)
            elif "long" in sl or sl.startswith("buy"):
                value = abs(value)
        out.append((symbol, value, entry))

    if isinstance(payload, dict):
        handle(payload)
        for key in ("positions", "data", "payload"):
            sub = payload.get(key)
            if isinstance(sub, list):
                for item in sub:
                    if isinstance(item, dict):
                        handle(item)
            elif isinstance(sub, dict):
                handle(sub)
    return out


def _iter_lighter_positions(lighter: LighterConnector, payload: Dict[str, Any]) -> List[Tuple[str, Optional[float], Dict[str, Any]]]:
    out: List[Tuple[str, Optional[float], Dict[str, Any]]] = []

    def handle(entry: Dict[str, Any]) -> None:
        if not isinstance(entry, dict):
            return
        symbol = entry.get("symbol")
        if not symbol:
            mid = entry.get("market_id") or entry.get("market_index") or entry.get("marketId")
            try:
                if mid is not None:
                    mid_i = int(mid)
                    symbol = lighter._market_to_symbol.get(mid_i)  # type: ignore[attr-defined]
            except Exception:
                symbol = None
        if not symbol:
            return
        value: Optional[float] = None
        for key in (
            "position",
            "position_size",
            "net_base",
            "base_amount",
            "basePosition",
        ):
            cand = entry.get(key)
            value = _try_float(cand)
            if value is not None:
                break
        if value is None:
            long_abs = _try_float(entry.get("long_position") or entry.get("longBase"))
            short_abs = _try_float(entry.get("short_position") or entry.get("shortBase"))
            if long_abs is not None or short_abs is not None:
                value = (long_abs or 0.0) - (short_abs or 0.0)
        if value is None:
            return
        out.append((symbol, value, entry))

    if isinstance(payload, dict):
        handle(payload)
        for key in ("positions", "data", "payload"):
            sub = payload.get(key)
            if isinstance(sub, list):
                for item in sub:
                    if isinstance(item, dict):
                        handle(item)
            elif isinstance(sub, dict):
                handle(sub)
    return out


async def bootstrap_backpack_positions(backpack: BackpackConnector, tracker: WSPositionTracker) -> None:
    try:
        positions = await backpack.get_positions()
    except Exception:
        return
    if not isinstance(positions, list):
        return
    for entry in positions:
        if not isinstance(entry, dict):
            continue
        symbol = entry.get("symbol") or entry.get("market")
        value = None
        for key in ("position", "size", "quantity", "positionSize", "netSize"):
            value = _try_float(entry.get(key))
            if value is not None:
                break
        if symbol and value is not None:
            tracker.prime(symbol, value, entry)


async def bootstrap_lighter_positions(lighter: LighterConnector, tracker: WSPositionTracker) -> None:
    try:
        positions = await lighter.get_positions()
    except Exception:
        return
    if not isinstance(positions, list):
        return
    for entry in positions:
        if not isinstance(entry, dict):
            continue
        symbol = entry.get("symbol")
        if not symbol:
            try:
                mid = entry.get("market_id") or entry.get("market_index")
                if mid is not None:
                    symbol = lighter._market_to_symbol.get(int(mid))  # type: ignore[attr-defined]
            except Exception:
                symbol = None
        value = None
        for key in ("position", "position_size", "net_base", "base_amount"):
            value = _try_float(entry.get(key))
            if value is not None:
                break
        if symbol and value is not None:
            tracker.prime(symbol, value, entry)


async def wait_position_delta(
    tracker: WSPositionTracker,
    symbol: str,
    previous: Optional[float],
    expected_delta: float,
    venue_name: str,
    timeout: float = 45.0,
) -> float:
    baseline = previous if previous is not None else 0.0
    tolerance = max(abs(expected_delta) * 0.1, 1e-9)
    target_sign = 1 if expected_delta > 0 else -1 if expected_delta < 0 else 0

    def _predicate(value: Optional[float], _raw: Optional[Dict[str, Any]]) -> bool:
        if value is None:
            return False
        delta = value - baseline
        if target_sign > 0:
            return delta >= (expected_delta - tolerance)
        if target_sign < 0:
            return delta <= (expected_delta + tolerance)
        return abs(delta) <= tolerance

    new_value, _ = await tracker.wait_for(symbol, _predicate, timeout)
    if new_value is None:
        raise TimeoutError(f"{venue_name} position unresolved for {symbol}")
    return float(new_value)
async def discover_pairs(lighter: LighterConnector, backpack: BackpackConnector) -> List[Tuple[str, str]]:
    lg_syms = await lighter.list_symbols()
    bp_syms = await backpack.list_symbols()
    idx: Dict[Tuple[str, str, bool], List[str]] = {}
    for s in lg_syms:
        b, q, p = _norm_sym_lg(s)
        idx.setdefault((b, q, p), []).append(s)
        if p:
            idx.setdefault((b, q, False), []).append(s)
        else:
            idx.setdefault((b, q, True), []).append(s)
    matches: List[Tuple[str, str]] = []
    for s in bp_syms:
        b, q, p = _norm_sym_bp(s)
        if q != "USDC" or not p:
            continue
        for key in [(b, q, True), (b, q, False)]:
            cands = idx.get(key)
            if cands:
                matches.append((s, cands[0]))
                break
    return matches


async def pick_size_common(
    backpack: BackpackConnector,
    lighter: LighterConnector,
    bp_sym: str,
    lg_sym: str,
) -> CrossVenueSize:
    await backpack._ensure_markets()
    p_dec_bp, s_dec_bp = await backpack.get_price_size_decimals(bp_sym)
    p_dec_lg, s_dec_lg = await lighter.get_price_size_decimals(lg_sym)

    mi = backpack._market_info.get(bp_sym) or {}
    quantity_filter = (mi.get("filters") or {}).get("quantity") or {}
    min_bp_candidates = [
        quantity_filter.get("minQuantity"),
        quantity_filter.get("minQty"),
        mi.get("minOrderSize"),
    ]
    min_bp_float = 0.0
    for cand in min_bp_candidates:
        val = _try_float(cand)
        if val is not None and val > 0:
            min_bp_float = val
            break
    size_bp_min_i = 1
    if min_bp_float > 0:
        size_bp_min_i = max(1, int(round(min_bp_float * (10 ** s_dec_bp))))

    try:
        size_lg_min_i = await lighter.get_min_order_size_i(lg_sym)
    except Exception:
        size_lg_min_i = 1

    base_bp_min = size_bp_min_i / float(10 ** s_dec_bp)
    base_lg_min = size_lg_min_i / float(10 ** max(s_dec_lg, 0))

    base_size = max(base_bp_min, base_lg_min)
    if base_size <= 0:
        scale_hint = max(s_dec_bp, s_dec_lg, 1)
        base_size = 1 / float(10 ** scale_hint)

    size_bp_i = size_bp_min_i
    size_lg_i = size_lg_min_i
    for _ in range(4):
        size_bp_i = max(size_bp_min_i, int(round(base_size * (10 ** s_dec_bp))))
        size_lg_i = max(size_lg_min_i, int(round(base_size * (10 ** s_dec_lg))))
        base_bp = size_bp_i / float(10 ** s_dec_bp)
        base_lg = size_lg_i / float(10 ** max(s_dec_lg, 0))
        new_base = max(base_bp, base_lg)
        if abs(new_base - base_size) <= 1e-12:
            break
        base_size = new_base

    base_bp = size_bp_i / float(10 ** s_dec_bp)
    base_lg = size_lg_i / float(10 ** max(s_dec_lg, 0))

    if base_bp <= 0 or base_lg <= 0:
        raise RuntimeError(f"invalid common size for {bp_sym}/{lg_sym}")

    return CrossVenueSize(
        backpack=OrderSizeMeta(
            size_i=int(size_bp_i),
            size_float=float(base_bp),
            price_decimals=int(p_dec_bp),
            size_decimals=int(s_dec_bp),
        ),
        lighter=OrderSizeMeta(
            size_i=int(size_lg_i),
            size_float=float(base_lg),
            price_decimals=int(p_dec_lg),
            size_decimals=int(s_dec_lg),
        ),
    )


async def main():
    bundle = load_hedge_config()
    params = bundle.strategy

    lg = LighterConnector(bundle.lighter)
    bp = BackpackConnector(bundle.backpack)
    lg.start()
    bp.start()

    bp_tracker = WSPositionTracker("Backpack")
    lg_tracker = WSPositionTracker("Lighter")

    def _handle_bp_position(payload: Dict[str, Any]) -> None:
        for symbol, value, entry in _iter_backpack_positions(payload):
            bp_tracker.update(symbol, value, entry)

    def _handle_lg_position(payload: Dict[str, Any]) -> None:
        for symbol, value, entry in _iter_lighter_positions(lg, payload):
            lg_tracker.update(symbol, value, entry)

    bp.set_event_handlers(on_position_update=_handle_bp_position)
    lg.set_event_handlers(on_position_update=_handle_lg_position)

    await lg.start_ws_state()
    await bp.start_ws_state([])

    await bootstrap_backpack_positions(bp, bp_tracker)
    await bootstrap_lighter_positions(lg, lg_tracker)

    pairs = await discover_pairs(lg, bp)
    if not pairs:
        print("No matching symbols found between Backpack and Lighter")
        return
    print(f"matched_pairs={len(pairs)} sample={pairs[:5]}")

    last_bp_positions: Dict[str, float] = {}
    last_lg_positions: Dict[str, float] = {}

    try:
        while True:
            bp_sym, lg_sym = random.choice(pairs)
            if not bp_sym.endswith("_PERP"):
                base, quote, _ = _norm_sym_bp(bp_sym)
                perp_candidate = f"{base}_{quote}_PERP"
                if any(p[0] == perp_candidate for p in pairs):
                    bp_sym = perp_candidate
                    lg_sym = next(p[1] for p in pairs if p[0] == bp_sym)

            sizes = await pick_size_common(bp, lg, bp_sym, lg_sym)
            bp_order = sizes.backpack
            lg_order = sizes.lighter

            bid_bp, ask_bp, scale_bp = await bp.get_top_of_book(bp_sym)
            print(
                "cycle pick:"
                f" {bp_sym} ~ {lg_sym} bp_qty={bp_order.size_float} lg_qty={lg_order.size_float}"
                f" tob_bp=({bid_bp},{ask_bp}) scale={scale_bp}"
            )

            prev_bp_val = bp_tracker.snapshot(bp_sym)[0]
            if prev_bp_val is None:
                prev_bp_val = last_bp_positions.get(bp_sym)
            if prev_bp_val is None:
                prev_bp_val = 0.0
                bp_tracker.prime(bp_sym, prev_bp_val, None)
            last_bp_positions[bp_sym] = prev_bp_val

            prev_lg_val = lg_tracker.snapshot(lg_sym)[0]
            if prev_lg_val is None:
                prev_lg_val = last_lg_positions.get(lg_sym)
            if prev_lg_val is None:
                prev_lg_val = 0.0
                lg_tracker.prime(lg_sym, prev_lg_val, None)
            last_lg_positions[lg_sym] = prev_lg_val

            long = bool(random.getrandbits(1))
            bp_direction = 1 if long else -1

            bp_expected_delta = bp_direction * bp_order.size_float
            target_bp_base = prev_bp_val + bp_expected_delta
            current_bp_base = prev_bp_val
            bp_success = False
            reprice_attempts = max(0, params.max_reprice_attempts)
            total_attempts = 1 + reprice_attempts
            wait_per_attempt = min(params.reprice_after_seconds, params.backpack_fill_timeout)
            last_bp_cio = int(time.time() * 1000) % 1_000_000
            cancel_failed = False

            for attempt in range(1, total_attempts + 1):
                if attempt > 1:
                    bid_bp, ask_bp, scale_bp = await bp.get_top_of_book(bp_sym)
                if long:
                    if ask_bp:
                        price_i = max(1, ask_bp - 1)
                        if bid_bp and price_i < bid_bp:
                            price_i = bid_bp
                    else:
                        price_i = bid_bp or scale_bp or 1
                else:
                    if bid_bp:
                        price_i = max(1, bid_bp + 1)
                        if ask_bp and price_i > ask_bp:
                            price_i = ask_bp
                    else:
                        price_i = ask_bp or scale_bp or 1

                last_bp_cio = int(time.time() * 1000) % 1_000_000
                ret_bp, _resp, err_bp = await bp.place_limit(
                    bp_sym,
                    last_bp_cio,
                    bp_order.size_i,
                    price=price_i,
                    is_ask=not long,
                    post_only=False,
                    reduce_only=0,
                )
                order_id_raw = _extract_order_id(ret_bp)
                order_id = order_id_raw
                if order_id_raw and str(order_id_raw).isdigit():
                    order_id = str(int(order_id_raw))
                print(
                    "bp limit placed:"
                    f" sym={bp_sym} side={'Ask' if not long else 'Bid'} err={err_bp}"
                    f" id={order_id_raw} attempt={attempt}/{total_attempts}"
                )
                if err_bp:
                    if order_id is None or attempt >= total_attempts:
                        print("bp limit rejected with no live order; skipping cycle")
                        break
                    await asyncio.sleep(0.5)
                    continue

                try:
                    new_bp_val = await wait_position_delta(
                        bp_tracker,
                        bp_sym,
                        current_bp_base,
                        target_bp_base - current_bp_base,
                        "Backpack",
                        timeout=wait_per_attempt,
                    )
                except TimeoutError:
                    new_bp_val = None

                if new_bp_val is not None:
                    current_bp_base = new_bp_val
                    bp_success = True
                    break

                print(f"bp limit not filled within {wait_per_attempt}s, cancelling to reprice")
                try:
                    if order_id is not None:
                        cancel_ret, _, cancel_err = await bp.cancel_order(order_id, bp_sym)
                        if cancel_err:
                            order_info, order_err = await bp.get_order(bp_sym, order_id=order_id)
                            status = str((order_info or {}).get("status", "")).lower()
                            if order_err in ("not_found", "gone") or status in {
                                "filled",
                                "partiallyfilled",
                                "cancelled",
                                "canceled",
                                "expired",
                            }:
                                current_bp_base = target_bp_base
                                bp_success = True
                                cancel_failed = False
                                break
                            snap_val = bp_tracker.snapshot(bp_sym)[0]
                            if snap_val is not None:
                                current_bp_base = snap_val
                                remaining = target_bp_base - current_bp_base
                                if abs(remaining) <= _delta_tolerance(bp_expected_delta):
                                    bp_success = True
                                    cancel_failed = False
                                    break
                            print(f"bp cancel error: {cancel_err}")
                            cancel_failed = True
                    else:
                        _, _, cancel_err = await bp.cancel_all(bp_sym)
                        if cancel_err:
                            print(f"bp cancel_all error: {cancel_err}")
                            cancel_failed = True
                except Exception as cancel_exc:
                    print(f"bp cancel exception: {cancel_exc}")
                    cancel_failed = True
                await asyncio.sleep(0.1)

                if cancel_failed:
                    break

                if cancel_failed:
                    break

                snap_val = bp_tracker.snapshot(bp_sym)[0]
                if snap_val is not None:
                    current_bp_base = snap_val
                remaining = target_bp_base - current_bp_base
                if abs(remaining) <= _delta_tolerance(bp_expected_delta):
                    bp_success = True
                    break

            if cancel_failed:
                print("bp cancel failed; halting loop to avoid stacking outstanding Backpack orders.")
                break

            if not bp_success:
                print("bp limit attempts exhausted without fill; skipping cycle")
                if not cancel_failed:
                    try:
                        await bp.cancel_all(bp_sym)
                    except Exception as cancel_err:
                        print(f"bp cancel_all error: {cancel_err}")
                await asyncio.sleep(1.0)
                continue

            delta_bp = current_bp_base - prev_bp_val
            print(
                f"bp position ok: prev={prev_bp_val:.8f} -> {current_bp_base:.8f} (Δ={delta_bp:+.8f})"
            )
            prev_bp_val = current_bp_base
            last_bp_positions[bp_sym] = current_bp_base

            lg_expected_delta = -bp_direction * lg_order.size_float
            lg_cio = (last_bp_cio + 1) % 1_000_000
            is_ask = True if long else False
            lg_hedged = False
            ret_lg, _resp_lg, err_lg = await lg.place_market(
                lg_sym,
                lg_cio,
                base_amount=lg_order.size_i,
                is_ask=is_ask,
                reduce_only=0,
            )
            if err_lg:
                print(f"lg market hedge err={err_lg}; retrying once...")
                await asyncio.sleep(1.0)
                ret_lg, _resp_lg, err_lg = await lg.place_market(
                    lg_sym,
                    (lg_cio + 1) % 1_000_000,
                    base_amount=lg_order.size_i,
                    is_ask=is_ask,
                    reduce_only=0,
                )
            print(
                f"lg market hedge placed: sym={lg_sym} side={'Ask' if is_ask else 'Bid'} err={err_lg}"
            )

            if not err_lg:
                try:
                    new_lg_val = await wait_position_delta(
                        lg_tracker,
                        lg_sym,
                        prev_lg_val,
                        lg_expected_delta,
                        "Lighter",
                        timeout=params.lighter_fill_timeout,
                    )
                except TimeoutError as exc:
                    print(f"lg hedge position timeout: {exc}")
                else:
                    delta_lg = new_lg_val - prev_lg_val
                    print(
                        f"lg position ok: prev={prev_lg_val:.8f} -> {new_lg_val:.8f} (Δ={delta_lg:+.8f})"
                    )
                    prev_lg_val = new_lg_val
                    last_lg_positions[lg_sym] = new_lg_val
                    lg_hedged = True
            else:
                print("lg hedge failed; manual check recommended")

            if lg_hedged:
                hold_s = random.randint(params.hold_seconds_min, params.hold_seconds_max)
                print(f"holding for {hold_s}s...")
                await asyncio.sleep(hold_s)
            else:
                print("skip hold – hedge not confirmed, proceed to close")

            bid_bp, ask_bp, scale_close = await bp.get_top_of_book(bp_sym)
            if long:
                price_close = ask_bp or bid_bp or scale_close
                if bid_bp and ask_bp:
                    price_close = max(1, ask_bp)
            else:
                price_close = bid_bp or ask_bp or scale_close
                if bid_bp and ask_bp:
                    price_close = max(1, min(bid_bp, ask_bp - 1))

            bp_cio2 = int(time.time() * 1000) % 1_000_000
            ret_bp_close, _resp_close, err_bp_close = await bp.place_limit(
                bp_sym,
                bp_cio2,
                bp_order.size_i,
                price=price_close,
                is_ask=long,
                post_only=False,
                reduce_only=1,
            )
            print(
                f"bp limit close placed: sym={bp_sym} err={err_bp_close}"
            )
            if err_bp_close:
                await asyncio.sleep(1.0)
                continue

            bp_close_delta = -bp_direction * bp_order.size_float
            try:
                new_bp_val_close = await wait_position_delta(
                    bp_tracker,
                    bp_sym,
                    prev_bp_val,
                    bp_close_delta,
                    "Backpack",
                    timeout=params.backpack_fill_timeout,
                )
            except TimeoutError as exc:
                print(f"bp close timeout: {exc}; cancelling outstanding close")
                try:
                    await bp.cancel_all(bp_sym)
                except Exception as cancel_err:
                    print(f"bp cancel_all error: {cancel_err}")
                await asyncio.sleep(2.0)
                continue

            delta_bp_close = new_bp_val_close - prev_bp_val
            print(
                f"bp close position ok: prev={prev_bp_val:.8f} -> {new_bp_val_close:.8f} (Δ={delta_bp_close:+.8f})"
            )
            prev_bp_val = new_bp_val_close
            last_bp_positions[bp_sym] = new_bp_val_close

            if lg_hedged:
                lg_expected_close = -lg_expected_delta
                lg_cio2 = (bp_cio2 + 1) % 1_000_000
                ret_lg_close, _resp_lg_close, err_lg_close = await lg.place_market(
                    lg_sym,
                    lg_cio2,
                    base_amount=lg_order.size_i,
                    is_ask=not is_ask,
                    reduce_only=1,
                )
                print(
                    f"lg market close placed: sym={lg_sym} err={err_lg_close}"
                )
                if not err_lg_close:
                    try:
                        new_lg_val_close = await wait_position_delta(
                            lg_tracker,
                            lg_sym,
                            prev_lg_val,
                            lg_expected_close,
                            "Lighter",
                            timeout=params.lighter_fill_timeout,
                        )
                    except TimeoutError as exc:
                        print(f"lg close position timeout: {exc}")
                    else:
                        delta_lg_close = new_lg_val_close - prev_lg_val
                        print(
                            f"lg close position ok: prev={prev_lg_val:.8f} -> {new_lg_val_close:.8f} (Δ={delta_lg_close:+.8f})"
                        )
                        prev_lg_val = new_lg_val_close
                        last_lg_positions[lg_sym] = new_lg_val_close
                else:
                    print("lg close hedge failed; please verify manually")
            else:
                print("skip lighter close – no hedge position was opened")

            cool = random.randint(params.cooldown_seconds_min, params.cooldown_seconds_max)
            print(f"cooldown {cool}s...")
            await asyncio.sleep(cool)

    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        try:
            await lg.stop_ws_state()
        except Exception:
            pass
        try:
            await bp.stop_ws_state()
        except Exception:
            pass
        try:
            await bp.close()
        except Exception:
            pass
        try:
            await lg.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
