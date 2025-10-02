from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bpx.account import Account
from bpx.public import Public

from .base import BaseConnector


def _decimal_places(value: str) -> int:
    if "." not in value:
        return 0
    return len(value.split(".")[1].rstrip("0"))


def _format_int(value: int, decimals: int) -> str:
    scale = Decimal(10) ** decimals
    return str(Decimal(value) / scale)


class BackpackConnector(BaseConnector):
    base_url = "https://api.backpack.exchange"

    def __init__(self, *, key_path: Path) -> None:
        super().__init__("backpack")
        self._key_path = key_path
        self._account: Optional[Account] = None
        self._public = Public()
        self._markets: Dict[str, Dict[str, Any]] = {}

    def _load_keys(self) -> Tuple[str, str]:
        content = self._key_path.read_text(encoding="utf-8")
        lines = dict(
            line.split(":", 1)
            for line in content.splitlines()
            if ":" in line
        )
        api_key = lines.get("api key") or lines.get("api_key") or lines.get("apiKey")
        api_secret = lines.get("api secret") or lines.get("api_secret") or lines.get("apiSecret")
        if not api_key or not api_secret:
            raise ValueError(f"invalid backpack key file: {self._key_path}")
        return api_key.strip(), api_secret.strip()

    async def start(self) -> None:
        if self._account is None:
            api_key, api_secret = self._load_keys()
            self._account = Account(api_key=api_key, secret_key=api_secret)
            markets = self._public.get_markets()
            self._markets = {entry["symbol"]: entry for entry in markets if entry.get("visible")}
        await super().start()

    async def stop(self) -> None:
        await super().stop()
        self._account = None

    def _get_market_info(self, symbol: str) -> Dict[str, Any]:
        if symbol not in self._markets:
            raise ValueError(f"unknown market {symbol}")
        return self._markets[symbol]

    async def get_price_size_decimals(self, symbol: str) -> Tuple[int, int]:
        info = self._get_market_info(symbol)
        price_tick = info["filters"]["price"]["tickSize"]
        size_step = info["filters"]["quantity"]["stepSize"]
        return _decimal_places(price_tick), _decimal_places(size_step)

    async def get_min_size_i(self, symbol: str) -> int:
        info = self._get_market_info(symbol)
        min_qty = info["filters"]["quantity"]["minQuantity"]
        _, size_dec = await self.get_price_size_decimals(symbol)
        scale = Decimal(10) ** size_dec
        return int(Decimal(min_qty) * scale)

    async def get_top_of_book(self, symbol: str) -> Tuple[Optional[int], Optional[int], int]:
        price_dec, _ = await self.get_price_size_decimals(symbol)
        scale = 10 ** price_dec
        book = self._public.get_depth(symbol)
        bid = int(Decimal(book["bids"][0][0]) * scale) if book["bids"] else None
        ask = int(Decimal(book["asks"][0][0]) * scale) if book["asks"] else None
        return bid, ask, scale

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
        if self._account is None:
            raise RuntimeError("connector not started")
        price_dec, size_dec = await self.get_price_size_decimals(symbol)
        response = self._account.execute_order(
            symbol=symbol,
            side="Sell" if is_ask else "Buy",
            order_type="Limit",
            quantity=_format_int(base_amount, size_dec),
            price=_format_int(price, price_dec),
            client_id=client_order_index,
            post_only=post_only,
            reduce_only=bool(reduce_only),
            time_in_force="GTC",
        )
        return str(response.get("orderId") or response.get("order_id") or response)

    async def submit_market_order(
        self,
        *,
        symbol: str,
        client_order_index: int,
        size_i: int,
        is_ask: bool,
        reduce_only: int = 0,
    ) -> str:
        if self._account is None:
            raise RuntimeError("connector not started")
        _, size_dec = await self.get_price_size_decimals(symbol)
        response = self._account.execute_order(
            symbol=symbol,
            side="Sell" if is_ask else "Buy",
            order_type="Market",
            quantity=_format_int(size_i, size_dec),
            client_id=client_order_index,
            reduce_only=bool(reduce_only),
        )
        return str(response.get("orderId") or response.get("order_id") or response)

    async def cancel_by_client_id(self, symbol: str, client_order_index: int) -> None:
        if self._account is None:
            raise RuntimeError("connector not started")
        self._account.cancel_order(symbol=symbol, client_id=client_order_index)

    async def get_order(self, symbol: str, client_order_index: int) -> Dict[str, Any]:
        if self._account is None:
            raise RuntimeError("connector not started")
        return self._account.get_open_order(symbol=symbol, client_id=client_order_index) or {}

    async def get_positions(self) -> List[Dict[str, Any]]:
        if self._account is None:
            raise RuntimeError("connector not started")
        data = self._account.get_open_positions() or []
        return data if isinstance(data, list) else [data]

    async def get_margin(self) -> Dict[str, Any]:
        if self._account is None:
            raise RuntimeError("connector not started")
        balances = self._account.get_balances() or {}
        return balances if isinstance(balances, dict) else {"balances": balances}


__all__ = ["BackpackConnector"]
