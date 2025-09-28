from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, Optional


class COIManager:
    """Manage client order indices with per-venue wrap-around limits."""

    DEFAULT_LIMIT = (1 << 32) - 1

    def __init__(self, *, default_limit: int | None = None, logger: Optional[logging.Logger] = None) -> None:
        self._default_limit = int(default_limit or self.DEFAULT_LIMIT)
        self._limits: Dict[str, int] = {}
        self._sequences: Dict[str, int] = {}
        self._lock = threading.Lock()
        self.log = logger or logging.getLogger("mm_bot.execution.coi")

    def register_limit(self, venue: str, limit: int) -> None:
        """Override the COI wrap-around limit for a venue."""
        with self._lock:
            self._limits[venue.lower()] = max(1, int(limit))
            # reset sequence to fall within new bounds if needed
            seq = self._sequences.get(venue.lower())
            if seq is not None and seq > self._limits[venue.lower()]:
                self._sequences[venue.lower()] = 1

    def seed(self, venue: str, value: Optional[int] = None) -> int:
        """Seed sequence for venue; returns the value that will be used next."""
        with self._lock:
            limit = self._limit_for(venue)
            seed = value if value is not None else self._time_seed(limit)
            seed = 1 if seed <= 0 else (seed % limit or 1)
            self._sequences[venue.lower()] = seed
            return seed

    def next(self, venue: str) -> int:
        """Return next client order index for a venue, handling wrap-around."""
        key = venue.lower()
        with self._lock:
            limit = self._limit_for(venue)
            current = self._sequences.get(key)
            if current is None or current <= 0 or current > limit:
                current = self._time_seed(limit)
            else:
                current += 1
                if current > limit:
                    current = 1
            self._sequences[key] = current
            return current

    def _limit_for(self, venue: str) -> int:
        return self._limits.get(venue.lower(), self._default_limit)

    def _time_seed(self, limit: int) -> int:
        seed = int(time.time() * 1000) % limit
        return seed or 1


class NonceManager:
    """Utility to inspect and refresh connector nonce managers."""

    def __init__(self, *, logger: Optional[logging.Logger] = None) -> None:
        self.log = logger or logging.getLogger("mm_bot.execution.nonce")

    def snapshot(self, connector: Any) -> Optional[str]:
        signer = getattr(connector, "signer", None)
        manager = getattr(signer, "nonce_manager", None)
        mapping = getattr(manager, "nonce", None)
        if isinstance(mapping, dict) and mapping:
            try:
                return ",".join(f"{k}:{mapping[k]}" for k in sorted(mapping))
            except Exception:
                return str(mapping)
        value = getattr(manager, "current_nonce", None)
        if value is not None:
            return str(value)
        return None

    async def refresh(self, connector: Any, api_key_index: Optional[int]) -> None:
        signer = getattr(connector, "signer", None)
        manager = getattr(signer, "nonce_manager", None)
        if manager is None or api_key_index is None:
            return
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, manager.hard_refresh_nonce, int(api_key_index))
            self.log.debug(
                "%s refreshed nonce api_key=%s nonce_state=%s",
                getattr(connector, "name", "connector"),
                api_key_index,
                self.snapshot(connector),
            )
        except Exception as exc:
            self.log.warning(
                "%s nonce refresh failed api_key=%s error=%s",
                getattr(connector, "name", "connector"),
                api_key_index,
                exc,
            )

    @staticmethod
    def is_nonce_error(info: Optional[Dict[str, Any]] = None, reason: Optional[str] = None) -> bool:
        reason_text = str(reason or "").lower()
        if "invalid nonce" in reason_text or "nonce" in reason_text and "refresh" in reason_text:
            return True
        if isinstance(info, dict):
            code = str(info.get("code") or "").strip()
            message = str(info.get("message") or info.get("error") or "").lower()
            if code in {"21104", "100001"}:
                return True
            if "nonce" in message and ("invalid" in message or "out of sync" in message):
                return True
        return False


__all__ = ["COIManager", "NonceManager"]
