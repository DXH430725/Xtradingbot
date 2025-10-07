from __future__ import annotations

import sys
import asyncio
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import BaseConnector
from xbot.utils.logging import get_logger


# Prefer vendored SDK: sdk/lighter-python (fallback to sdk/lighter)
_repo_root = Path(__file__).resolve().parents[2]
_sdk_paths = [
    _repo_root / "sdk" / "lighter-python",
    _repo_root / "sdk" / "lighter",
]
for _p in _sdk_paths:
    if _p.exists():
        sp = str(_p)
        if sp not in sys.path:
            sys.path.insert(0, sp)


@dataclass
class _MarketInfo:
    symbol: str
    market_id: int
    price_decimals: int
    size_decimals: int
    min_quantity: Decimal | None = None


class LighterConnector(BaseConnector):
    base_url = "https://mainnet.zklighter.elliot.ai"

    def __init__(self, *, key_path: Path) -> None:
        super().__init__("lighter")
        self._key_path = key_path
        self._markets: Dict[str, _MarketInfo] = {}
        self._sdk_available = False
        self._api_client = None  # set when SDK available
        self._signer = None
        self._logger = get_logger(__name__)

    def _load_keys(self) -> Dict[str, str]:
        if not self._key_path.exists():
            return {}
        content = self._key_path.read_text(encoding="utf-8")
        lines = dict(
            line.split(":", 1) for line in content.splitlines() if ":" in line
        )
        return {k.strip().lower(): v.strip() for k, v in lines.items()}

    @staticmethod
    def _key_get(keys: Dict[str, str], *names: str) -> Optional[str]:
        for n in names:
            if n in keys and keys[n]:
                return keys[n]
        return None

    async def start(self) -> None:
        try:
            self._logger.info(
                "lighter_sdk_import_start", extra={"paths": [str(p) for p in _sdk_paths]}
            )
            import lighter  # type: ignore  # noqa: F401
            from lighter import ApiClient, Configuration  # type: ignore

            self._api_client = ApiClient(configuration=Configuration(host=self.base_url))
            order_api = lighter.OrderApi(self._api_client)  # type: ignore[attr-defined]
            ob = await order_api.order_books()
            for m in getattr(ob, "order_books", []):
                symbol = getattr(m, "symbol", None)
                if not symbol:
                    continue
                mi = _MarketInfo(
                    symbol=symbol,
                    market_id=getattr(m, "market_id", 0),
                    price_decimals=getattr(m, "supported_price_decimals", 0),
                    size_decimals=getattr(m, "supported_size_decimals", 0),
                )
                self._markets[symbol] = mi
                base = symbol.split("_")[0].split("-")[0]
                if base and base != symbol and base not in self._markets:
                    self._markets[base] = mi
            self._sdk_available = True
            self._logger.info(
                "lighter_markets_built",
                extra={"count": len(self._markets), "sample": list(self._markets.keys())[:10]},
            )
        except Exception:
            self._sdk_available = False
            self._logger.exception("lighter_bootstrap_failed")
        await super().start()

    async def stop(self) -> None:
        try:
            if self._api_client is not None:
                await self._api_client.close()
        except Exception:
            pass
        try:
            if self._signer is not None and getattr(self._signer, "api_client", None) is not None:
                await self._signer.api_client.close()  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            import lighter  # type: ignore
            default_client = lighter.ApiClient.get_default()
            if default_client is not None:
                await default_client.close()
                lighter.ApiClient.set_default(None)  # type: ignore[arg-type]
        except Exception:
            pass
        await super().stop()

    def _get_market_info(self, symbol: str) -> _MarketInfo:
        if symbol not in self._markets:
            keys = list(self._markets.keys())
            self._logger.info(
                "lighter_unknown_symbol",
                extra={"symbol": symbol, "known": keys[:20], "total": len(keys)},
            )
            raise ValueError(f"unknown market {symbol}")
        return self._markets[symbol]

    def get_market_index(self, symbol: str) -> int:
        return self._get_market_info(symbol).market_id

    def get_account_index(self) -> Optional[int]:
        try:
            keys = self._load_keys()
            v = self._key_get(keys, "account_index", "accountindex")
            return int(v) if v is not None and v != "" else None
        except Exception:
            return None

    async def get_price_size_decimals(self, symbol: str) -> Tuple[int, int]:
        info = self._get_market_info(symbol)
        return info.price_decimals, info.size_decimals

    async def get_min_size_i(self, symbol: str) -> int:
        _p, _s = await self.get_price_size_decimals(symbol)
        return 1

    async def get_top_of_book(self, symbol: str) -> Tuple[Optional[int], Optional[int], int]:
        if not self._sdk_available:
            raise RuntimeError("Lighter SDK not available; cannot query order book")
        import lighter  # type: ignore
        info = self._get_market_info(symbol)
        price_scale = 10 ** info.price_decimals
        order_api = lighter.OrderApi(self._api_client)  # type: ignore[attr-defined]
        try:
            obo = await order_api.order_book_orders(info.market_id, 1)
        except Exception:
            return None, None, price_scale
        best_bid = None
        best_ask = None
        try:
            if getattr(obo, "bids", None):
                px = obo.bids[0].price
                best_bid = int(Decimal(px) * price_scale)
            if getattr(obo, "asks", None):
                px = obo.asks[0].price
                best_ask = int(Decimal(px) * price_scale)
        except Exception:
            pass
        return best_bid, best_ask, price_scale

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
        await self._ensure_signer()
        info = self._get_market_info(symbol)
        base_amount_i = int(base_amount)
        price_i = int(price)
        self._logger.info(
            "lighter_submit_limit",
            extra={
                "market_id": info.market_id,
                "coi": client_order_index,
                "base_amount": base_amount_i,
                "price": price_i,
                "is_ask": is_ask,
                "post_only": post_only,
                "reduce_only": reduce_only,
            },
        )
        if base_amount_i < 10:
            self._logger.info(
                "lighter_size_warn",
                extra={"base_amount_i": base_amount_i, "note": "size may be below venue minimum"},
            )
        tif = (
            self._signer.ORDER_TIME_IN_FORCE_POST_ONLY
            if post_only
            else self._signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
        )
        tx_info, error = self._signer.sign_create_order(  # type: ignore[attr-defined]
            info.market_id,
            client_order_index,
            base_amount_i,
            price_i,
            is_ask,
            self._signer.ORDER_TYPE_LIMIT,
            tif,
            int(bool(reduce_only)),
            0,
            self._signer.DEFAULT_28_DAY_ORDER_EXPIRY,
        )
        if error is not None:
            self._logger.info("lighter_submit_limit_failed", extra={"error": str(error)})
            raise RuntimeError(f"limit order failed: {error}")
        resp = await self._signer.send_tx(self._signer.TX_TYPE_CREATE_ORDER, tx_info)
        self._logger.info(
            "lighter_submit_limit_ack",
            extra={
                "coi": client_order_index,
                "code": getattr(resp, "code", None),
                "tx_hash": getattr(resp, "tx_hash", None),
            },
        )
        if getattr(resp, "code", None) != 200:
            raise RuntimeError(
                f"limit order rejected: code={getattr(resp, 'code', None)} msg={getattr(resp, 'message', None)}"
            )
        try:
            order_id = await self._resolve_order_id(info.market_id, client_order_index)
            if order_id:
                self._logger.info(
                    "lighter_submit_limit_resolved", extra={"coi": client_order_index, "order_id": order_id}
                )
                return str(order_id)
        except Exception as exc:
            self._logger.info("lighter_submit_limit_lookup_error", extra={"error": str(exc)})
        return ""

    async def submit_market_order(
        self,
        *,
        symbol: str,
        client_order_index: int,
        size_i: int,
        is_ask: bool,
        reduce_only: int = 0,
    ) -> str:
        """Emulate market via LIMIT+IOC at top-of-book price.

        The Lighter signer rejects price < 1 even for MARKET type. We therefore
        submit a LIMIT order with IOC at best bid/ask to achieve market-like behaviour.
        """
        await self._ensure_signer()
        info = self._get_market_info(symbol)
        # Fetch best prices; ensure a valid integer price >= 1
        bid, ask, scale = await self.get_top_of_book(symbol)
        price_i: int
        if is_ask:
            # Sell: cross the bid
            if bid is None or bid <= 0:
                # Fallback: use minimal valid price to satisfy validation
                price_i = max(1, 1)
            else:
                price_i = int(bid)
        else:
            # Buy: cross the ask
            if ask is None or ask <= 0:
                # Fallback: minimal valid price
                price_i = max(1, 1)
            else:
                price_i = int(ask)

        self._logger.info(
            "lighter_submit_market",
            extra={
                "market_id": info.market_id,
                "coi": client_order_index,
                "size_i": int(size_i),
                "is_ask": is_ask,
                "reduce_only": reduce_only,
                "price_i": price_i,
                "mode": "limit+ioc",
            },
        )
        # Prefer MARKET type with IOC and non-zero avg price to satisfy signer validation
        tx_info, error = self._signer.sign_create_order(  # type: ignore[attr-defined]
            info.market_id,
            client_order_index,
            int(size_i),
            int(price_i),
            is_ask,
            self._signer.ORDER_TYPE_MARKET,
            self._signer.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            int(bool(reduce_only)),
            0,
            self._signer.DEFAULT_IOC_EXPIRY,
        )
        if error is not None:
            self._logger.info("lighter_submit_market_failed", extra={"error": str(error)})
            raise RuntimeError(f"market order failed: {error}")
        resp = await self._signer.send_tx(self._signer.TX_TYPE_CREATE_ORDER, tx_info)
        self._logger.info(
            "lighter_submit_market_ack",
            extra={
                "coi": client_order_index,
                "code": getattr(resp, "code", None),
                "tx_hash": getattr(resp, "tx_hash", None),
            },
        )
        if getattr(resp, "code", None) != 200:
            raise RuntimeError(
                f"market order rejected: code={getattr(resp, 'code', None)} msg={getattr(resp, 'message', None)}"
            )
        try:
            order_id = await self._resolve_order_id(info.market_id, client_order_index)
            if order_id:
                self._logger.info(
                    "lighter_submit_market_resolved", extra={"coi": client_order_index, "order_id": order_id}
                )
                return str(order_id)
        except Exception as exc:
            self._logger.info("lighter_submit_market_lookup_error", extra={"error": str(exc)})
        return ""

    async def cancel_by_client_id(self, symbol: str, client_order_index: int) -> Dict[str, Any]:
        await self._ensure_signer()
        info = self._get_market_info(symbol)
        acct_idx = self.get_account_index()
        if acct_idx is None:
            return {"error": "missing account_index for cancel"}
        order_index: Optional[int] = None
        try:
            import lighter  # type: ignore
            order_api = lighter.OrderApi(self._api_client)  # type: ignore[attr-defined]
            deadline = int(__import__("time").time() + 600)
            token, err = self._signer.create_auth_token_with_expiry(deadline)
            if err is not None:
                return {"error": f"auth_token_error: {err}"}
            orders = await order_api.account_active_orders(
                account_index=acct_idx, market_id=info.market_id, auth=token
            )
            for o in getattr(orders, "orders", []) or []:
                try:
                    coi_val = int(getattr(o, "client_order_index", -1))
                    if coi_val == int(client_order_index):
                        oi = getattr(o, "order_index", None)
                        order_index = int(oi) if oi is not None else None
                        break
                except Exception:
                    continue
        except Exception:
            pass
        if order_index is None:
            return {"error": "order_index_not_found"}
        _, tx_hash, err2 = await self._signer.cancel_order(  # type: ignore[attr-defined]
            market_index=info.market_id, order_index=order_index
        )
        if err2 is not None:
            return {"error": str(err2)}
        return {
            "tx": getattr(tx_hash, "tx_hash", None) or getattr(tx_hash, "hash", None) or str(tx_hash)
        }

    async def cancel_by_order_id(self, symbol: str, order_id: str) -> Dict[str, Any]:
        await self._ensure_signer()
        info = self._get_market_info(symbol)
        _, tx_hash, err = await self._signer.cancel_order(  # type: ignore[attr-defined]
            market_index=info.market_id, order_index=int(order_id)
        )
        if err is not None:
            return {"error": str(err)}
        return {
            "tx": getattr(tx_hash, "tx_hash", None) or getattr(tx_hash, "hash", None) or str(tx_hash)
        }

    async def get_order(self, symbol: str, client_order_index: int) -> Dict[str, Any]:
        return {
            "client_order_index": client_order_index,
            "status": "open",
            "state": "open",
            "symbol": symbol,
        }

    async def get_positions(self) -> List[Dict[str, Any]]:
        return []

    async def get_margin(self) -> Dict[str, Any]:
        return {}

    async def _ensure_signer(self) -> None:
        if not self._sdk_available:
            raise RuntimeError("Lighter SDK not available")
        if self._signer is not None:
            return
        try:
            from lighter import SignerClient  # type: ignore
            keys = self._load_keys()
            priv = self._key_get(
                keys, "api_key_private_key", "apikeyprivatekey", "private_key", "privatekey"
            ) or ""
            acct = self._key_get(keys, "account_index", "accountindex") or "0"
            api_k = self._key_get(keys, "api_key_index", "apikeyindex") or "0"
            acct_idx = int(acct)
            key_idx = int(api_k)
            self._logger.info(
                "signer_params",
                extra={"venue": "lighter", "account_index": acct_idx, "api_key_index": key_idx},
            )
            self._signer = SignerClient(
                url=self.base_url,
                private_key=priv,
                account_index=acct_idx,
                api_key_index=key_idx,
            )
            try:
                err = self._signer.check_client()
            except Exception:
                self._logger.exception("lighter_signer_check_failed")
                raise
            if err is not None:
                raise RuntimeError(f"Lighter SignerClient check failed: {err}")
        except Exception:
            self._logger.exception("lighter_signer_init_failed")
            raise

    async def _resolve_order_id(self, market_id: int, client_order_index: int) -> Optional[int]:
        try:
            import lighter  # type: ignore
            order_api = lighter.OrderApi(self._api_client)  # type: ignore[attr-defined]
            deadline = int(__import__("time").time() + 600)
            token, err = self._signer.create_auth_token_with_expiry(deadline)
            if err is not None:
                self._logger.info("lighter_auth_token_error", extra={"error": str(err)})
                return None
            acct_idx = self.get_account_index()
            if acct_idx is None:
                return None
            for _ in range(5):
                try:
                    resp = await order_api.account_active_orders(
                        account_index=acct_idx, market_id=market_id, auth=token
                    )
                    for o in getattr(resp, "orders", []) or []:
                        try:
                            if int(getattr(o, "client_order_index", -1)) == int(client_order_index):
                                oi = getattr(o, "order_index", None)
                                return int(oi) if oi is not None else None
                        except Exception:
                            continue
                except Exception:
                    pass
                await asyncio.sleep(0.2)
        except Exception as exc:
            self._logger.info("lighter_resolve_order_id_error", extra={"error": str(exc)})
        return None


__all__ = ["LighterConnector"]
