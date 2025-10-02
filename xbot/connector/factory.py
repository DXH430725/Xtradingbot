from __future__ import annotations

import os
from pathlib import Path

from .backpack import BackpackConnector
from .grvt import GrvtConnector
from .lighter import LighterConnector
from .interface import IConnector
from pysdk.grvt_ccxt_env import GrvtEnv


def _default_key_path(filename: str) -> Path:
    base = Path(__file__).resolve().parents[2]
    return base / filename


def build_connector(venue: str) -> IConnector:
    normalized = venue.lower()
    if normalized == "backpack":
        key_file = Path(os.getenv("BACKPACK_KEY_FILE", _default_key_path("Backpack_key.txt")))
        return BackpackConnector(key_path=key_file)
    if normalized == "lighter":
        key_file = Path(os.getenv("LIGHTER_KEY_FILE", _default_key_path("Lighter_key.txt")))
        return LighterConnector(key_path=key_file)
    if normalized == "grvt":
        key_file = Path(os.getenv("GRVT_KEY_FILE", _default_key_path("Grvt_key.txt")))
        env_name = os.getenv("GRVT_ENV", "prod").lower()
        env = GrvtEnv[env_name.upper()] if env_name in {e.name.lower() for e in GrvtEnv} else GrvtEnv.PROD
        return GrvtConnector(key_path=key_file, env=env)
    raise ValueError(f"unsupported venue {venue}")


__all__ = ["build_connector"]
