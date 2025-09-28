from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass
class BackpackConfig:
    base_url: str = "https://api.backpack.exchange"
    ws_url: str = "wss://ws.backpack.exchange"
    keys_file: str = "Backpack_key.txt"
    window_ms: int = 5000
    rpm: int = 300  # conservative default
    broker_id: str = "1500"


def load_backpack_keys(path: str) -> Tuple[Optional[str], Optional[str]]:
    pub = priv = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line.lower().startswith("api key:"):
                    pub = line.split(":", 1)[1].strip()
                elif line.lower().startswith("api secret:"):
                    priv = line.split(":", 1)[1].strip()
    except Exception:
        pass
    return pub, priv


__all__ = ["BackpackConfig", "load_backpack_keys"]
