from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import lighter

from .base import BaseConnector


def _parse_key_file(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip().lower()] = value.strip()
    return data


class LighterConnector(BaseConnector):
    base_url = "https://mainnet.zklighter.elliot.ai"

    def __init__(self, *, key_path: Path) -> None:
        super().__init__("lighter")
        self._key_path = key_path
        self._signer: Optional[lighter.SignerClient] = None
        self._api_client: Optional[lighter.ApiClient] = None
        self._order_api: Optional[lighter.OrderApi] = None
        self._account_api: Optional[lighter.AccountApi] = None
        self._market_meta: Dict[str, Dict[str, Any]] = {}
        self._account_index: Optional[int] = None
        self._api_key_index: Optional[int] = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._signer is None:
            keys = _parse_key_file(self._key_path)
            private_key = keys.get("private key") or keys.get("private_key")
            api_key_index = int(keys.get("api key index") or keys.get("api_key_index") or "0")
            account_index = int(keys.get("account index") or keys.get("account_index") or "0")
            base_url = keys.get("base url") or keys.get("base_url") or self.base_url
            self._signer = lighter.SignerClient(
                url=base_url,
                private_key=private_key,
                api_key_index=api_key_index,
                account_index=account_index,
            )
            self._api_client = self._signer.api_client
            self._order_api = lighter.OrderApi(self._api_client)
            self._account_api = lighter.AccountApi(self._api_client)
            self._account_index = account_index
            self._api_key_index = api_key_index
            await self._hydrate_markets()
        await super().start()

    async def stop(self) -> None:
        await super().stop()
        if self._api_client is not None:
            await self._api_client.close()
        self._api_client = None
        self._order_api = None
        self._account_api = None
        self._signer = None
        self._market_meta.clear()

    async def _hydrate_markets(self) -> None:
        if self._order_api is None:
            raise RuntimeError("order api unavailable")
        books = await self._order_api.order_books()
        self._market_meta = {
            entry.symbol: {
                "market_id": entry.market_id,
                "size_decimals": entry.supported_size_decimals,
                "price_decimals": entry.supported_price_decimals,
                "min_base": entry.min_base_amount,
            }
            for entry in books.order_books
            if entry.status == "active"
        }

    def _market(self, symbol: str) -> Dict[str, Any]:
        if symbol not in self._market_meta:
            raise ValueError(f"unknown lighter symbol {symbol}")
        return self._market_meta[symbol]

    async def get_price_size_decimals(self, symbol: str) -> Tuple[int, int]:
        meta = self._market(symbol)
        return int(meta["price_decimals"]), int(meta["size_decimals"])

    async def get_min_size_i(self, symbol: str) -> int:
        price_dec, size_dec = await self.get_price_size_decimals(symbol)
        meta = self._market(symbol)
        scale = Decimal(10) ** size_dec
        return int(Decimal(meta["min_base"]) * scale)

    async def get_top_of_book(self, symbol: str) -> Tuple[Optional[int], Optional[int], int]:
        if self._order_api is None:
            raise RuntimeError("order api unavailable")
        meta = self._market(symbol)
        market_id = meta["market_id"]
        book = await self._order_api.order_book_orders(market_id=market_id, depth=1)
        price_dec = int(meta["price_decimals"])
        scale = 10 ** price_dec
        best_bid = int(Decimal(book.bids[0].price) * scale) if book.bids else None
        best_ask = int(Decimal(book.asks[0].price) * scale) if book.asks else None
        return best_bid, best_ask, scale

    async def submit_limit_order(
        self,
        *,
        symbol: str,
        client_order_index: int,
        base_amount: int,
        price: int,
        is_ask: bool,
        post_only: bool = False,
        reduce_only: int = 0,
    ) -> str:
        if self._signer is None:
            raise RuntimeError("connector not started")
        meta = self._market(symbol)
        tif = lighter.SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY if post_only else lighter.SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
        create_order, tx_hash, error = await self._signer.create_order(
            meta["market_id"],
            client_order_index,
            base_amount,
            price,
            is_ask,
            order_type=lighter.SignerClient.ORDER_TYPE_LIMIT,
            time_in_force=tif,
            reduce_only=bool(reduce_only),
        )
        if error:
            raise RuntimeError(f"lighter limit order failed: {error}")
        return tx_hash.tx_hash if tx_hash else str(create_order.nonce)

    async def submit_market_order(
        self,
        *,
        symbol: str,
        client_order_index: int,
        size_i: int,
        is_ask: bool,
        reduce_only: int = 0,
    ) -> str:
        if self._signer is None:
            raise RuntimeError("connector not started")
        best_bid, best_ask, _scale = await self.get_top_of_book(symbol)
        reference = best_ask if not is_ask else best_bid
        if reference is None:
            raise RuntimeError("unable to determine reference price for market order")
        meta = self._market(symbol)
        _, tx_hash, error = await self._signer.create_market_order(
            meta["market_id"],
            client_order_index,
            size_i,
            int(Decimal(reference) * (Decimal("1.02") if not is_ask else Decimal("0.98"))),
            is_ask,
            reduce_only=bool(reduce_only),
        )
        if error:
            raise RuntimeError(f"lighter market order failed: {error}")
        return tx_hash.tx_hash

    async def cancel_by_client_id(self, symbol: str, client_order_index: int) -> None:
        if self._signer is None:
            raise RuntimeError("connector not started")
        meta = self._market(symbol)
        _, _, error = await self._signer.cancel_order(meta["market_id"], client_order_index)
        if error:
            raise RuntimeError(f"lighter cancel failed: {error}")

    async def _auth_token(self) -> str:
        if self._signer is None:
            raise RuntimeError("connector not started")
        token, error = self._signer.create_auth_token_with_expiry()
        if error:
            raise RuntimeError(f"auth token error: {error}")
        return token

    async def get_order(self, symbol: str, client_order_index: int) -> Dict[str, Any]:
        if self._account_api is None or self._account_index is None:
            raise RuntimeError("connector not started")
        meta = self._market(symbol)
        auth = await self._auth_token()
        orders = await self._account_api.account_active_orders(
            account_index=self._account_index,
            market_id=meta["market_id"],
            auth=auth,
        )
        for order in orders.orders:
            if order.client_order_index == client_order_index:
                return {
                    "state": "open",
                    "order_id": str(order.order_index),
                    "price": order.price,
                    "size": order.base_amount,
                }
        return {"state": "unknown"}

    async def get_positions(self) -> List[Dict[str, Any]]:
        if self._account_api is None or self._account_index is None:
            raise RuntimeError("connector not started")
        response = await self._account_api.account(by="index", value=str(self._account_index))
        positions: List[Dict[str, Any]] = []
        for account in response.accounts:
            for pos in account.positions or []:
                size = Decimal(pos.position) * (1 if pos.sign >= 0 else -1)
                positions.append({
                    "symbol": pos.symbol,
                    "net_size": str(size),
                    "entry_price": pos.avg_entry_price,
                    "unrealized_pnl": pos.unrealized_pnl,
                })
        return positions

    async def get_margin(self) -> Dict[str, Any]:
        if self._account_api is None or self._account_index is None:
            raise RuntimeError("connector not started")
        response = await self._account_api.account(by="index", value=str(self._account_index))
        if not response.accounts:
            return {}
        account = response.accounts[0]
        return {
            "collateral": account.collateral,
            "available_balance": account.available_balance,
            "total_asset_value": account.total_asset_value,
        }


__all__ = ["LighterConnector"]
