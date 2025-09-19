import asyncio
import time
from collections import defaultdict
from typing import Dict, Optional


class RateLimiter:
    """
    Simple token-bucket rate limiter with weighted requests.

    - capacity: total tokens in a 60s window
    - weights: per-endpoint weight mapping (defaults to 1)
    - burst: optional instantaneous burst allowance (defaults to capacity)
    """

    def __init__(
        self,
        capacity_per_minute: int = 4000,
        weights: Optional[Dict[str, int]] = None,
        burst: Optional[int] = None,
    ):
        self.capacity = max(1, int(capacity_per_minute))
        self.tokens = float(self.capacity)
        self.updated_at = time.monotonic()
        self.burst = float(burst if burst is not None else self.capacity)
        self.weights = {**(weights or {})}
        self._lock = asyncio.Lock()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self.updated_at
        # refill rate per second
        rate_per_sec = self.capacity / 60.0
        self.tokens = min(self.burst, self.tokens + elapsed * rate_per_sec)
        self.updated_at = now

    async def acquire(self, endpoint_key: str = "default"):
        weight = float(self.weights.get(endpoint_key, 1))
        async with self._lock:
            while True:
                self._refill()
                if self.tokens >= weight:
                    self.tokens -= weight
                    return
                # sleep just enough for one token-equivalent
                rate_per_sec = self.capacity / 60.0
                needed = (weight - self.tokens) / max(1e-6, rate_per_sec)
                await asyncio.sleep(max(0.01, min(1.0, needed)))


def lighter_default_weights() -> Dict[str, int]:
    """Weights adapted from lighter_rate_limit.txt.

    Keys are endpoint hints; use these when calling acquire().
    For non-listed endpoints, default to weight=1.
    """
    return {
        # transactions / nonce
        "/api/v1/sendTx": 6,
        "/api/v1/sendTxBatch": 6,
        "/api/v1/nextNonce": 6,
        # root/info
        "/": 10,
        "/info": 10,
        # public data
        "/api/v1/publicPools": 50,
        "/api/v1/txFromL1TxHash": 50,
        "/api/v1/candlesticks": 50,
        # account misc
        "/api/v1/accountActiveOrders": 300,
        "/api/v1/accountInactiveOrders": 300,
        "/api/v1/deposit/latest": 100,
        "/api/v1/pnl": 100,
        # api keys
        "/api/v1/apikeys": 150,
    }
