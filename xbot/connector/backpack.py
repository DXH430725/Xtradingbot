from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import BaseConnector

# Ensure vendored SDK (sdk/bpx-py) is importable without installation
_repo_root = Path(__file__).resolve().parents[2]
_sdk_path = _repo_root / "sdk" / "bpx-py"
if str(_sdk_path) not in sys.path:
    sys.path.insert(0, str(_sdk_path))

try:
    from bpx.async_.public import Public  # type: ignore
    from bpx.async_.account import Account  # type: ignore
    from bpx.constants.enums import OrderTypeEnum, TimeInForceEnum
except Exception as exc:  # pragma: no cover
    raise ImportError(
        "Backpack SDK not found. Ensure sdk/bpx-py is present."
    ) from exc


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
        self._public = Public()
        self._account: Optional[Account] = None
        self._markets: Dict[str, Dict[str, Any]] = {}

    def _load_keys(self) -> Tuple[str, str]:
        # Keys not needed for public REST; keep for forward-compat
        if not self._key_path.exists():
            return "", ""
        content = self._key_path.read_text(encoding="utf-8")
        lines = dict(
            line.split(":", 1)
            for line in content.splitlines()
            if ":" in line
        )
        api_key = lines.get("api key") or lines.get("api_key") or lines.get("apiKey") or ""
        api_secret = lines.get("api secret") or lines.get("api_secret") or lines.get("apiSecret") or ""
        return api_key.strip(), api_secret.strip()

    async def start(self) -> None:
        # Initialize account client if keys present
        pub, sec = self._load_keys()
        if pub and sec:
            self._account = Account(public_key=pub, secret_key=sec)

        markets = await self._public.get_markets()
        # API may return dict or list; normalize to list of dicts
        if isinstance(markets, dict) and "data" in markets:
            markets = markets["data"]
        self._markets = {entry["symbol"]: entry for entry in markets if entry.get("visible", True)}
        await super().start()

    async def stop(self) -> None:
        await super().stop()

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
        book = await self._public.get_depth(symbol)
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        try:
            # Ensure best prices regardless of server ordering
            bids_sorted = sorted(bids, key=lambda x: Decimal(str(x[0])), reverse=True)
            asks_sorted = sorted(asks, key=lambda x: Decimal(str(x[0])))
        except Exception:
            bids_sorted, asks_sorted = bids, asks
        bid = int(Decimal(str(bids_sorted[0][0])) * scale) if bids_sorted else None
        ask = int(Decimal(str(asks_sorted[0][0])) * scale) if asks_sorted else None
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
        if not self._account:
            raise RuntimeError("account keys not configured for order submission")
        price_dec, size_dec = await self.get_price_size_decimals(symbol)
        qty = _format_int(base_amount, size_dec)
        px = _format_int(price, price_dec)
        side = "Ask" if is_ask else "Bid"
        resp = await self._account.execute_order(
            symbol=symbol,
            side=side,
            order_type=OrderTypeEnum.LIMIT,
            time_in_force=TimeInForceEnum.GTC,
            quantity=qty,
            price=px,
            client_id=client_order_index,
            post_only=post_only,
            reduce_only=bool(reduce_only),
        )
        if isinstance(resp, dict) and resp.get("id"):
            return str(resp["id"])
        raise RuntimeError(f"limit order failed: {resp}")

    async def submit_market_order(
        self,
        *,
        symbol: str,
        client_order_index: int,
        size_i: int,
        is_ask: bool,
        reduce_only: int = 0,
    ) -> str:
        if not self._account:
            raise RuntimeError("account keys not configured for order submission")
        _, size_dec = await self.get_price_size_decimals(symbol)
        qty = _format_int(size_i, size_dec)
        side = "Ask" if is_ask else "Bid"
        resp = await self._account.execute_order(
            symbol=symbol,
            side=side,
            order_type=OrderTypeEnum.MARKET,
            quantity=qty,
            client_id=client_order_index,
            reduce_only=bool(reduce_only),
        )
        if isinstance(resp, dict) and resp.get("id"):
            return str(resp["id"])
        raise RuntimeError(f"market order failed: {resp}")

    async def cancel_by_client_id(self, symbol: str, client_order_index: int) -> None:
        if not self._account:
            raise RuntimeError("account keys not configured for order cancel")
        resp = await self._account.cancel_order(symbol=symbol, client_id=client_order_index)
        # Successful cancel returns status; treat absence of error as success
        if isinstance(resp, dict) and resp.get("code"):
            raise RuntimeError(f"cancel failed: {resp}")

    async def get_order(self, symbol: str, client_order_index: int) -> Dict[str, Any]:
        if not self._account:
            raise RuntimeError("account keys not configured for order query")
        resp = await self._account.get_open_order(symbol=symbol, client_id=client_order_index)
        return resp if isinstance(resp, dict) else {"raw": resp}

    async def get_positions(self) -> List[Dict[str, Any]]:
        if not self._account:
            return []
        resp = await self._account.get_open_positions()
        data = resp.get("data") if isinstance(resp, dict) else None
        return data or []

    async def get_margin(self) -> Dict[str, Any]:
        if not self._account:
            return {}
        balances = await self._account.get_balances()
        collateral = await self._account.get_collateral()
        return {
            "balances": balances,
            "collateral": collateral,
        }


__all__ = ["BackpackConnector"]
