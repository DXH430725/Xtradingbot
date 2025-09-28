from __future__ import annotations

import asyncio
import random
from typing import Optional


async def wait_random(min_secs: float, max_secs: float, *, rng: Optional[random.Random] = None) -> float:
    """Await a random delay between the provided bounds and return chosen delay."""

    if min_secs < 0 or max_secs < 0:
        raise ValueError("wait bounds must be non-negative")
    if max_secs < min_secs:
        min_secs, max_secs = max_secs, min_secs
    rng = rng or random
    delay = rng.uniform(min_secs, max_secs) if max_secs > min_secs else float(max_secs)
    if delay <= 0:
        return 0.0
    await asyncio.sleep(delay)
    return delay


__all__ = ["wait_random"]
