from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pysdk.grvt_ccxt import GrvtCcxt
from pysdk.grvt_ccxt_env import GrvtEnv
from pysdk.grvt_ccxt_types import GrvtOrderSide

from .base import BaseConnector


def _parse_key_file(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip().lower()] = value.strip()
    return data


class GrvtConnector(BaseConnector):
    base_url = "https://api.gravity.tech"

    def __init__(self, *, key_path: Path, env: GrvtEnv = GrvtEnv.PROD) -> None:
        super().__init__("grvt")
        self._key_path = key_path
        self._env = env
        self._client: Optional[GrvtCcxt] = None

    async def start(self) -> None:
        if self._client is None:
            keys = _parse_key_file(self._key_path)
            trading_id = keys.get("trading account id") or keys.get("trading_account_id")
            api_key = keys.get("api key") or keys.get("api_key")
            private_key = keys.get("secret private key") or keys.get("private_key")
            if private_key and private_key.startswith("0x"):
                private_key = private_key[2:]
            params = {
                "trading_account_id": trading_id,
                "api_key": api_key,
                "private_key": private_key,
            }
            self._client = GrvtCcxt(env=self._env, parameters=params)
        await super().start()

    async def stop(self) -> None:
        await super().stop()
        self._client = None

    def _market(self, symbol: str) -> Dict[str, Any]:
        if self._client is None:
            raise RuntimeError("connector not started")
        if symbol not in self._client.markets:
            raise ValueError(f"unknown grvt symbol {symbol}")
        return self._client.markets[symbol]

    async def get_price_size_decimals(self, symbol: str) -> Tuple[int, int]:
        market = self._market(symbol)
        tick_size = Decimal(market["tick_size"])
        price_decimals = abs(tick_size.as_tuple().exponent)
        size_decimals = int(market.get("base_decimals", 6))
        return price_decimals, size_decimals

    async def get_min_size_i(self, symbol: str) -> int:
        market = self._market(symbol)
        _, size_dec = await self.get_price_size_decimals(symbol)
        min_size = Decimal(market.get("min_size", "0"))
        scale = Decimal(10) ** size_dec
        return int(min_size * scale)

    async def get_top_of_book(self, symbol: str) -> Tuple[Optional[int], Optional[int], int]:
        if self._client is None:
            raise RuntimeError("connector not started")
        book = self._client.fetch_order_book(symbol, limit=5)
        price_dec, _ = await self.get_price_size_decimals(symbol)
        scale = 10 ** price_dec
        bid = int(Decimal(book["bids"][0][0]) * scale) if book["bids"] else None
        ask = int(Decimal(book["asks"][0][0]) * scale) if book["asks"] else None
        return bid, ask, scale

    def _format_amount(self, value: int, decimals: int) -> str:
        scale = Decimal(10) ** decimals
        return str(Decimal(value) / scale)

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
        if self._client is None:
            raise RuntimeError("connector not started")
        price_dec, size_dec = await self.get_price_size_decimals(symbol)
        amount = self._format_amount(base_amount, size_dec)
        price_str = self._format_amount(price, price_dec)
        params = {"client_order_id": client_order_index}
        order = self._client.create_order(
            symbol=symbol,
            order_type="limit",
            side="sell" if is_ask else "buy",
            amount=amount,
            price=price_str,
            params=params,
        )
        metadata = order.get("metadata", {})
        return str(metadata.get("client_order_id") or metadata.get("order_id") or client_order_index)

    async def submit_market_order(
        self,
        *,
        symbol: str,
        client_order_index: int,
        size_i: int,
        is_ask: bool,
        reduce_only: int = 0,
    ) -> str:
        if self._client is None:
            raise RuntimeError("connector not started")
        _, size_dec = await self.get_price_size_decimals(symbol)
        amount = self._format_amount(size_i, size_dec)
        order = self._client.create_order(
            symbol=symbol,
            order_type="market",
            side="sell" if is_ask else "buy",
            amount=amount,
            params={"client_order_id": client_order_index},
        )
        metadata = order.get("metadata", {})
        return str(metadata.get("client_order_id") or metadata.get("order_id") or client_order_index)

    async def cancel_by_client_id(self, symbol: str, client_order_index: int) -> None:
        if self._client is None:
            raise RuntimeError("connector not started")
        params = {"client_order_id": client_order_index, "symbol": symbol}
        self._client.cancel_order(params=params)

    async def get_order(self, symbol: str, client_order_index: int) -> Dict[str, Any]:
        if self._client is None:
            raise RuntimeError("connector not started")
        order = self._client.fetch_order(params={"client_order_id": client_order_index, "symbol": symbol})
        return order or {"state": "unknown"}

    async def get_positions(self) -> List[Dict[str, Any]]:
        if self._client is None:
            raise RuntimeError("connector not started")
        positions = self._client.fetch_positions()
        return positions or []

    async def get_margin(self) -> Dict[str, Any]:
        if self._client is None:
            raise RuntimeError("connector not started")
        balance = self._client.fetch_balance()
        return balance or {}


__all__ = ["GrvtConnector"]
