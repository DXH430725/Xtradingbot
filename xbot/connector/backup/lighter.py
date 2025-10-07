from __future__ import annotations

import sys
import asyncio
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import BaseConnector
from xbot.utils.logging import get_logger


# Try to make a vendored Lighter SDK importable
# Prefer sdk/lighter-python (repo layout), then fallback to sdk/lighter
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
        # Lighter SDK ApiClient (distinct from BaseConnector httpx client)
        self._api_client = None  # type: ignore
        self._signer = None  # type: ignore
        self._logger = get_logger(__name__)

    def _load_keys(self) -> Dict[str, str]:
        if not self._key_path.exists():
            return {}
        content = self._key_path.read_text(encoding="utf-8")
        lines = dict(
            line.split(":", 1)
            for line in content.splitlines()
            if ":" in line
        )
        # Expected keys: api_key_private_key, account_index, api_key_index
        return {k.strip().lower(): v.strip() for k, v in lines.items()}

    @staticmethod
    def _key_get(keys: Dict[str, str], *names: str) -> Optional[str]:
        for n in names:
            if n in keys and keys[n]:
                return keys[n]
        return None

    async def start(self) -> None:
        try:
            self._logger.info("lighter_sdk_import_start", extra={"paths": [str(p) for p in _sdk_paths]})
            import lighter  # type: ignore  # noqa: F401
            from lighter import ApiClient, Configuration  # type: ignore
            # Initialize shared API client (aiohttp under the hood) for public endpoints
            self._api_client = ApiClient(configuration=Configuration(host=self.base_url))
            # Load markets via order books snapshot
            order_api = lighter.OrderApi(self._api_client)  # type: ignore[attr-defined]
            ob = await order_api.order_books()
            self._logger.info("lighter_order_books_ok", extra={"has_sdk": True})
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
                # Add a convenient base-symbol alias if server returns composite symbols
                base = symbol.split("_")[0].split("-")[0]
                if base and base != symbol and base not in self._markets:
                    self._markets[base] = mi
            self._sdk_available = True
            self._logger.info(
                "lighter_markets_built",
                extra={
                    "count": len(self._markets),
                    "sample": list(self._markets.keys())[:10],
                },
            )
        except Exception:
            # SDK not present; allow public methods to raise when called
            self._sdk_available = False
            self._logger.exception("lighter_bootstrap_failed")
        await super().start()

    async def stop(self) -> None:
        # Close SDK ApiClient sessions to avoid unclosed aiohttp warnings
        try:
            if self._api_client is not None:
                await self._api_client.close()
        except Exception:
            pass
        try:
            if self._signer is not None and getattr(self._signer, "api_client", None) is not None:
                # SignerClient maintains its own ApiClient
                await self._signer.api_client.close()  # type: ignore[attr-defined]
        except Exception:
            pass
        # Also attempt to close any global default client if created implicitly
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
            # Help diagnose symbol mismatches
            keys = list(self._markets.keys())
            self._logger.info(
                "lighter_unknown_symbol",
                extra={"symbol": symbol, "known": keys[:20], "total": len(keys)},
            )
            raise ValueError(f"unknown market {symbol}")
        return self._markets[symbol]

    def get_market_index(self, symbol: str) -> int:
        """Return the market_id for a venue symbol or its base alias."""
        return self._get_market_info(symbol).market_id

    def get_account_index(self) -> Optional[int]:
        """Return account_index from key file if present, otherwise None."""
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
        # Lighter min size not readily exposed in the sample; fallback to 1 unit in integer scale
        _p, s = await self.get_price_size_decimals(symbol)
        return 1  # conservative default; adjust when SDK field available

    async def get_top_of_book(self, symbol: str) -> Tuple[Optional[int], Optional[int], int]:
        if not self._sdk_available:
            raise RuntimeError("Lighter SDK not available; cannot query order book")
        # Prefer WS for live top-of-book; fallback to OrderApi.order_book_orders(limit=1)
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
                px = obo.bids[0].price  # string
                best_bid = int(Decimal(px) * price_scale)
            if getattr(obo, "asks", None):
                px = obo.asks[0].price  # string
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
        # Map to SDK call (signature based on ref) — implement with your vendored SDK
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
        # Bypass buggy SDK wrapper; sign and send directly
        tx_info, error = self._signer.sign_create_order(
            info.market_id,
            client_order_index,
            base_amount_i,
            price_i,
            is_ask,
            self._signer.ORDER_TYPE_LIMIT,
            self._signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            int(bool(reduce_only)),
            0,
        )
        if error is not None:
            self._logger.info("lighter_submit_limit_failed", extra={"error": str(error)})
            raise RuntimeError(f"limit order failed: {error}")
        resp = await self._signer.send_tx(self._signer.TX_TYPE_CREATE_ORDER, tx_info)
        self._logger.info(
            "lighter_submit_limit_ack",
            extra={"coi": client_order_index, "code": getattr(resp, "code", None), "tx_hash": getattr(resp, "tx_hash", None)},
        )
        if getattr(resp, "code", None) != 200:
            raise RuntimeError(f"limit order rejected: code={getattr(resp, 'code', None)} msg={getattr(resp, 'message', None)}")
        # Try to resolve exchange order_id by querying active orders shortly after submit
        try:
            order_id = await self._resolve_order_id(info.market_id, client_order_index)
            if order_id:
                self._logger.info("lighter_submit_limit_resolved", extra={"coi": client_order_index, "order_id": order_id})
                return str(order_id)
        except Exception as exc:
            self._logger.info("lighter_submit_limit_lookup_error", extra={"error": str(exc)})
        # Fallback: return empty to allow client-id based cancel path
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
        await self._ensure_signer()
        info = self._get_market_info(symbol)
        self._logger.info(
            "lighter_submit_market",
            extra={
                "market_id": info.market_id,
                "coi": client_order_index,
                "size_i": int(size_i),
                "is_ask": is_ask,
                "reduce_only": reduce_only,
            },
        )
        tx_info, error = self._signer.sign_create_order(
            info.market_id,
            client_order_index,
            int(size_i),
            0,
            is_ask,
            self._signer.ORDER_TYPE_MARKET,
            self._signer.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            int(bool(reduce_only)),
            0,
        )
        if error is not None:
            self._logger.info("lighter_submit_market_failed", extra={"error": str(error)})
            raise RuntimeError(f"market order failed: {error}")
        resp = await self._signer.send_tx(self._signer.TX_TYPE_CREATE_ORDER, tx_info)
        self._logger.info(
            "lighter_submit_market_ack",
            extra={"coi": client_order_index, "code": getattr(resp, "code", None), "tx_hash": getattr(resp, "tx_hash", None)},
        )
        if getattr(resp, "code", None) != 200:
            raise RuntimeError(f"market order rejected: code={getattr(resp, 'code', None)} msg={getattr(resp, 'message', None)}")
        try:
            order_id = await self._resolve_order_id(info.market_id, client_order_index)
            if order_id:
                self._logger.info("lighter_submit_market_resolved", extra={"coi": client_order_index, "order_id": order_id})
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
        # Resolve order_index via REST active orders using an auth token
        order_index: Optional[int] = None
        try:
            import lighter  # type: ignore
            order_api = lighter.OrderApi(self._api_client)  # type: ignore[attr-defined]
            # Build auth token (10-min expiry)
            deadline = int(__import__("time").time() + 600)
            token, err = self._signer.create_auth_token_with_expiry(deadline)
            if err is not None:
                return {"error": f"auth_token_error: {err}"}
            orders = await order_api.account_active_orders(account_index=acct_idx, market_id=info.market_id, auth=token)
            for o in getattr(orders, "orders", []) or []:
                try:
                    coi_val = getattr(o, "client_order_index", None)
                    if coi_val is None:
                        continue
                    if int(coi_val) == int(client_order_index):
                        oi_val = getattr(o, "order_index", None)
                        if oi_val is not None:
                            order_index = int(oi_val)
                        break
                except Exception:
                    continue
        except Exception as exc:
            return {"error": f"lookup_failed: {exc}"}
        if order_index is None:
            return {"error": "order_not_found_for_client_index"}
        # Perform signed cancel by order_index
        _, tx_hash, err2 = await self._signer.cancel_order(market_index=info.market_id, order_index=order_index)
        if err2 is not None:
            return {"error": str(err2)}
        return {"tx": getattr(tx_hash, "tx_hash", None) or getattr(tx_hash, "hash", None) or str(tx_hash)}

    async def cancel_by_order_id(self, symbol: str, order_id: str) -> Dict[str, Any]:
        await self._ensure_signer()
        info = self._get_market_info(symbol)
        _, tx_hash, err = await self._signer.cancel_order(market_index=info.market_id, order_index=int(order_id))
        if err is not None:
            return {"error": str(err)}
        return {"tx": getattr(tx_hash, "tx_hash", None) or getattr(tx_hash, "hash", None) or str(tx_hash)}

    async def get_order(self, symbol: str, client_order_index: int) -> Dict[str, Any]:
        # Return a minimal OPEN state; WS will drive subsequent state transitions.
        return {"client_order_index": client_order_index, "status": "open", "state": "open", "symbol": symbol}

    async def get_positions(self) -> List[Dict[str, Any]]:
        # Placeholder; implement via SDK/account API when available
        return []

    async def get_margin(self) -> Dict[str, Any]:
        # Placeholder for balance/collateral snapshot via SDK
        return {}

    async def _ensure_signer(self) -> None:
        if not self._sdk_available:
            raise RuntimeError("Lighter SDK not available")
        if self._signer is not None:
            return
        # Lazy initialize SignerClient to avoid nonce HTTP during public bootstrap
        try:
            from lighter import SignerClient  # type: ignore
            keys = self._load_keys()
            priv = self._key_get(keys, "api_key_private_key", "apikeyprivatekey", "private_key", "privatekey") or ""
            acct = self._key_get(keys, "account_index", "accountindex") or "0"
            api_k = self._key_get(keys, "api_key_index", "apikeyindex") or "0"
            acct_idx = int(acct)
            key_idx = int(api_k)
            self._logger.info("signer_params", extra={"venue": "lighter", "account_index": acct_idx, "api_key_index": key_idx})
            self._signer = SignerClient(
                url=self.base_url,
                private_key=priv,
                account_index=acct_idx,
                api_key_index=key_idx,
            )
            # Validate signer client is ready (mirrors reference implementation)
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
        """After submit, poll active_orders briefly to resolve exchange order_index for the client id."""
        try:
            import lighter  # type: ignore
            order_api = lighter.OrderApi(self._api_client)  # type: ignore[attr-defined]
            # build short-lived auth token
            deadline = int(__import__("time").time() + 600)
            token, err = self._signer.create_auth_token_with_expiry(deadline)
            if err is not None:
                self._logger.info("lighter_auth_token_error", extra={"error": str(err)})
                return None
            acct_idx = self.get_account_index()
            if acct_idx is None:
                return None
            # poll a few times to allow order to appear
            for _ in range(5):
                try:
                    resp = await order_api.account_active_orders(account_index=acct_idx, market_id=market_id, auth=token)
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
