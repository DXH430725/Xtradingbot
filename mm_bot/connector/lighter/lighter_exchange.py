import asyncio
import json
import os
import sys
import time
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple, Callable

from mm_bot.utils.throttler import RateLimiter, lighter_default_weights
from .lighter_auth import load_keys_from_file
from mm_bot.connector.base import BaseConnector
from mm_bot.execution.orders import OrderState, TrackingLimitOrder, TrackingMarketOrder


def _ensure_lighter_on_path(root: str):
    sdk_path = os.path.join(root, "lighter-python")
    if sdk_path not in sys.path:
        sys.path.insert(0, sdk_path)


_ensure_lighter_on_path(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import lighter  # type: ignore  # noqa: E402
from eth_account import Account  # noqa: E402
import websockets  # type: ignore


@dataclass
class LighterConfig:
    base_url: str = os.getenv("XTB_LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")
    keys_file: str = os.getenv("XTB_LIGHTER_KEYS_FILE", os.path.abspath(os.path.join(os.getcwd(), "Lighter_key.txt")))
    account_index: Optional[int] = (
        int(os.getenv("XTB_LIGHTER_ACCOUNT_INDEX", "-1")) if os.getenv("XTB_LIGHTER_ACCOUNT_INDEX") else None
    )
    rpm: int = int(os.getenv("XTB_LIGHTER_RPM", "4000"))  # 60 for standard, 4000 for premium


class LighterConnector(BaseConnector):
    def __init__(self, config: Optional[LighterConfig] = None, debug: bool = False):
        super().__init__("lighter", debug=debug)
        self.config = config or LighterConfig()
        self.debug = debug
        self.log = logging.getLogger("mm_bot.connector.lighter")

        self.api_client: Optional[lighter.ApiClient] = None
        self.account_api: Optional[lighter.AccountApi] = None
        self.order_api: Optional[lighter.OrderApi] = None
        self.tx_api: Optional[lighter.TransactionApi] = None
        self.signer: Optional[lighter.SignerClient] = None

        self._throttler = RateLimiter(capacity_per_minute=self.config.rpm, weights=lighter_default_weights())
        self._ws: Optional[Any] = None
        self._started = False

        self._account_index: Optional[int] = self.config.account_index
        self._api_key_index: Optional[int] = None
        self._api_priv: Optional[str] = None
        self._eth_priv: Optional[str] = None

        self._symbol_to_market: Dict[str, int] = {}
        self._market_to_symbol: Dict[int, str] = {}

        # simple event hooks (strategy can subscribe)
        self._on_order_book_update: Optional[Callable[[str, Dict[str, Any]], None]] = None
        self._on_account_update: Optional[Callable[[str, Dict[str, Any]], None]] = None
        self._on_order_filled: Optional[Callable[[Dict[str, Any]], None]] = None
        self._on_order_cancelled: Optional[Callable[[Dict[str, Any]], None]] = None
        self._on_trade: Optional[Callable[[Dict[str, Any]], None]] = None
        self._on_position_update: Optional[Callable[[Dict[str, Any]], None]] = None

        # in-memory WS caches
        # positions by market_index
        self._positions_by_market: Dict[int, Dict[str, Any]] = {}
        # active orders by market_index: order_index -> order dict
        self._active_orders_by_market: Dict[int, Dict[int, Dict[str, Any]]] = {}
        # map client_order_index -> order_index for convenience
        self._order_index_by_coi: Dict[int, int] = {}
        # recent trades by market
        self._trades_by_market: Dict[int, Deque[Dict[str, Any]]] = {}
        self._trades_maxlen: int = 1000
        # finalized orders set to avoid duplicate events
        self._finalized_order_indices: set[int] = set()

        # inflight and account snapshots for account WS handler safety
        self._inflight_by_coi: Dict[int, Dict[str, Any]] = {}
        self._inflight_by_idx: Dict[int, Dict[str, Any]] = {}
        self._account_positions: Dict[int, Dict[str, Any]] = {}

        # ws state channel
        self._ws_state_task: Optional[asyncio.Task] = None
        self._ws_state_stop: bool = False
        # periodic REST reconciliation of active orders
        self._rest_reconcile_task: Optional[asyncio.Task] = None

    

    # lifecycle -----------------------------------------------------------------
    def start(self, core=None):
        if self._started:
            return
        # load keys
        api_key_index, api_priv, _api_pub, eth_priv = load_keys_from_file(self.config.keys_file)
        if api_priv is None or api_key_index is None:
            raise RuntimeError(f"Missing keys; check {self.config.keys_file}")

        self._api_key_index = api_key_index
        self._api_priv = api_priv
        self._eth_priv = eth_priv

        # APIs
        self.api_client = lighter.ApiClient(configuration=lighter.Configuration(host=self.config.base_url))
        self.account_api = lighter.AccountApi(self.api_client)
        self.order_api = lighter.OrderApi(self.api_client)
        self.tx_api = lighter.TransactionApi(self.api_client)

        self._started = True

    def stop(self, core=None):
        self._started = False

    async def close(self):
        if self.api_client is not None:
            await self.api_client.close()
            self.api_client = None

    # helpers -------------------------------------------------------------------
    async def _ensure_account_index(self) -> int:
        if self._account_index is not None:
            return int(self._account_index)
        if not self._eth_priv:
            raise RuntimeError("Cannot derive account index: missing eth private key")
        acct = Account.from_key(self._eth_priv)
        l1 = acct.address
        await self._throttler.acquire("/api/v1/accountsByL1Address")
        sub = await self.account_api.accounts_by_l1_address(l1)
        accounts = sub.sub_accounts if sub and sub.sub_accounts else []
        if not accounts:
            raise RuntimeError(f"No accounts found for {l1}")
        idx = int(accounts[0].index)
        self._account_index = idx
        return idx

    async def _ensure_signer(self):
        if self.signer is not None:
            return
        account_index = await self._ensure_account_index()
        self.signer = lighter.SignerClient(
            url=self.config.base_url,
            private_key=self._api_priv,
            api_key_index=int(self._api_key_index or 0),
            account_index=account_index,
        )

    async def _ensure_markets(self):
        if self._symbol_to_market:
            return
        await self._throttler.acquire("/api/v1/orderBooks")
        ob = await self.order_api.order_books()
        for entry in ob.order_books:
            self._symbol_to_market[entry.symbol] = int(entry.market_id)
            self._market_to_symbol[int(entry.market_id)] = entry.symbol

    async def list_symbols(self) -> List[str]:
        await self._ensure_markets()
        return list(self._symbol_to_market.keys())

    async def get_price_size_decimals(self, symbol: str) -> Tuple[int, int]:
        """Return (price_decimals, size_decimals) for a given symbol."""
        await self._ensure_markets()
        mid = await self.get_market_id(symbol)
        await self._throttler.acquire("/api/v1/orderBooks")
        ob_list = await self.order_api.order_books()
        entry = next((e for e in ob_list.order_books if int(e.market_id) == mid), None)
        if not entry:
            raise RuntimeError("market scales not found")
        return int(entry.supported_price_decimals), int(entry.supported_size_decimals)

    async def get_min_order_size_i(self, symbol: str) -> int:
        """Return minimal base size in integer units for a symbol."""
        await self._ensure_markets()
        mid = await self.get_market_id(symbol)
        await self._throttler.acquire("/api/v1/orderBooks")
        ob_list = await self.order_api.order_books()
        entry = next((e for e in ob_list.order_books if int(e.market_id) == mid), None)
        if not entry:
            raise RuntimeError("Market entry not found")
        size_dec = int(entry.supported_size_decimals)
        try:
            min_size = float(entry.min_base_amount)
        except Exception:
            min_size = float(str(entry.min_base_amount)) if entry.min_base_amount is not None else 0.0
        return max(1, int(round(min_size * (10 ** size_dec))))

    # public / rest --------------------------------------------------------------
    async def get_market_id(self, symbol: str) -> int:
        await self._ensure_markets()
        if symbol not in self._symbol_to_market:
            raise ValueError(f"Unknown symbol {symbol}")
        return self._symbol_to_market[symbol]

    async def best_effort_latency_ms(self) -> float:
        t0 = time.perf_counter()
        await self._throttler.acquire("/")
        resp = await lighter.RootApi(self.api_client).status()
        _ = getattr(resp, "code", None)
        return (time.perf_counter() - t0) * 1000.0

    # account data ---------------------------------------------------------------
    async def get_account_overview(self) -> Dict[str, Any]:
        idx = await self._ensure_account_index()
        await self._throttler.acquire("/api/v1/account")
        # account(by, value): by="index" and value is string index
        detail = await self.account_api.account(by="index", value=str(idx))
        return detail.to_dict()

    # websocket -----------------------------------------------------------------
    def _mk_ws(self, market_ids: Optional[List[int]] = None, accounts: Optional[List[int]] = None, host: Optional[str] = None):
        from .lighter_ws import LighterWS
        return LighterWS(
            order_book_ids=market_ids,
            account_ids=accounts,
            host=host or self.config.base_url.replace("https://", ""),
            on_order_book_update=self._on_order_book_update,
            on_account_update=self._handle_account_update,
        )

    async def start_ws_order_book(self, symbols: List[str], on_update=None):
        ids = [await self.get_market_id(s) for s in symbols]
        self._on_order_book_update = on_update
        self._ws = self._mk_ws(market_ids=ids, accounts=None)
        self._ws.start()
        return ids

    async def start_ws_account(self, on_update=None):
        idx = await self._ensure_account_index()
        self._on_account_update = on_update
        self._ws = self._mk_ws(market_ids=None, accounts=[idx])
        self._ws.start()
        return idx

    async def stop_ws(self):
        if self._ws is not None:
            await self._ws.stop()
            self._ws = None

    # ----------------------- WS full-state maintenance ------------------------
    def set_event_handlers(
        self,
        *,
        on_order_filled: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_order_cancelled: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_trade: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_position_update: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        super().set_event_handlers(
            on_order_filled=on_order_filled,
            on_order_cancelled=on_order_cancelled,
            on_trade=on_trade,
            on_position_update=on_position_update,
        )

    async def start_ws_state(self) -> None:
        if self._ws_state_task and not self._ws_state_task.done():
            return
        self._ws_state_stop = False
        self._ws_state_task = asyncio.create_task(self._ws_state_loop(), name="lighter_ws_state")
        # start periodic reconciliation
        if self._rest_reconcile_task is None or self._rest_reconcile_task.done():
            self._rest_reconcile_task = asyncio.create_task(self._rest_reconcile_loop(), name="lighter_rest_reconcile")

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

    async def _ws_state_loop(self):
        # reconnect loop
        while not self._ws_state_stop:
            try:
                await self._run_state_ws_once()
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(1.0)

    async def _rest_reconcile_loop(self):
        """Every 60s reconcile WS open-order cache with REST accountActiveOrders.
        Only checks markets that currently have WS-known open orders to limit weight.
        """
        while not self._ws_state_stop:
            try:
                # collect markets to check
                mids = list(self._active_orders_by_market.keys())
                if not mids:
                    await asyncio.sleep(60.0)
                    continue
                idx = await self._ensure_account_index()
                for mid in list(mids):
                    try:
                        await self._throttler.acquire("/api/v1/accountActiveOrders")
                        rest_orders = await self.order_api.account_active_orders(int(idx), int(mid))
                        # extract list
                        rest_list = []
                        try:
                            rest_list = list(getattr(rest_orders, "orders", []) or [])
                        except Exception:
                            rest_list = []
                        rest_by_oi: Dict[int, Dict[str, Any]] = {}
                        for od in rest_list:
                            try:
                                if hasattr(od, "to_dict"):
                                    d = od.to_dict()
                                elif isinstance(od, dict):
                                    d = dict(od)
                                else:
                                    d = {}
                                oi = int(d.get("order_index"))
                                rest_by_oi[oi] = d
                            except Exception:
                                continue
                        ws_map = dict(self._active_orders_by_market.get(int(mid), {}))
                        # remove items not in REST
                        removed = 0
                        for oi in list(ws_map.keys()):
                            if oi not in rest_by_oi:
                                ws_map.pop(oi, None)
                                removed += 1
                        # add items present in REST but missing in WS (optional)
                        added = 0
                        for oi, d in rest_by_oi.items():
                            if oi not in ws_map:
                                ws_map[oi] = d
                                added += 1
                        if ws_map:
                            self._active_orders_by_market[int(mid)] = ws_map
                        else:
                            self._active_orders_by_market.pop(int(mid), None)
                        # update coi map for convenience
                        for oi, d in ws_map.items():
                            try:
                                coi = int(d.get("client_order_index", 0) or 0)
                                if coi:
                                    self._order_index_by_coi[coi] = oi
                            except Exception:
                                pass
                        # optional light logging: only if changed
                        if removed or added:
                            # avoid dependency on global logger inside connector; print minimal info
                            pass
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # ignore and continue next market
                        continue
                await asyncio.sleep(60.0)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(60.0)

    async def _run_state_ws_once(self):
        await self._ensure_signer()
        account_index = await self._ensure_account_index()
        # connect
        uri = self.config.base_url.replace("https://", "wss://")
        if not uri.endswith("/stream"):
            uri = uri.rstrip("/") + "/stream"
        async with websockets.connect(uri) as ws:
            # wait for connected
            try:
                await asyncio.wait_for(ws.recv(), timeout=2.0)
            except Exception:
                pass
            # subscribe channels with auth
            auth, _ = self.signer.create_auth_token_with_expiry()
            subs = [
                {"type": "subscribe", "channel": f"account_all_orders/{account_index}", "auth": auth},
                {"type": "subscribe", "channel": f"account_all_trades/{account_index}", "auth": auth},
                {"type": "subscribe", "channel": f"account_all_positions/{account_index}", "auth": auth},
            ]
            for p in subs:
                await ws.send(json.dumps(p))

            # consume loop
            while not self._ws_state_stop:
                raw = await ws.recv()
                if isinstance(raw, (bytes, bytearray)):
                    continue
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                self._handle_state_message(data)

    def _handle_state_message(self, data: Dict[str, Any]):
        # update orders
        if "orders" in data and isinstance(data["orders"], dict):
            self._apply_orders_update(data["orders"])
        # update trades
        if "trades" in data and isinstance(data["trades"], dict):
            self._apply_trades_update(data["trades"])
        # update positions
        if "positions" in data and isinstance(data["positions"], dict):
            self._apply_positions_update(data["positions"])

    def _apply_orders_update(self, orders_dict: Dict[str, Any]):
        # orders_dict: { "{MARKET_INDEX}": [Order, ...] }
        for k, arr in orders_dict.items():
            try:
                mid = int(k)
            except Exception:
                try:
                    mid = int(str(k))
                except Exception:
                    continue
            # merge incrementally: start from existing open map
            curr_open: Dict[int, Dict[str, Any]] = dict(self._active_orders_by_market.get(mid, {}))
            if isinstance(arr, list):
                for od in arr:
                    if not isinstance(od, dict):
                        continue
                    status = str(od.get("status", ""))
                    oi = int(od.get("order_index", 0) or 0)
                    coi = int(od.get("client_order_index", 0) or 0)
                    if oi <= 0:
                        continue
                    symbol = self._market_to_symbol.get(mid)
                    is_ask = od.get("is_ask")
                    if isinstance(is_ask, str):
                        is_ask = is_ask.lower() in {"1", "true", "ask", "sell"}
                    elif isinstance(is_ask, (int, float)):
                        is_ask = bool(is_ask)
                    # terminal -> remove and fire event once
                    norm = status.strip().lower()
                    is_terminal = (
                        norm.startswith("canceled")
                        or norm == "filled"
                        or norm.startswith("expired")
                        or norm.startswith("rejected")
                        or norm.startswith("failed")
                    )
                    if is_terminal:
                        if oi in curr_open:
                            curr_open.pop(oi, None)
                        if coi:
                            self._order_index_by_coi[coi] = oi
                        if oi not in self._finalized_order_indices:
                            info = dict(od)
                            if norm.startswith("canceled") or norm.startswith("expired") or norm.startswith("rejected") or norm.startswith("failed"):
                                self._update_order_state(
                                    client_order_id=coi,
                                    exchange_order_id=str(oi),
                                    status=norm,
                                    symbol=symbol,
                                    is_ask=is_ask if isinstance(is_ask, bool) else None,
                                    info=info,
                                )
                            else:
                                self._update_order_state(
                                    client_order_id=coi,
                                    exchange_order_id=str(oi),
                                    status=norm,
                                    symbol=symbol,
                                    is_ask=is_ask if isinstance(is_ask, bool) else None,
                                    info=info,
                                )
                            self._finalized_order_indices.add(oi)
                        continue
                    # open/pending -> upsert
                    curr_open[oi] = od
                    if coi:
                        self._order_index_by_coi[coi] = oi
                        info = self._inflight_by_coi.get(int(coi))
                        if info is not None:
                            self._inflight_by_idx[oi] = info
                    self._update_order_state(
                        client_order_id=coi if coi else None,
                        exchange_order_id=str(oi),
                        status=norm or "open",
                        symbol=symbol,
                        is_ask=is_ask if isinstance(is_ask, bool) else None,
                        info=od,
                    )
            # store back
            if curr_open:
                self._active_orders_by_market[mid] = curr_open
            else:
                self._active_orders_by_market.pop(mid, None)

    def _apply_trades_update(self, trades_dict: Dict[str, Any]):
        for k, arr in trades_dict.items():
            try:
                mid = int(k)
            except Exception:
                try:
                    mid = int(str(k))
                except Exception:
                    continue
            dq = self._trades_by_market.get(mid)
            if dq is None:
                dq = deque(maxlen=self._trades_maxlen)
                self._trades_by_market[mid] = dq
            if isinstance(arr, list):
                for t in arr:
                    if not isinstance(t, dict):
                        continue
                    dq.append(t)
                    self.emit_trade(t)

    def _apply_positions_update(self, positions_dict: Dict[str, Any]):
        for k, pos in positions_dict.items():
            try:
                mid = int(pos.get("market_id", k))
            except Exception:
                continue
            self._positions_by_market[mid] = pos
            self.emit_position(pos)

    # account WS handler: cache positions and forward to user callback
    def _handle_account_update(self, account_id: str, payload: Dict[str, Any]):
        # detect fills via trades if available
        trades = payload.get("trades")
        if isinstance(trades, list) and trades:
            # correlate by tx_hash or market_id + side if possible
            for t in trades:
                try:
                    txh = t.get("tx_hash")
                    mid = int(t.get("market_id")) if t.get("market_id") is not None else None
                except Exception:
                    txh, mid = None, None
                # mark matching inflight as filled
                for coi, info in list(self._inflight_by_coi.items()):
                    if (txh and info.get("tx_hash") == txh) or (mid is not None and int(info.get("market_index")) == mid):
                        info["status"] = "filled"
                        try:
                            client_id = int(info.get("client_order_index")) if info.get("client_order_index") is not None else None
                        except Exception:
                            client_id = None
                        exch_id = info.get("order_index")
                        self._update_order_state(
                            client_order_id=client_id,
                            exchange_order_id=str(exch_id) if exch_id is not None else None,
                            state=OrderState.FILLED,
                            symbol=self._market_to_symbol.get(mid) if mid is not None else None,
                            info=dict(info),
                        )
                        oi = info.get("order_index")
                        if oi is not None:
                            self._inflight_by_idx.pop(int(oi), None)
                        self._inflight_by_coi.pop(coi, None)

        # cache positions if present
        positions = payload.get("positions")
        if isinstance(positions, dict):
            for k, v in positions.items():
                try:
                    mid = int(v.get("market_id", k))
                except Exception:
                    continue
                prev = self._account_positions.get(mid)
                self._account_positions[mid] = v
                # if position changed and we still track orders on this market, best-effort mark them as filled
                try:
                    prev_pos = float(prev.get("position")) if isinstance(prev, dict) and prev.get("position") is not None else None
                    curr_pos = float(v.get("position")) if v.get("position") is not None else None
                except Exception:
                    prev_pos = curr_pos = None
                if prev_pos is not None and curr_pos is not None and prev_pos != curr_pos:
                    for coi, info in list(self._inflight_by_coi.items()):
                        if int(info.get("market_index")) == mid:
                            info["status"] = "filled"
                            try:
                                client_id = int(info.get("client_order_index")) if info.get("client_order_index") is not None else None
                            except Exception:
                                client_id = None
                            exch_id = info.get("order_index")
                            self._update_order_state(
                                client_order_id=client_id,
                                exchange_order_id=str(exch_id) if exch_id is not None else None,
                                state=OrderState.FILLED,
                                symbol=self._market_to_symbol.get(mid) if mid is not None else None,
                                info=dict(info),
                            )
                            oi = info.get("order_index")
                            if oi is not None:
                                self._inflight_by_idx.pop(int(oi), None)
                            self._inflight_by_coi.pop(coi, None)
        # forward raw payload if user handler provided
        if self._on_account_update:
            try:
                self._on_account_update(account_id, payload)
            except Exception:
                pass

    # orders --------------------------------------------------------------------
    async def place_limit(
        self,
        symbol: str,
        client_order_index: int,
        base_amount: int,
        price: int,
        is_ask: bool,
        post_only: bool = False,
        reduce_only: int = 0,
    ) -> Tuple[Any, Any, Optional[str]]:
        await self._ensure_signer()
        market_index = await self.get_market_id(symbol)
        try:
            idx = await self._ensure_account_index()
        except Exception:
            idx = None
        # intent log (INFO for field audit)
        try:
            self.log.info(
                f"place_limit submit: sym={symbol} mid={market_index} idx={idx} cio={client_order_index} "
                f"is_ask={bool(is_ask)} po={bool(post_only)} ro={int(reduce_only)} price_i={price} base_i={base_amount}"
            )
        except Exception:
            pass
        tif = lighter.SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY if post_only else lighter.SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
        await self._throttler.acquire("/api/v1/sendTx")
        self.create_tracking_limit_order(client_order_index, symbol=symbol, is_ask=is_ask, price_i=price, size_i=base_amount)
        self._update_order_state(
            client_order_id=client_order_index,
            symbol=symbol,
            is_ask=is_ask,
            state=OrderState.SUBMITTING,
            info={
                "market_index": market_index,
                "price": price,
                "base_amount": base_amount,
                "post_only": post_only,
                "reduce_only": reduce_only,
            },
        )
        created_tx, ret, err = await self.signer.create_order(
            market_index=market_index,
            client_order_index=client_order_index,
            base_amount=base_amount,
            price=price,
            is_ask=1 if is_ask else 0,
            order_type=lighter.SignerClient.ORDER_TYPE_LIMIT,
            time_in_force=tif,
            reduce_only=reduce_only,
            trigger_price=0,
        )
        # normalize non-OK responses into errors and result log (best-effort stringify)
        try:
            txh = None
            if isinstance(ret, dict):
                txh = ret.get("tx_hash") or ret.get("txHash")
            elif hasattr(ret, "tx_hash"):
                txh = getattr(ret, "tx_hash", None)
            code = getattr(ret, "code", None)
            if err is None and (ret is None or (code is not None and int(code) != 200)):
                # promote non-OK code to error string
                err = f"non_ok_code:{code}"
            self.log.info(
                f"place_limit result: cio={client_order_index} mid={market_index} tx_hash={txh} err={err}"
            )
        except Exception:
            pass
        # register inflight on success
        if err is None:
            try:
                self._inflight_by_coi[int(client_order_index)] = {
                    "market_index": int(market_index),
                    "is_ask": bool(is_ask),
                    "base_amount": int(base_amount),
                    "reduce_only": int(reduce_only),
                    "client_order_index": int(client_order_index),
                    "created_at": time.time(),
                }
            except Exception:
                pass
            exch_id = None
            if isinstance(ret, dict):
                exch_id = ret.get("tx_hash") or ret.get("txHash")
            self._update_order_state(
                client_order_id=client_order_index,
                exchange_order_id=str(exch_id) if exch_id else None,
                symbol=symbol,
                is_ask=is_ask,
                state=OrderState.OPEN,
                info=ret if isinstance(ret, dict) else {},
            )
        else:
            self._update_order_state(
                client_order_id=client_order_index,
                symbol=symbol,
                is_ask=is_ask,
                state=OrderState.FAILED,
                info={"error": err, "response": ret},
            )
        return (created_tx, ret, err)

    async def submit_limit_order(
        self,
        symbol: str,
        client_order_index: int,
        base_amount: int,
        price: int,
        is_ask: bool,
        *,
        post_only: bool = False,
        reduce_only: int = 0,
    ) -> TrackingLimitOrder:
        tracker = self.create_tracking_limit_order(
            client_order_index,
            symbol=symbol,
            is_ask=is_ask,
            price_i=price,
            size_i=base_amount,
        )
        await self.place_limit(
            symbol=symbol,
            client_order_index=client_order_index,
            base_amount=base_amount,
            price=price,
            is_ask=is_ask,
            post_only=post_only,
            reduce_only=reduce_only,
        )
        return tracker

    async def is_coi_open(self, client_order_index: int, market_index: Optional[int] = None) -> Optional[bool]:
        """Best-effort REST check whether an order (by client_order_index) appears in account active orders.
        Returns True if present; False if not found; None on error.
        """
        try:
            await self._ensure_markets()
            if market_index is None:
                info = self._inflight_by_coi.get(int(client_order_index))
                if isinstance(info, dict):
                    try:
                        market_index = int(info.get("market_index"))
                    except Exception:
                        market_index = None
            if market_index is None:
                return None
            idx = await self._ensure_account_index()
            await self._throttler.acquire("/api/v1/accountActiveOrders")
            orders = await self.order_api.account_active_orders(int(idx), int(market_index))
            arr = []
            try:
                arr = list(getattr(orders, "orders", []) or [])
            except Exception:
                arr = []
            for od in arr:
                try:
                    coi = int(getattr(od, "client_order_index", None) if not isinstance(od, dict) else od.get("client_order_index"))
                    if coi == int(client_order_index):
                        return True
                except Exception:
                    continue
            return False
        except Exception:
            return None

    async def place_market(
        self,
        symbol: str,
        client_order_index: int,
        base_amount: int,
        is_ask: bool,
        reduce_only: int = 0,
        ) -> Tuple[Any, Any, Optional[str]]:
        await self._ensure_signer()
        market_index = await self.get_market_id(symbol)
        # Fetch top-of-book to provide avg execution price required by signer
        await self._throttler.acquire("/api/v1/orderBookOrders")
        ob = await self.order_api.order_book_orders(market_index, 1)
        if is_ask:
            # selling hits best bid
            ideal = ob.bids[0].price if ob and ob.bids else None
        else:
            # buying lifts best ask
            ideal = ob.asks[0].price if ob and ob.asks else None
        if ideal is None:
            return None, None, "empty order book"
        try:
            avg_execution_price = int(str(ideal).replace(".", ""))
        except Exception:
            return None, None, "invalid top-of-book price"

        await self._throttler.acquire("/api/v1/sendTx")
        self.create_tracking_market_order(client_order_index, symbol=symbol, is_ask=is_ask)
        self._update_order_state(
            client_order_id=client_order_index,
            symbol=symbol,
            is_ask=is_ask,
            state=OrderState.SUBMITTING,
            info={
                "market_index": market_index,
                "base_amount": base_amount,
                "avg_execution_price": avg_execution_price,
                "reduce_only": reduce_only,
            },
        )
        created_tx, ret, err = await self.signer.create_market_order(
            market_index=market_index,
            client_order_index=client_order_index,
            base_amount=base_amount,
            avg_execution_price=avg_execution_price,
            is_ask=bool(is_ask),
            reduce_only=bool(reduce_only),
        )
        # normalize non-OK responses into errors
        try:
            code = getattr(ret, "code", None)
            if err is None and (ret is None or (code is not None and int(code) != 200)):
                err = f"non_ok_code:{code}"
        except Exception:
            pass
        if err is None:
            try:
                self._inflight_by_coi[int(client_order_index)] = {
                    "market_index": int(market_index),
                    "is_ask": bool(is_ask),
                    "base_amount": int(base_amount),
                    "reduce_only": int(reduce_only),
                    "client_order_index": int(client_order_index),
                    "created_at": time.time(),
                }
            except Exception:
                pass
        if err:
            self._update_order_state(
                client_order_id=client_order_index,
                symbol=symbol,
                is_ask=is_ask,
                state=OrderState.FAILED,
                info={"error": err, "response": ret},
            )
        else:
            exch_id = None
            if isinstance(ret, dict):
                exch_id = ret.get("tx_hash") or ret.get("txHash")
            self._update_order_state(
                client_order_id=client_order_index,
                exchange_order_id=str(exch_id) if exch_id else None,
                symbol=symbol,
                is_ask=is_ask,
                state=OrderState.FILLED,
                info=ret if isinstance(ret, dict) else {},
            )
        return (created_tx, ret, err)

    async def submit_market_order(
        self,
        symbol: str,
        client_order_index: int,
        base_amount: int,
        is_ask: bool,
        *,
        reduce_only: int = 0,
    ) -> TrackingMarketOrder:
        tracker = self.create_tracking_market_order(client_order_index, symbol=symbol, is_ask=is_ask)
        await self.place_market(
            symbol=symbol,
            client_order_index=client_order_index,
            base_amount=base_amount,
            is_ask=is_ask,
            reduce_only=reduce_only,
        )
        return tracker

    async def cancel_all(self) -> Tuple[Any, Any, Optional[str]]:
        await self._ensure_signer()
        # immediate cancel-all, time as current epoch ms
        tif = lighter.SignerClient.CANCEL_ALL_TIF_IMMEDIATE
        # For immediate cancel-all, signer expects nil time; use 0
        await self._throttler.acquire("/api/v1/sendTx")
        return await self.signer.cancel_all_orders(time_in_force=tif, time=0)

    async def cancel_order(self, order_index: int, market_index: Optional[int] = None) -> Tuple[Any, Any, Optional[str]]:
        await self._ensure_signer()
        if market_index is None:
            # infer from ws cache
            for mid, omap in self._active_orders_by_market.items():
                if int(order_index) in omap:
                    market_index = int(mid)
                    break
        if market_index is None:
            raise ValueError("market_index required if order is not tracked")
        await self._throttler.acquire("/api/v1/sendTx")
        result = await self.signer.cancel_order(market_index=int(market_index), order_index=int(order_index))
        created_tx, ret, err = result
        client_id = None
        for coi, idx in self._order_index_by_coi.items():
            if idx == int(order_index):
                client_id = coi
                break
        state = OrderState.CANCELLED if err is None else OrderState.FAILED
        self._update_order_state(
            client_order_id=client_id,
            exchange_order_id=str(order_index),
            state=state,
            symbol=self._market_to_symbol.get(int(market_index)) if market_index is not None else None,
            info=ret if isinstance(ret, dict) else {"error": err},
        )
        return result

    async def cancel_by_client_order_index(
        self,
        client_order_index: int,
        symbol: Optional[str] = None,
    ) -> Tuple[Any, Any, Optional[str]]:
        await self._ensure_signer()
        target_coi = int(client_order_index)
        market_index: Optional[int] = None
        info = self._inflight_by_coi.get(target_coi)
        if info is not None:
            try:
                market_index = int(info.get("market_index"))
            except Exception:
                market_index = None
        if market_index is None and symbol:
            try:
                market_index = await self.get_market_id(symbol)
            except Exception:
                market_index = None

        order_index: Optional[int] = None
        for _ in range(5):
            order_index = self._order_index_by_coi.get(target_coi)
            if order_index:
                break
            for mid, omap in self._active_orders_by_market.items():
                for oi, od in omap.items():
                    try:
                        coi = int((od.get("client_order_index")) if isinstance(od, dict) else 0)
                    except Exception:
                        continue
                    if coi == target_coi:
                        order_index = int(oi)
                        self._order_index_by_coi[target_coi] = order_index
                        market_index = int(mid)
                        break
                if order_index:
                    break
            if order_index:
                break
            await asyncio.sleep(0.2)

        if order_index is not None:
            return await self.cancel_order(order_index=int(order_index), market_index=market_index)

        # fallback: cancel all if unable to map specific order
        return await self.cancel_all()

    async def is_order_open(self, order_index: int, market_index: Optional[int] = None) -> Optional[bool]:
        """Best-effort check via REST whether an order is still open.
        Returns True if present in account active orders; False if not found; None on error.
        """
        try:
            await self._ensure_markets()
            if market_index is None:
                for mid, omap in self._active_orders_by_market.items():
                    if int(order_index) in omap:
                        market_index = int(mid)
                        break
            if market_index is None:
                return False
            idx = await self._ensure_account_index()
            await self._throttler.acquire("/api/v1/accountActiveOrders")
            orders = await self.order_api.account_active_orders(int(idx), int(market_index))
            arr = []
            try:
                arr = list(getattr(orders, "orders", []) or [])
            except Exception:
                arr = []
            for od in arr:
                try:
                    oi = int(getattr(od, "order_index", None) if not isinstance(od, dict) else od.get("order_index"))
                    if oi == int(order_index):
                        return True
                except Exception:
                    continue
            return False
        except Exception:
            return None

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        await self._ensure_markets()
        if symbol is None:
            # aggregate all open orders from ws cache
            out: List[Dict[str, Any]] = []
            for mid, omap in self._active_orders_by_market.items():
                out.extend(list(omap.values()))
            return out
        mid = await self.get_market_id(symbol)
        omap = self._active_orders_by_market.get(int(mid), {})
        return list(omap.values())

    async def get_positions(self) -> List[Dict[str, Any]]:
        # return ws-cached positions list
        out: List[Dict[str, Any]] = []
        for mid, pos in self._positions_by_market.items():
            d = dict(pos)
            d["market_index"] = int(d.get("market_id", mid))
            d["symbol"] = d.get("symbol") or self._market_to_symbol.get(int(mid))
            out.append(d)
        return out

    # Convenience high-level APIs ------------------------------------------------
    async def get_best_bid(self, symbol: str) -> Optional[int]:
        mid = await self.get_market_id(symbol)
        await self._throttler.acquire("/api/v1/orderBookOrders")
        ob = await self.order_api.order_book_orders(mid, 1)
        if ob and ob.bids:
            return int(str(ob.bids[0].price).replace(".", ""))
        return None

    async def get_best_ask(self, symbol: str) -> Optional[int]:
        mid = await self.get_market_id(symbol)
        await self._throttler.acquire("/api/v1/orderBookOrders")
        ob = await self.order_api.order_book_orders(mid, 1)
        if ob and ob.asks:
            return int(str(ob.asks[0].price).replace(".", ""))
        return None

    async def get_top_of_book(self, symbol: str) -> Tuple[Optional[int], Optional[int], int]:
        """
        Return (best_bid_i, best_ask_i, scale) where scale is 10**decimals detected
        from the price string in orderBookOrders. This ensures consistent scaling
        with how we convert prices to integers elsewhere.
        """
        mid = await self.get_market_id(symbol)
        await self._throttler.acquire("/api/v1/orderBookOrders")
        ob = await self.order_api.order_book_orders(mid, 1)
        scale = 1
        bid_i = ask_i = None
        if ob and ob.asks:
            p = str(ob.asks[0].price)
            if "." in p:
                decimals = len(p.split(".", 1)[1])
                scale = 10 ** decimals
            else:
                scale = 1
            try:
                ask_i = int(p.replace(".", ""))
            except Exception:
                ask_i = None
        if ob and ob.bids:
            try:
                bid_i = int(str(ob.bids[0].price).replace(".", ""))
            except Exception:
                bid_i = None
        return bid_i, ask_i, scale

    async def _scales(self, symbol: str) -> Tuple[int, int]:
        await self._ensure_markets()
        mid = await self.get_market_id(symbol)
        ob_list = await self.order_api.order_books()
        entry = next((e for e in ob_list.order_books if int(e.market_id) == mid), None)
        if not entry:
            raise RuntimeError("market scales not found")
        return int(entry.supported_price_decimals), int(entry.supported_size_decimals)

    async def place_limit_order(self, symbol: str, side: str, price: float, size: float, post_only: bool = True, reduce_only: bool = False):
        p_dec, s_dec = await self._scales(symbol)
        price_i = int(round(price * (10 ** p_dec)))
        size_i = int(round(size * (10 ** s_dec)))
        cio = int(time.time() * 1000) % 1_000_000
        return await self.place_limit(symbol, cio, size_i, price_i, is_ask=(side.lower()=="sell"), post_only=post_only, reduce_only=int(reduce_only))

    async def place_market_order(self, symbol: str, side: str, size: float, reduce_only: bool = False):
        _p, s_dec = await self._scales(symbol)
        size_i = int(round(size * (10 ** s_dec)))
        cio = int(time.time() * 1000) % 1_000_000
        return await self.place_market(symbol, cio, size_i, is_ask=(side.lower()=="sell"), reduce_only=int(reduce_only))
