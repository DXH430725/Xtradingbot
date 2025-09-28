from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class LighterConfig:
    base_url: str = os.getenv("XTB_LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")
    keys_file: str = os.getenv(
        "XTB_LIGHTER_KEYS_FILE", os.path.abspath(os.path.join(os.getcwd(), "Lighter_key.txt"))
    )
    account_index: Optional[int] = (
        int(os.getenv("XTB_LIGHTER_ACCOUNT_INDEX", "-1")) if os.getenv("XTB_LIGHTER_ACCOUNT_INDEX") else None
    )
    rpm: int = int(os.getenv("XTB_LIGHTER_RPM", "4000"))


__all__ = ["LighterConfig"]
