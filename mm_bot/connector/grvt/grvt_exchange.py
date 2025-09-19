import os
import sys
import time
import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

# Ensure local SDK path is importable (clone to repo root as "grvt-pysdk")
def _ensure_grvt_on_path(root: str):
    sdk_root = os.path.join(root, "grvt-pysdk")
    sdk_src = os.path.join(sdk_root, "src")
    for p in (sdk_src, sdk_root):
        if p not in sys.path:
            sys.path.insert(0, p)

_ensure_grvt_on_path(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

try:
    # Import GRVT SDK modules
    from pysdk.grvt_ccxt_env import GrvtEnv
    from pysdk.grvt_ccxt_pro import GrvtCcxtPro
    from pysdk.grvt_ccxt_ws import GrvtCcxtWS, GrvtWSEndpointType
    from pysdk.grvt_ccxt_types import GrvtOrderSide
except Exception:
    GrvtEnv = None  # type: ignore
    GrvtCcxtPro = None  # type: ignore
    GrvtCcxtWS = None  # type: ignore
    GrvtWSEndpointType = None  # type: ignore
    GrvtOrderSide = None  # type: ignore


@dataclass
class GrvtConfig:
    base_url: str = os.getenv("XTB_GRVT_BASE_URL", "")
    # add more fields as needed (keys file, account info, rpm, etc.)


class GrvtConnector:
    """
    Skeleton connector for GRVT SDK with a Lighter-compatible interface surface.
    Fill in real REST/WS calls using grvt-pysdk.
    """

    def __init__(self, config: Optional[GrvtConfig] = None, debug: bool = False):
        self.config = config or GrvtConfig()
        self.debug = debug

        # SDK clients to be initialized in start()
        self._started = False
        self._rest: Optional[GrvtCcxtPro] = None
        self._ws: Optional[GrvtCcxtWS] = None

        # symbol/market maps
        self._symbol_to_market: Dict[str, int] = {}
        self._market_to_symbol: Dict[int, str] = {}

        # event handlers
        self._on_order_filled = None
        self._on_order_cancelled = None
        self._on_trade = None
        self._on_position_update = None

        # WS state tasks
        self._ws_state_task: Optional[asyncio.Task] = None
        self._ws_state_stop: bool = False
        self._rest_reconcile_task: Optional[asyncio.Task] = None

        # caches
        self._active_orders_by_symbol: Dict[str, Dict[int, Dict[str, Any]]] = {}
        self._positions_by_symbol: Dict[str, Dict[str, Any]] = {}
        self._coi_to_order_id: Dict[int, str] = {}
        self._order_id_to_int: Dict[str, int] = {}
        self._price_decimals_by_symbol: Dict[str, int] = {}
        self._size_decimals_by_symbol: Dict[str, int] = {}
        self._min_size_i_by_symbol: Dict[str, int] = {}

    # lifecycle
    def start(self, core=None):
        if self._started:
            return
        if GrvtCcxtPro is None:
            raise RuntimeError("GRVT SDK not found. Ensure grvt-pysdk is cloned and on sys.path")
        # load keys
        trading_account_id, api_key, priv_key = self._load_keys()
        env_name = os.getenv("XTB_GRVT_ENV", os.getenv("GRVT_ENV", "prod")).lower()
        try:
            env = GrvtEnv(env_name)
        except Exception:
            env = GrvtEnv.TESTNET
        params = {
            "api_key": api_key,
            "trading_account_id": trading_account_id,
            "private_key": priv_key,
        }
        self._rest = GrvtCcxtPro(env=env, parameters=params, order_book_ccxt_format=False)
        self._started = True

    def stop(self, core=None):
        self._started = False

    async def close(self):
        # REST is aiohttp-based; nothing explicit required beyond GC
        self._rest = None
        if self._ws is not None:
            try:
                await self._ws.__aexit__()
            except Exception:
                pass
            self._ws = None

    def set_event_handlers(
        self,
        *,
        on_order_filled: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_order_cancelled: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_trade: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_position_update: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._on_order_filled = on_order_filled
        self._on_order_cancelled = on_order_cancelled
        self._on_trade = on_trade
        self._on_position_update = on_position_update

    async def start_ws_state(self) -> None:
        if self._ws_state_task and not self._ws_state_task.done():
            return
        if self._ws is None:
            # initialize WS client
            trading_account_id, api_key, priv_key = self._load_keys()
            env_name = os.getenv("XTB_GRVT_ENV", os.getenv("GRVT_ENV", "prod")).lower()
            try:
                env = GrvtEnv(env_name)
            except Exception:
                env = GrvtEnv.TESTNET
            self._ws = GrvtCcxtWS(env=env, loop=asyncio.get_running_loop(), parameters={
                "api_key": api_key,
                "trading_account_id": trading_account_id,
                "private_key": priv_key,
            })
            await self._ws.initialize()
            # subscribe to account streams
            await self._ws.subscribe(
                stream="v1.position",
                callback=self._on_ws_position,
                ws_end_point_type=GrvtWSEndpointType.TRADE_DATA,
                params={}
            )
            await self._ws.subscribe(
                stream="v1.order",
                callback=self._on_ws_order,
                ws_end_point_type=GrvtWSEndpointType.TRADE_DATA,
                params={}
            )
            await self._ws.subscribe(
                stream="v1.fill",
                callback=self._on_ws_fill,
                ws_end_point_type=GrvtWSEndpointType.TRADE_DATA,
                params={}
            )
        self._ws_state_stop = False
        self._ws_state_task = asyncio.create_task(self._ws_state_loop(), name="grvt_ws_state")
        # start periodic reconciliation (10s)
        if self._rest_reconcile_task is None or self._rest_reconcile_task.done():
            self._rest_reconcile_task = asyncio.create_task(self._rest_reconcile_loop(), name="grvt_rest_reconcile")

    async def stop_ws_state(self) -> None:
        self._ws_state_stop = True
        if self._ws_state_task and not self._ws_state_task.done():
            self._ws_state_task.cancel()
            try:
                await self._ws_state_task
            except asyncio.CancelledError:
                pass
        self._ws_state_task = None
        if self._rest_reconcile_task and not self._rest_reconcile_task.done():
            self._rest_reconcile_task.cancel()
            try:
                await self._rest_reconcile_task
            except asyncio.CancelledError:
                pass
        self._rest_reconcile_task = None
        if self._ws is not None:
            try:
                await self._ws.__aexit__()
            except Exception:
                pass
            self._ws = None

    async def _ws_state_loop(self):
        # TODO: implement GRVT WS subscription(s) and cache maintenance
        while not self._ws_state_stop:
            await asyncio.sleep(1.0)

    async def _rest_reconcile_loop(self):
        """Every ~10s reconcile WS caches with REST: open orders and positions.
        Keeps _active_orders_by_symbol and _positions_by_symbol in sync with server state.
        """
        while not self._ws_state_stop:
            try:
                if self._rest is None:
                    await asyncio.sleep(10.0)
                    continue
                # Open orders (fetch all)
                try:
                    orders = await self._rest.fetch_open_orders(symbol=None)
                except Exception:
                    orders = []
                by_sym: Dict[str, Dict[int, Dict[str, Any]]] = {}
                for od in orders or []:
                    try:
                        sym = od.get("legs", [{}])[0].get("instrument") if isinstance(od.get("legs"), list) else od.get("instrument")
                        if not sym:
                            continue
                        m = by_sym.get(sym) or {}
                        # normalize and compute order_index mapping
                        order_id = str(od.get("order_id") or od.get("id") or "")
                        if order_id and order_id not in self._order_id_to_int:
                            try:
                                self._order_id_to_int[order_id] = int(order_id, 16) if order_id.startswith("0x") else int(order_id)
                            except Exception:
                                self._order_id_to_int[order_id] = abs(hash(order_id)) & 0x7FFFFFFF
                        oi = self._order_id_to_int.get(order_id, 0)
                        m[oi] = od
                        by_sym[sym] = m
                        # coi mapping (if present)
                        try:
                            coi_s = str(((od.get("metadata") or {}).get("client_order_id") or od.get("client_order_id") or "0"))
                            coi = int(coi_s) if coi_s.isdigit() else 0
                            if coi:
                                self._coi_to_order_id[coi] = order_id
                        except Exception:
                            pass
                    except Exception:
                        continue
                # replace map
                self._active_orders_by_symbol = by_sym
                # Positions
                try:
                    poss = await self._rest.fetch_positions(symbols=[])
                except Exception:
                    poss = []
                if isinstance(poss, list):
                    new_pos: Dict[str, Dict[str, Any]] = {}
                    for p in poss:
                        try:
                            sym = p.get("instrument") or p.get("symbol")
                            if sym:
                                new_pos[sym] = p
                        except Exception:
                            continue
                    self._positions_by_symbol = new_pos
            except asyncio.CancelledError:
                break
            except Exception:
                # swallow errors and continue
                pass
            await asyncio.sleep(10.0)

    # markets
    async def list_symbols(self) -> List[str]:
        await self._ensure_markets()
        return list(self._symbol_to_market.keys())

    async def get_market_id(self, symbol: str) -> int:
        await self._ensure_markets()
        if symbol not in self._symbol_to_market:
            raise ValueError(f"Unknown symbol {symbol}")
        return int(self._symbol_to_market[symbol])

    async def get_price_size_decimals(self, symbol: str) -> Tuple[int, int]:
        await self._ensure_markets()
        return self._price_decimals_by_symbol[symbol], self._size_decimals_by_symbol[symbol]

    async def get_min_order_size_i(self, symbol: str) -> int:
        await self._ensure_markets()
        return self._min_size_i_by_symbol[symbol]

    # order book
    async def get_top_of_book(self, symbol: str) -> Tuple[Optional[int], Optional[int], int]:
        await self._ensure_markets()
        assert self._rest is not None
        # Prefer mini ticker best prices; fall back to order book
        scale = 1
        bid_i = ask_i = None
        try:
            tkr = await self._rest.fetch_mini_ticker(symbol)
            if tkr:
                bp = str(tkr.get("best_bid_price") or "")
                ap = str(tkr.get("best_ask_price") or "")
                if bp:
                    if "." in bp:
                        scale = 10 ** len(bp.split(".", 1)[1])
                    bid_i = int(bp.replace(".", ""))
                if ap:
                    if "." in ap:
                        scale = 10 ** len(ap.split(".", 1)[1])
                    ask_i = int(ap.replace(".", ""))
        except Exception:
            pass
        if bid_i is None or ask_i is None:
            try:
                ob = await self._rest.fetch_order_book(symbol, limit=10)
                if ob and (bid_i is None) and ob.get("bids"):
                    p = str(ob["bids"][0]["price"]) if isinstance(ob["bids"][0], dict) else str(ob["bids"][0][0])
                    if "." in p:
                        scale = 10 ** len(p.split(".", 1)[1])
                    bid_i = int(p.replace(".", ""))
                if ob and (ask_i is None) and ob.get("asks"):
                    p = str(ob["asks"][0]["price"]) if isinstance(ob["asks"][0], dict) else str(ob["asks"][0][0])
                    if "." in p:
                        scale = 10 ** len(p.split(".", 1)[1])
                    ask_i = int(p.replace(".", ""))
            except Exception:
                pass
        return bid_i, ask_i, scale

    # account
    async def get_account_overview(self) -> Dict[str, Any]:
        assert self._rest is not None
        return await self._rest.get_account_summary(type="sub-account")

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        assert self._rest is not None
        # fetch via REST and normalize keys
        orders = await self._rest.fetch_open_orders(symbol=symbol)
        out: List[Dict[str, Any]] = []
        p_dec = None
        s_dec = None
        if symbol:
            p_dec, s_dec = await self.get_price_size_decimals(symbol)
        for od in orders:
            try:
                sym = od.get("legs", [{}])[0].get("instrument") if isinstance(od.get("legs"), list) else od.get("instrument")
                if not sym:
                    sym = symbol
                if s_dec is None:
                    _p_dec, s_dec = await self.get_price_size_decimals(sym)
                leg = od.get("legs", [{}])[0] if isinstance(od.get("legs"), list) else od
                price_s = str(leg.get("limit_price") or leg.get("price") or "0")
                size_s = str(leg.get("size") or leg.get("base_amount") or "0")
                is_buy = bool(leg.get("is_buying_asset") or leg.get("is_buy") or leg.get("side") == "buy")
                coi_s = str(((od.get("metadata") or {}).get("client_order_id") or od.get("client_order_id") or "0"))
                order_id = str(od.get("order_id") or od.get("id") or "")
                if order_id and order_id not in self._order_id_to_int:
                    try:
                        self._order_id_to_int[order_id] = int(order_id, 16) if order_id.startswith("0x") else int(order_id)
                    except Exception:
                        self._order_id_to_int[order_id] = abs(hash(order_id)) & 0x7FFFFFFF
                oi = self._order_id_to_int.get(order_id, 0)
                coi = int(coi_s) if coi_s.isdigit() else 0
                if coi:
                    self._coi_to_order_id[coi] = order_id
                # derive price integer by removing decimal point; scale implicit
                price_i = int(price_s.replace(".", "")) if price_s else 0
                size_i = int(Decimal(size_s) * (10 ** s_dec)) if size_s else 0
                d = dict(od)
                d.update({
                    "symbol": sym,
                    "order_index": oi,
                    "client_order_index": coi,
                    "is_ask": (not is_buy),
                    "price": price_i,
                    "base_amount": size_i,
                })
                out.append(d)
            except Exception:
                continue
        return out

    async def get_positions(self) -> List[Dict[str, Any]]:
        assert self._rest is not None
        poss = await self._rest.fetch_positions(symbols=[])
        out: List[Dict[str, Any]] = []
        for p in poss:
            try:
                sym = p.get("instrument") or p.get("symbol")
                if not sym:
                    continue
                mid = await self.get_market_id(sym)
                # Try typical keys for position size
                raw = p.get("size") or p.get("position_size") or p.get("position") or 0
                posf = float(raw)
                out.append({
                    **p,
                    "symbol": sym,
                    "market_index": int(mid),
                    "position": posf,
                })
            except Exception:
                continue
        return out

    # trading
    async def place_limit(
        self,
        symbol: str,
        client_order_index: int,
        base_amount: int,
        price: int,
        is_ask: bool,
        post_only: bool = False,
        reduce_only: int = 0,
    ):
        await self._ensure_markets()
        assert self._rest is not None
        p_dec, s_dec = await self.get_price_size_decimals(symbol)
        amount = Decimal(base_amount) / Decimal(10 ** s_dec)
        limit_price = Decimal(price) / Decimal(10 ** p_dec)
        side: GrvtOrderSide = "sell" if is_ask else "buy"  # type: ignore
        try:
            ret = await self._rest.create_order(
                symbol=symbol,
                order_type="limit",
                side=side,
                amount=str(amount),
                price=str(limit_price),
                params={
                    "client_order_id": int(client_order_index),
                    "post_only": bool(post_only),
                    "reduce_only": bool(reduce_only),
                },
            )
            # record mapping
            if ret:
                try:
                    oid = str(ret.get("order_id") or "")
                    if oid:
                        self._order_id_to_int[oid] = int(oid, 16) if oid.startswith("0x") else int(oid)
                        self._coi_to_order_id[int(client_order_index)] = oid
                except Exception:
                    pass
            return (None, ret, None)
        except Exception as e:
            return (None, None, str(e))

    async def place_market(
        self,
        symbol: str,
        client_order_index: int,
        base_amount: int,
        is_ask: bool,
        reduce_only: int = 0,
    ):
        await self._ensure_markets()
        assert self._rest is not None
        _p_dec, s_dec = await self.get_price_size_decimals(symbol)
        amount = Decimal(base_amount) / Decimal(10 ** s_dec)
        side: GrvtOrderSide = "sell" if is_ask else "buy"  # type: ignore
        try:
            ret = await self._rest.create_order(
                symbol=symbol,
                order_type="market",
                side=side,
                amount=str(amount),
                params={
                    "client_order_id": int(client_order_index),
                    "reduce_only": bool(reduce_only),
                },
            )
            if ret:
                try:
                    oid = str(ret.get("order_id") or "")
                    if oid:
                        self._order_id_to_int[oid] = int(oid, 16) if oid.startswith("0x") else int(oid)
                        self._coi_to_order_id[int(client_order_index)] = oid
                except Exception:
                    pass
            return (None, ret, None)
        except Exception as e:
            return (None, None, str(e))

    async def cancel_order(self, order_index: int, market_index: Optional[int] = None) -> Any:
        assert self._rest is not None
        # Try to cancel by order_id first; fall back to client_order_id if we have mapping
        oid = None
        # reverse lookup
        for k, v in self._order_id_to_int.items():
            if int(v) == int(order_index):
                oid = k
                break
        if oid:
            return await self._rest.cancel_order(id=str(oid))
        # fallback: if someone passed client order index by mistake
        try:
            coi = int(order_index)
        except Exception:
            coi = 0
        if coi and coi in self._coi_to_order_id:
            return await self._rest.cancel_order(params={"client_order_id": str(coi)})
        # as last resort, attempt cancel using JSON-RPC if WS available
        if self._ws is not None:
            try:
                return await self._ws.rpc_cancel_order(id=str(order_index))
            except Exception:
                pass
        return False

    async def cancel_all(self) -> Any:
        assert self._rest is not None
        return await self._rest.cancel_all_orders()

    async def best_effort_latency_ms(self) -> float:
        t0 = time.perf_counter()
        try:
            # lightweight call
            await self.list_symbols()
        except Exception:
            pass
        return (time.perf_counter() - t0) * 1000.0

    # --------------------- helpers ---------------------
    def _load_keys(self) -> Tuple[str, str, str]:
        path = os.getenv("XTB_GRVT_KEYS_FILE", os.path.abspath(os.path.join(os.getcwd(), "Grvt_key.txt")))
        trading_account_id = ""
        api_key = ""
        priv = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s.lower().startswith("trading account id:"):
                        trading_account_id = s.split(":", 1)[1].strip()
                    elif s.lower().startswith("api key:"):
                        api_key = s.split(":", 1)[1].strip()
                    elif s.lower().startswith("secret private key:"):
                        priv = s.split(":", 1)[1].strip()
        if not trading_account_id or not api_key or not priv:
            raise RuntimeError(f"Missing GRVT credentials in {path}")
        return trading_account_id, api_key, priv

    async def _ensure_markets(self) -> None:
        if self._symbol_to_market:
            return
        assert self._rest is not None
        markets = await self._rest.load_markets()
        if not markets:
            markets = {}
        # create synthetic integer market ids
        idx = 1
        for sym, info in markets.items():
            self._symbol_to_market[sym] = idx
            self._market_to_symbol[idx] = sym
            # decimals
            size_dec = int(info.get("base_decimals", 0) or 0)
            tick_size = str(info.get("tick_size", "0.01") or "0.01")
            if "." in tick_size:
                price_dec = len(tick_size.split(".", 1)[1].rstrip("0"))
            else:
                price_dec = 0
            min_size_str = str(info.get("min_size", "0") or "0")
            try:
                min_size_i = int(Decimal(min_size_str) * (10 ** size_dec))
            except Exception:
                min_size_i = 1
            self._price_decimals_by_symbol[sym] = price_dec
            self._size_decimals_by_symbol[sym] = size_dec
            self._min_size_i_by_symbol[sym] = max(1, min_size_i)
            idx += 1

    # --------------------- WS callbacks ---------------------
    async def _on_ws_position(self, message: Dict[str, Any]) -> None:
        try:
            payload = message.get("payload") or message.get("result") or {}
            # expect list under 'positions' or similar; best-effort
            items = payload.get("positions") or payload.get("result") or []
            if isinstance(items, dict):
                items = [items]
            for pos in items:
                sym = pos.get("instrument") or pos.get("symbol")
                if not sym:
                    continue
                self._positions_by_symbol[sym] = pos
                if self._on_position_update:
                    try:
                        self._on_position_update(pos)
                    except Exception:
                        pass
        except Exception:
            pass

    async def _on_ws_order(self, message: Dict[str, Any]) -> None:
        try:
            payload = message.get("payload") or message.get("result") or {}
            od = payload.get("order") or payload
            if not isinstance(od, dict):
                return
            sym = (od.get("legs", [{}])[0] or {}).get("instrument") if isinstance(od.get("legs"), list) else od.get("instrument")
            if not sym:
                return
            m = self._active_orders_by_symbol.get(sym) or {}
            oid = str(od.get("order_id") or od.get("id") or "")
            if oid and oid not in self._order_id_to_int:
                try:
                    self._order_id_to_int[oid] = int(oid, 16) if oid.startswith("0x") else int(oid)
                except Exception:
                    self._order_id_to_int[oid] = abs(hash(oid)) & 0x7FFFFFFF
            oi = self._order_id_to_int.get(oid, 0)
            status = str(((od.get("state") or {}).get("status")) or od.get("status") or "").upper()
            m[oi] = od
            self._active_orders_by_symbol[sym] = m
            if status == "CANCELLED" and self._on_order_cancelled:
                try:
                    self._on_order_cancelled(od)
                except Exception:
                    pass
            if status == "FILLED" and self._on_order_filled:
                try:
                    self._on_order_filled(od)
                except Exception:
                    pass
        except Exception:
            pass

    async def _on_ws_fill(self, message: Dict[str, Any]) -> None:
        try:
            payload = message.get("payload") or message.get("result") or {}
            t = payload.get("fill") or payload
            if not isinstance(t, dict):
                return
            if self._on_trade:
                try:
                    self._on_trade(t)
                except Exception:
                    pass
        except Exception:
            pass
