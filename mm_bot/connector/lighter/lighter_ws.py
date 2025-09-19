import asyncio
import json
from typing import Callable, Dict, List, Optional

try:
    import lighter
except Exception:
    lighter = None  # set at runtime by connector


class LighterWS:
    """
    Thin wrapper around lighter.WsClient with async run loop management.
    """

    def __init__(
        self,
        order_book_ids: Optional[List[int]] = None,
        account_ids: Optional[List[int]] = None,
        on_order_book_update: Optional[Callable[[str, Dict], None]] = None,
        on_account_update: Optional[Callable[[str, Dict], None]] = None,
        host: Optional[str] = None,
    ):
        if lighter is None:
            raise RuntimeError("lighter SDK not available")
        self._client = lighter.WsClient(
            host=host,
            order_book_ids=order_book_ids or [],
            account_ids=account_ids or [],
            on_order_book_update=on_order_book_update,
            on_account_update=on_account_update,
        )
        self._task: Optional[asyncio.Task] = None

    def start(self):
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="lighter_ws")

    async def _run(self):
        await self._client.run_async()

    async def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

