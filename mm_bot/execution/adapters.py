from __future__ import annotations

from typing import Any, Optional


def get_api_key_index(connector: Any) -> Optional[int]:
    signer = getattr(connector, "signer", None)
    for attr in ("api_key_index", "apiKeyIndex", "api_key_id"):
        value = getattr(signer, attr, None)
        if value is not None:
            try:
                return int(value)
            except Exception:
                return None
    value = getattr(connector, "_api_key_index", None)
    if value is not None:
        try:
            return int(value)
        except Exception:
            return None
    return None


__all__ = ["get_api_key_index"]
