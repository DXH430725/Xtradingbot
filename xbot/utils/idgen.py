from __future__ import annotations

import itertools
import secrets
from typing import Iterable


class ClientOrderIdGenerator:
    """Simple circular generator for connector-scoped client order indices."""

    def __init__(self, *, start: int | None = None, modulo: int = 1_000_000) -> None:
        if modulo <= 0:
            raise ValueError("modulo must be positive")
        seed = start if start is not None else secrets.randbelow(modulo)
        self._counter = itertools.count(seed)
        self._modulo = modulo

    def next(self) -> int:
        value = next(self._counter) % self._modulo
        return value if value > 0 else 1

    def batch(self, count: int) -> Iterable[int]:
        for _ in range(count):
            yield self.next()


__all__ = ["ClientOrderIdGenerator"]
