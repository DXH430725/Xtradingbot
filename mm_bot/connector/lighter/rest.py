from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from eth_account import Account

from mm_bot.execution.orders import OrderState, TrackingLimitOrder, TrackingMarketOrder


class LighterRESTMixin:
    """REST and trading helpers for the Lighter connector."""

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
        nonce_type = getattr(self._lighter.nonce_manager, "NonceManagerType", None)
        nonce_mode = None
        if nonce_type is not None:
            try:
                nonce_mode = nonce_type.API
            except Exception:
                nonce_mode = None
        self.signer = self._lighter.SignerClient(
            url=self.config.base_url,
            private_key=self._api_priv,
            api_key_index=int(self._api_key_index or 0),
            account_index=account_index,
            nonce_management_type=nonce_mode or self._lighter.nonce_manager.NonceManagerType.OPTIMISTIC,
        )
        try:
            key_idx = int(getattr(self.signer, "api_key_index", 0))
            self.signer.nonce_manager.hard_refresh_nonce(key_idx)
        except Exception:
            pass

    def _store_market_entry(self, entry: Any) -> None:
        try:
            symbol = entry.symbol
            market_id = int(entry.market_id)
        except Exception:
            return
        self._symbol_to_market[symbol] = market_id
        self._market_to_symbol[market_id] = symbol
        price_dec = self._safe_int(getattr(entry, "supported_price_decimals", 0))
        size_dec = self._safe_int(getattr(entry, "supported_size_decimals", 0))
        min_base = self._safe_float(getattr(entry, "min_base_amount", 0.0))
        tick_val = getattr(entry, "price_tick", None)
        tick_size = self._safe_float(tick_val) if tick_val is not None else (1.0 / (10 ** price_dec) if price_dec else 0.0)
        step_val = getattr(entry, "quantity_step", None)
        step_size = self._safe_float(step_val) if step_val is not None else (1.0 / (10 ** size_dec) if size_dec else 0.0)
        self._market_info_by_symbol[symbol] = {
            "symbol": symbol,
            "market_index": market_id,
            "price_precision": price_dec,
            "quantity_precision": size_dec,
            "min_qty": min_base,
            "tick_size": tick_size,
            "step_size": step_size,
        }

    async def _refresh_market_entries(self) -> None:
        await self._throttler.acquire("/api/v1/orderBooks")
        ob = await self.order_api.order_books()
        entries = getattr(ob, "order_books", []) or []
        for entry in entries:
            self._store_market_entry(entry)

    async def _ensure_markets(self):
        if not self._symbol_to_market:
            await self._refresh_market_entries()

    async def list_symbols(self) -> List[str]:
        await self._ensure_markets()
        return list(self._symbol_to_market.keys())

    async def get_price_size_decimals(self, symbol: str) -> Tuple[int, int]:
        """Return (price_decimals, size_decimals) for a given symbol."""
        entry = await self._get_market_entry(symbol)
        return int(entry["price_precision"]), int(entry["quantity_precision"])

    async def get_min_order_size_i(self, symbol: str) -> int:
        """Return minimal base size in integer units for a symbol."""
        entry = await self._get_market_entry(symbol)
        size_dec = int(entry["quantity_precision"])
        min_size = self._safe_float(entry.get("min_qty", 0.0))
        return max(1, int(round(min_size * (10 ** size_dec))))

    async def get_market_info(self, symbol: str) -> Dict[str, Any]:
        entry = await self._get_market_entry(symbol)
        return dict(entry)

    async def get_market_id(self, symbol: str) -> int:
        await self._ensure_markets()
        if symbol not in self._symbol_to_market:
            raise ValueError(f"Unknown symbol {symbol}")
        return self._symbol_to_market[symbol]

    async def best_effort_latency_ms(self) -> float:
        t0 = time.perf_counter()
        await self._throttler.acquire("/")
        resp = await self._lighter.RootApi(self.api_client).status()
        _ = getattr(resp, "code", None)
        return (time.perf_counter() - t0) * 1000.0

    async def get_account_overview(self) -> Dict[str, Any]:
        idx = await self._ensure_account_index()
        await self._throttler.acquire("/api/v1/account")
        # account(by, value): by="index" and value is string index
        detail = await self.account_api.account(by="index", value=str(idx))
        return detail.to_dict()

    async def _get_market_entry(self, symbol: str) -> Dict[str, Any]:
        await self._ensure_markets()
        info = self._market_info_by_symbol.get(symbol)
        if info is None:
            await self._refresh_market_entries()
            info = self._market_info_by_symbol.get(symbol)
        if info is None:
            raise ValueError(f"Market info unavailable for {symbol}")
        return info

    def _safe_int(self, value: Any) -> int:
        try:
            return int(value)
        except Exception:
            try:
                return int(float(value))
            except Exception:
                return 0

    def _safe_float(self, value: Any) -> float:
        try:
            return float(value)
        except Exception:
            try:
                return float(str(value))
            except Exception:
                return 0.0

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
        tif = self._lighter.SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY if post_only else self._lighter.SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
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
            order_type=self._lighter.SignerClient.ORDER_TYPE_LIMIT,
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
        *,
        max_slippage: Optional[float] = None,
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
        slippage = max_slippage if (max_slippage is not None and max_slippage > 0) else None
        acceptable_price = avg_execution_price
        if slippage is not None:
            try:
                acceptable_price = int(round(avg_execution_price * (1 + slippage * (-1 if is_ask else 1))))
            except Exception:
                acceptable_price = avg_execution_price

        info_payload = {
            "market_index": market_index,
            "base_amount": base_amount,
            "avg_execution_price": avg_execution_price,
            "reduce_only": reduce_only,
        }
        if slippage is not None:
            info_payload["max_slippage"] = slippage
            info_payload["acceptable_execution_price"] = acceptable_price

        self._update_order_state(
            client_order_id=client_order_index,
            symbol=symbol,
            is_ask=is_ask,
            state=OrderState.SUBMITTING,
            info=info_payload,
        )

        if slippage is not None:
            created_tx, ret, err = await self.signer.create_market_order_if_slippage(
                market_index=market_index,
                client_order_index=client_order_index,
                base_amount=base_amount,
                max_slippage=slippage,
                is_ask=bool(is_ask),
                reduce_only=bool(reduce_only),
                ideal_price=avg_execution_price,
            )
        else:
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
        max_slippage: Optional[float] = None,
    ) -> TrackingMarketOrder:
        tracker = self.create_tracking_market_order(client_order_index, symbol=symbol, is_ask=is_ask)
        await self.place_market(
            symbol=symbol,
            client_order_index=client_order_index,
            base_amount=base_amount,
            is_ask=is_ask,
            reduce_only=reduce_only,
            max_slippage=max_slippage,
        )
        return tracker

    async def cancel_all(self) -> Tuple[Any, Any, Optional[str]]:
        await self._ensure_signer()
        # immediate cancel-all, time as current epoch ms
        tif = self._lighter.SignerClient.CANCEL_ALL_TIF_IMMEDIATE
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
