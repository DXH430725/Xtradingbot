from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Deque, Dict, Optional

from mm_bot.connector.base import BaseConnector
from mm_bot.utils.throttler import RateLimiter, lighter_default_weights

from .config import LighterConfig
from .lighter_auth import load_keys_from_file
from .rest import LighterRESTMixin
from .ws import LighterWSMixin


def _ensure_lighter_on_path(root: str) -> None:
    sdk_path = os.path.join(root, "lighter-python")
    if sdk_path not in sys.path:
        sys.path.insert(0, sdk_path)


_ensure_lighter_on_path(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import lighter  # type: ignore  # noqa: E402


class LighterConnector(LighterRESTMixin, LighterWSMixin, BaseConnector):
    """Lighter exchange connector composed from REST and WS mixins."""

    def __init__(self, config: Optional[LighterConfig] = None, debug: bool = False) -> None:
        super().__init__("lighter", debug=debug)
        self.config = config or LighterConfig()
        self.debug_enabled = debug
        self.log = logging.getLogger("mm_bot.connector.lighter")
        self._lighter = lighter

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
        self._market_info_by_symbol: Dict[str, Dict[str, Any]] = {}

        self._on_order_book_update: Optional[Any] = None
        self._on_account_update: Optional[Any] = None

        self._positions_by_market: Dict[int, Dict[str, Any]] = {}
        self._active_orders_by_market: Dict[int, Dict[int, Dict[str, Any]]] = {}
        self._order_index_by_coi: Dict[int, int] = {}
        self._trades_by_market: Dict[int, Deque[Dict[str, Any]]] = {}
        self._trades_maxlen: int = 1000
        self._finalized_order_indices: set[int] = set()

        self._inflight_by_coi: Dict[int, Dict[str, Any]] = {}
        self._inflight_by_idx: Dict[int, Dict[str, Any]] = {}
        self._account_positions: Dict[int, Dict[str, Any]] = {}

        self._ws_state_task: Optional[asyncio.Task] = None
        self._ws_state_stop: bool = False
        self._rest_reconcile_task: Optional[asyncio.Task] = None

    def start(self, core: Any | None = None) -> None:  # noqa: D401 - lifecycle hook
        if self._started:
            return
        api_key_index, api_priv, _api_pub, eth_priv = load_keys_from_file(self.config.keys_file)
        if api_priv is None or api_key_index is None:
            raise RuntimeError(f"Missing keys; check {self.config.keys_file}")

        self._api_key_index = api_key_index
        self._api_priv = api_priv
        self._eth_priv = eth_priv

        self.api_client = self._lighter.ApiClient(configuration=self._lighter.Configuration(host=self.config.base_url))
        self.account_api = self._lighter.AccountApi(self.api_client)
        self.order_api = self._lighter.OrderApi(self.api_client)
        self.tx_api = self._lighter.TransactionApi(self.api_client)

        self._started = True

    def stop(self, core: Any | None = None) -> None:
        self._started = False

    async def close(self) -> None:
        if self.api_client is not None:
            await self.api_client.close()
            self.api_client = None


__all__ = ["LighterConnector", "LighterConfig"]
