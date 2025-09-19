import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple

from mm_bot.strategy.strategy_base import StrategyBase
from mm_bot.connector.interfaces import IConnector


@dataclass
class ASParams:
    gamma: float = 0.02  # risk aversion
    k: float = 1.5       # order arrival intensity parameter
    tau: float = 6.0     # holding horizon (seconds)
    beta: float = 1.4    # inventory penalty scaling
    window_secs: float = 60.0  # volatility estimation window
    min_spread_abs: float = 5.0  # $ minimal total spread floor
    min_spread_bps: Optional[float] = None  # optional bps floor of total spread (e.g., 5.0 -> 5 bps)
    requote_ticks: int = 1  # threshold in integer ticks to trigger quote replace per side
    sigma_ewma_alpha: float = 0.2  # EWMA smoothing for sigma estimation (0-1)
    # per-order size override (in base units); if None, use exchange min size
    size_base: Optional[float] = None
    # inventory guardrails (in base units). When |pos| >= max_position_base, enter cleanup
    max_position_base: Optional[float] = None
    recover_ratio: float = 2.0 / 3.0  # exit cleanup when |pos| <= max * recover_ratio
    # dynamic risk scaling: when |q| grows to max, interpolate gamma/beta towards these caps
    gamma_max: Optional[float] = None  # if None, default 3x gamma
    beta_max: Optional[float] = None   # if None, default 2x beta
    # telemetry
    telemetry_enabled: bool = False
    telemetry_url: Optional[str] = None
    telemetry_interval_secs: int = 30


class AvellanedaStoikovStrategy(StrategyBase):
    """
    Avellaneda–Stoikov market making strategy.

    - Estimates short-term volatility from rolling mid samples
    - Computes reservation price r_t and optimal half-spread delta_t
    - Quotes bid/ask around r_t as post-only orders
    - Cancels prior working orders before placing new quotes (simple replace policy)
    - Inventory from connector positions; basic skew via reservation price
    """

    def __init__(
        self,
        connector: IConnector,
        symbol: Optional[str] = None,
        params: Optional[ASParams] = None,
    ):
        self.log = logging.getLogger("mm_bot.strategy.as_model")
        self.connector = connector
        self.symbol = symbol
        self.params = params or ASParams()

        self._core: Any = None
        self._started = False

        # market/scale
        self._market_id: Optional[int] = None
        self._size_i: Optional[int] = None
        self._size_scale: int = 1

        # recent mid samples (integer price, ms timestamp)
        self._mids: Deque[Tuple[float, int]] = deque(maxlen=600)
        self._ewma_var: Optional[float] = None

        # track our last client_order_indices for bid/ask
        self._last_cio_bid: Optional[int] = None
        self._last_cio_ask: Optional[int] = None
        # our own live client_order_indices tracked per side
        self._our_cois_bid: set[int] = set()
        self._our_cois_ask: set[int] = set()

        # one-time cleanup
        self._did_initial_cleanup = False
        # inventory control state: None | 'long_reduce' | 'short_reduce'
        self._inv_mode: Optional[str] = None
        # telemetry task
        self._telemetry_task: Optional[asyncio.Task] = None

    def start(self, core: Any) -> None:
        self._core = core
        self._started = True
        # ensure connector emits events if needed (not strictly required here)
        self.connector.set_event_handlers()
        # start telemetry if configured
        if getattr(self.params, "telemetry_enabled", False) and getattr(self.params, "telemetry_url", None):
            try:
                self._telemetry_task = asyncio.create_task(self._telemetry_loop())
            except Exception:
                self._telemetry_task = None

    def stop(self) -> None:
        self._started = False
        if self._telemetry_task is not None:
            try:
                self._telemetry_task.cancel()
            except Exception:
                pass
            self._telemetry_task = None

    # -------------------- helpers --------------------
    async def _ensure_ready(self) -> None:
        if self._market_id is not None and self.symbol is not None and self._size_i is not None:
            return
        await self.connector.start_ws_state()
        if self.symbol is None:
            syms = []
            try:
                syms = await self.connector.list_symbols()
            except Exception:
                syms = []
            if syms:
                sym = next((s for s in syms if str(s).upper().startswith("BTC")), syms[0])
            else:
                raise RuntimeError("No symbols available from connector; please set symbol explicitly")
            self.symbol = sym
        self._market_id = await self.connector.get_market_id(self.symbol)
        # compute scales and size
        _p_dec, s_dec = await self.connector.get_price_size_decimals(self.symbol)
        self._size_scale = 10 ** int(s_dec)
        min_size_i = max(1, int(await self.connector.get_min_order_size_i(self.symbol)))
        # choose order size: user override or min size
        if getattr(self.params, "size_base", None):
            want_i = int(round(float(self.params.size_base) * self._size_scale))
            self._size_i = max(min_size_i, want_i)
        else:
            self._size_i = min_size_i
        self.log.info(
            f"AS ready: symbol={self.symbol} market_id={self._market_id} size_i(use)={self._size_i} size_i(min)={min_size_i} params={self.params}"
        )

    async def _current_mid_and_scale(self) -> Tuple[Optional[int], Optional[int], int]:
        bid, ask, scale = await self.connector.get_top_of_book(self.symbol)
        if bid is None or ask is None:
            return None, None, scale
        mid = (bid + ask) // 2
        return bid, ask, scale

    def _update_vol_window(self, mid_i: int, now_ms: int) -> None:
        # drop old samples beyond window
        cutoff = now_ms - int(self.params.window_secs * 1000)
        self._mids.append((float(mid_i), now_ms))
        while self._mids and self._mids[0][1] < cutoff:
            self._mids.popleft()
        # update EWMA variance on new sample using normalized diffs
        if len(self._mids) >= 2:
            prev = self._mids[-2]
            cur = self._mids[-1]
            dprice = cur[0] - prev[0]
            dt = max(1e-3, (cur[1] - prev[1]) / 1000.0)
            norm = float(dprice) / (dt ** 0.5)
            alpha = max(0.0, min(1.0, float(getattr(self.params, "sigma_ewma_alpha", 0.2))))
            if self._ewma_var is None:
                self._ewma_var = max(0.0, norm * norm)
            else:
                self._ewma_var = alpha * (norm * norm) + (1.0 - alpha) * float(self._ewma_var)

    def _sigma_abs_per_sec(self) -> float:
        # EWMA of normalized diffs variance
        try:
            v = float(self._ewma_var or 0.0)
            return math.sqrt(v)
        except Exception:
            return 0.0

    async def _find_open_for_side(self, is_ask: bool) -> List[Dict[str, Any]]:
        """Return list of our open orders for the side (best-effort).
        We identify by symbol and side; connector returns 'is_ask' and integer 'price'.
        """
        orders = await self.connector.get_open_orders(self.symbol)
        out: List[Dict[str, Any]] = []
        allowed = self._our_cois_ask if is_ask else self._our_cois_bid
        for o in orders:
            try:
                if bool(o.get("is_ask")) != bool(is_ask):
                    continue
                coi = o.get("client_order_index")
                coi = int(coi) if coi is not None else None
                # if we have tracked COIs, only consider our own
                if allowed and (coi not in allowed):
                    continue
                out.append(o)
            except Exception:
                continue
        # sort so that index 0 is top-of-queue for that side
        if is_ask:
            # best ask is lowest price
            out.sort(key=lambda x: int(x.get("price", 0)))
        else:
            # best bid is highest price
            out.sort(key=lambda x: int(x.get("price", 0)), reverse=True)
        return out

    async def _update_quote_side(self, target_price_i: int, is_ask: bool, *, reduce_only: bool) -> Optional[int]:
        """Conditionally update a side with double-buffering and side limits.
        - If existing top order price within requote_ticks, keep; else place new then cancel old.
        - Limit max 2 live orders per side.
        Returns client_order_index of placed order or None if unchanged/failed.
        """
        requote_ticks = max(0, int(getattr(self.params, "requote_ticks", 1)))
        existing = await self._find_open_for_side(is_ask)
        # if have existing, check top-of-queue price
        if existing:
            try:
                cur_price = int(existing[0].get("price", 0))
            except Exception:
                cur_price = 0
            if abs(cur_price - int(target_price_i)) < requote_ticks:
                return None  # within threshold, skip replace
        # enforce side limit: if already >=2, cancel the farthest one first
        if len(existing) >= 2:
            try:
                far = existing[-1]
                oi = int(far.get("order_index"))
                await self.connector.cancel_order(oi)
                await asyncio.sleep(0)  # yield
            except Exception:
                pass
        # double-buffer: place new first, then cancel old best if exists
        cio = await self._place_post_only(int(target_price_i), is_ask=is_ask, reduce_only=reduce_only)
        if cio is None:
            return None
        # cancel previous best, if any
        if existing:
            try:
                oi = int(existing[0].get("order_index", 0) or 0)
                if oi:
                    await self.connector.cancel_order(oi)
                    # best-effort remove from our tracked set
                    try:
                        coi0 = int(existing[0].get("client_order_index", 0) or 0)
                        if coi0:
                            if is_ask:
                                self._our_cois_ask.discard(coi0)
                            else:
                                self._our_cois_bid.discard(coi0)
                    except Exception:
                        pass
            except Exception:
                pass
        return cio

    async def _cancel_prev_quotes(self) -> None:
        opens = await self.connector.get_open_orders(self.symbol)
        if not opens:
            return
        for o in list(opens):
            try:
                oi = int(o.get("order_index"))
            except Exception:
                continue
            try:
                await self.connector.cancel_order(oi, market_index=self._market_id)
            except Exception:
                pass
        # wait for cache to clear
        for _ in range(30):
            opens2 = await self.connector.get_open_orders(self.symbol)
            if len(opens2) == 0:
                break
            await asyncio.sleep(0.1)

    async def _place_post_only(self, price_i: int, is_ask: bool, *, reduce_only: bool = False) -> Optional[int]:
        cio = int(time.time() * 1000) % 1_000_000
        try:
            _tx, ret, err = await self.connector.place_limit(
                self.symbol,
                cio,
                self._size_i,
                price=price_i,
                is_ask=is_ask,
                post_only=True,
                reduce_only=1 if reduce_only else 0,
            )
            if err:
                self.log.warning(f"place {'ask' if is_ask else 'bid'} rejected: {err}")
                return None
            # record this COI as ours
            try:
                if is_ask:
                    self._our_cois_ask.add(int(cio))
                else:
                    self._our_cois_bid.add(int(cio))
            except Exception:
                pass
            return cio
        except Exception as e:
            self.log.warning(f"place {'ask' if is_ask else 'bid'} error: {e}")
            return None

    async def _inventory_base(self) -> float:
        # read from connector positions
        try:
            positions = await self.connector.get_positions()
            pos = next((p for p in positions if int(p.get("market_index", p.get("market_id", -1))) == int(self._market_id)), None)
            if pos is None:
                return 0.0
            raw = pos.get("position")
            return float(raw) if raw is not None else 0.0
        except Exception:
            return 0.0

    # -------------------- main tick --------------------
    async def on_tick(self, now_ms: float):
        if not self._started:
            return
        await self._ensure_ready()

        # one-time cleanup of legacy orders
        if not self._did_initial_cleanup:
            try:
                await self.connector.cancel_all()
            except Exception:
                pass
            for _ in range(50):
                opens0 = await self.connector.get_open_orders(self.symbol)
                if len(opens0) == 0:
                    break
                await asyncio.sleep(0.1)
            self._did_initial_cleanup = True

        bid, ask, scale = await self.connector.get_top_of_book(self.symbol)
        if bid is None or ask is None or scale <= 0:
            return
        mid_i = (bid + ask) // 2
        self._update_vol_window(mid_i, int(now_ms))

        sigma_abs = self._sigma_abs_per_sec()  # integer price units per sqrt(sec)
        # dynamic gamma/beta by inventory state
        base_gamma = float(self.params.gamma)
        base_beta = float(self.params.beta)
        gamma_cap = float(self.params.gamma_max) if getattr(self.params, "gamma_max", None) else base_gamma * 3.0
        beta_cap = float(self.params.beta_max) if getattr(self.params, "beta_max", None) else base_beta * 2.0
        max_pos = float(getattr(self.params, "max_position_base", 0.0) or 0.0)
        inv_ratio = min(1.0, abs(await self._inventory_base()) / max_pos) if max_pos > 0 else 0.0
        gamma = base_gamma + (gamma_cap - base_gamma) * inv_ratio
        beta = base_beta + (beta_cap - base_beta) * inv_ratio
        k = max(1e-9, self.params.k)
        tau = max(1e-6, self.params.tau)

        # reservation price and optimal half-spread (Avellaneda–Stoikov)
        # r = s - beta * q * gamma * sigma^2 * tau (beta allows tuning inventory pressure)
        q = await self._inventory_base()
        r_i = mid_i - int(round(beta * q * (sigma_abs ** 2) * tau))

        # half-spread: delta = (gamma * sigma^2 * tau)/2 + (1/gamma) * ln(1 + gamma/k)
        delta_abs = 0.5 * gamma * (sigma_abs ** 2) * tau + (1.0 / gamma) * math.log(1.0 + gamma / k)
        # enforce minimal total spread floors
        min_half_abs = max(1, int(round((self.params.min_spread_abs / 2.0) * scale)))
        min_half_bps = 0
        try:
            bps = float(getattr(self.params, "min_spread_bps", 0.0) or 0.0)
            if bps > 0:
                # total spread bps of mid; half-spread is half of it
                min_half_bps = int(round((bps * 0.0001) * (mid_i) / 2.0))
        except Exception:
            min_half_bps = 0
        min_half = max(min_half_abs, min_half_bps)
        delta_i = max(min_half, int(round(delta_abs)))  # delta_abs already in integer units due to sigma_abs choice

        bid_i = max(1, r_i - delta_i)
        ask_i = max(bid_i + 1, r_i + delta_i)

        # Safety: do not cross best prices
        if bid is not None:
            bid_i = min(bid_i, bid - 1)
        if ask is not None:
            ask_i = max(ask_i, ask + 1)

        # inventory control: enter/exit cleanup state
        max_pos = float(getattr(self.params, "max_position_base", 0.0) or 0.0)
        recover_ratio = float(getattr(self.params, "recover_ratio", 2.0/3.0) or (2.0/3.0))
        if max_pos > 0.0:
            if self._inv_mode is None:
                if q >= max_pos:
                    self._inv_mode = "long_reduce"
                elif q <= -max_pos:
                    self._inv_mode = "short_reduce"
            else:
                if abs(q) <= max_pos * recover_ratio:
                    self._inv_mode = None

        mode = self._inv_mode
        self.log.info(
            f"AS quote: mid_i={mid_i} sigma_abs={sigma_abs:.1f} q={q:.6f} r_i={r_i} delta_i={delta_i} -> bid_i={bid_i} ask_i={ask_i} inv_mode={mode}"
        )

        # replace quotes according to mode, with conditional updates + double-buffer
        cio_bid = cio_ask = None
        if mode == "long_reduce":
            # only sell to reduce long; mark reduce_only
            cio_ask = await self._update_quote_side(ask_i, is_ask=True, reduce_only=True)
        elif mode == "short_reduce":
            # only buy to reduce short; mark reduce_only
            cio_bid = await self._update_quote_side(bid_i, is_ask=False, reduce_only=True)
        else:
            cio_bid = await self._update_quote_side(bid_i, is_ask=False, reduce_only=False)
            cio_ask = await self._update_quote_side(ask_i, is_ask=True, reduce_only=False)
        self._last_cio_bid, self._last_cio_ask = cio_bid, cio_ask

    async def _collect_telemetry(self) -> Dict[str, Any]:
        import json  # noqa: F401
        # latency
        try:
            lat_ms = await self.connector.best_effort_latency_ms()
        except Exception:
            lat_ms = None
        # top of book
        try:
            bid, ask, scale = await self.connector.get_top_of_book(self.symbol)
        except Exception:
            bid = ask = scale = None
        # position and direction
        q = await self._inventory_base()
        direction = ("long" if q > 0 else ("short" if q < 0 else None))
        max_pos = float(getattr(self.params, "max_position_base", 0.0) or 0.0)
        # pnl/equity via account overview with fallbacks
        acct: Dict[str, Any] = {}
        pnl = None
        balance_total = None
        try:
            ov = await self.connector.get_account_overview()
            if isinstance(ov, dict):
                acct = ov
                # unrealized pnl
                for k in ("unrealized_pnl", "unrealizedPnl", "upnl", "u_pnl", "pnl"):
                    v = ov.get(k)
                    if v is not None:
                        try:
                            pnl = float(v)
                            break
                        except Exception:
                            try:
                                pnl = float(str(v))
                                break
                            except Exception:
                                pass
                # equity/balance
                for k in (
                    "collateral",
                    "total_equity",
                    "equity",
                    "total_asset_value",
                    "cross_asset_value",
                    "balance",
                    "totalBalance",
                    "account_equity",
                    "available_balance",
                ):
                    v = ov.get(k)
                    if v is not None:
                        try:
                            balance_total = float(v)
                            break
                        except Exception:
                            try:
                                balance_total = float(str(v))
                                break
                            except Exception:
                                pass
        except Exception:
            pass
        # fallback pnl from positions
        if pnl is None:
            try:
                poss = await self.connector.get_positions()
                total_upnl = 0.0
                has = False
                for p in poss:
                    for k in ("unrealized_pnl", "unrealizedPnl", "upnl", "u_pnl"):
                        if p.get(k) is not None:
                            try:
                                total_upnl += float(p.get(k))
                                has = True
                                break
                            except Exception:
                                pass
                pnl = total_upnl if has else None
            except Exception:
                pass
        # telemetry payload (align to trend_ladder keys where possible)
        return {
            "ts": time.time(),
            "symbol": self.symbol,
            "direction": direction,
            "position_base": q,
            "max_position_base": max_pos or None,
            "inv_mode": self._inv_mode,
            "tp_active": 0,
            "size_i": self._size_i,
            "latency_ms": lat_ms,
            "pnl_unrealized": pnl,
            "balance_total": balance_total,
            "tob": {"bid_i": bid, "ask_i": ask, "scale": scale},
            "account": {"overview": acct},
        }

    async def _telemetry_loop(self) -> None:
        import urllib.request
        import json
        url = str(getattr(self.params, "telemetry_url", ""))
        interval = max(5, int(getattr(self.params, "telemetry_interval_secs", 30) or 30))
        while self._started and url:
            try:
                payload = await self._collect_telemetry()
                body = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
                await asyncio.to_thread(urllib.request.urlopen, req, timeout=5)
            except Exception:
                pass
            await asyncio.sleep(interval)
