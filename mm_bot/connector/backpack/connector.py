from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from nacl.encoding import RawEncoder
from nacl.signing import SigningKey

from mm_bot.connector.base import BaseConnector
from mm_bot.execution.orders import TrackingLimitOrder, TrackingMarketOrder
from mm_bot.utils.throttler import RateLimiter, lighter_default_weights

from .config import BackpackConfig, load_backpack_keys
from .rest import BackpackRESTMixin
from .ws import BackpackWSMixin


class BackpackConnector(BackpackRESTMixin, BackpackWSMixin, BaseConnector):
    """Backpack exchange connector composed from REST + WS mixins."""

    def __init__(self, config: Optional[BackpackConfig] = None, debug: bool = False) -> None:
        super().__init__("backpack", debug=debug)
        self.config = config or BackpackConfig()
        self.log = logging.getLogger("mm_bot.connector.backpack")

        self._throttler = RateLimiter(capacity_per_minute=self.config.rpm, weights=lighter_default_weights())
        self._session: Optional[aiohttp.ClientSession] = None
        self._started = False

        self._api_pub: Optional[str] = None
        self._api_priv: Optional[str] = None
        self._signing_key: Optional[SigningKey] = None
        self._verifying_key_b64: Optional[str] = None
        self._broker_id: str = self.config.broker_id

        self._symbol_to_market: Dict[str, str] = {}
        self._market_info: Dict[str, Dict[str, Any]] = {}
        self._price_decimals: Dict[str, int] = {}
        self._size_decimals: Dict[str, int] = {}

        self._ws_task: Optional[asyncio.Task] = None
        self._ws_stop: bool = False
        self._top_of_book_cache: Dict[str, Tuple[Optional[int], Optional[int], int, float]] = {}
        self._top_of_book_events: Dict[str, asyncio.Event] = {}
        self._depth_state: Dict[str, Dict[str, Any]] = {}
        self._positions_by_symbol: Dict[str, Dict[str, Any]] = {}

    def _init_symbol_mapping(self) -> Dict[str, str]:
        """Initialize Backpack-specific symbol mapping."""
        return {
            'BTC': 'BTC_USDC_PERP',
            'ETH': 'ETH_USDC_PERP',
            'SOL': 'SOL_USDC_PERP',
            'DOGE': 'DOGE_USDC_PERP',
            'WIF': 'WIF_USDC_PERP',
            'BONK': 'BONK_USDC_PERP',
            'JTO': 'JTO_USDC_PERP',
            'JUP': 'JUP_USDC_PERP',
            'RNDR': 'RNDR_USDC_PERP',
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self, core: Any | None = None) -> None:  # noqa: D401 - lifecycle
        if self._started:
            return
        pub, priv = load_backpack_keys(self.config.keys_file)
        if not pub or not priv:
            raise RuntimeError(f"Missing backpack keys; check {self.config.keys_file}")
        self._api_pub = pub
        self._api_priv = priv
        try:
            signing_bytes = base64.b64decode(priv)
            self._signing_key = SigningKey(signing_bytes)
            verifying_key = self._signing_key.verify_key.encode(RawEncoder)
            self._verifying_key_b64 = base64.b64encode(verifying_key).decode("utf-8")
        except Exception as exc:
            raise RuntimeError(f"Invalid backpack private key: {exc}")
        self._session = aiohttp.ClientSession()
        self._started = True

    async def close(self) -> None:
        await self.stop_ws_state()
        if self._session:
            await self._session.close()
            self._session = None
        self._started = False

    # ------------------------------------------------------------------
    # Connector facade (thin wrappers for type hints)
    # ------------------------------------------------------------------
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
        return await super().submit_limit_order(
            symbol,
            client_order_index,
            base_amount,
            price,
            is_ask,
            post_only=post_only,
            reduce_only=reduce_only,
        )

    async def submit_market_order(
        self,
        symbol: str,
        client_order_index: int,
        base_amount: int,
        is_ask: bool,
        *,
        reduce_only: int = 0,
    ) -> TrackingMarketOrder:
        return await super().submit_market_order(
            symbol,
            client_order_index,
            base_amount,
            is_ask,
            reduce_only=reduce_only,
        )


__all__ = ["BackpackConnector", "BackpackConfig", "load_backpack_keys"]
