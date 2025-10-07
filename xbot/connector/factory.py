from __future__ import annotations

import os
from pathlib import Path

from .interface import IConnector


def _default_key_path(filename: str) -> Path:
    base = Path(__file__).resolve().parents[2]
    return base / filename


def build_connector(venue: str) -> IConnector:
    normalized = venue.lower()
    if normalized == "backpack":
        key_file = Path(os.getenv("BACKPACK_KEY_FILE", _default_key_path("Backpack_key.txt")))
        from .backpack import BackpackConnector
        return BackpackConnector(key_path=key_file)
    if normalized == "lighter":
        key_file = Path(os.getenv("LIGHTER_KEY_FILE", _default_key_path("Lighter_key.txt")))
        from .lighter import LighterConnector
        return LighterConnector(key_path=key_file)
    # Keep factory structure for parallelism; other venues can be added here.
    raise ValueError(f"unsupported venue {venue}")


__all__ = ["build_connector"]
