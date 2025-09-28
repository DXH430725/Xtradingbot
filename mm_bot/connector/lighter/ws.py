from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional

import websockets

from mm_bot.execution.orders import OrderState


class LighterWSMixin:
    """Websocket state management for the Lighter connector."""

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
