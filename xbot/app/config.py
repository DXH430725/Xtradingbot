from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

from execution.risk_service import RiskLimits
from core.heartbeat import HeartbeatConfig

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


@dataclass(slots=True)
class AppConfig:
    venue: str
    symbol: str
    mode: str
    qty: float
    side: str = "buy"
    price_offset_ticks: int = 0
    interval_secs: float = 10.0
    timeout_secs: float = 120.0
    reduce_only: int = 0
    symbol_map: Dict[str, str] = field(default_factory=dict)
    risk_limits: RiskLimits = field(default_factory=RiskLimits)
    heartbeat_config: Optional[HeartbeatConfig] = None


def _read_yaml(path: Path) -> Dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML not available; install pyyaml or avoid YAML configs")
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_config(
    *,
    venue: str,
    symbol: str,
    qty: float,
    side: str = "buy",
    mode: str = "market",
    price_offset_ticks: int = 0,
    interval_secs: float = 10.0,
    timeout_secs: float = 120.0,
    reduce_only: int = 0,
    config_path: Optional[str] = None,
) -> AppConfig:
    payload: Dict[str, Any] = {}
    if config_path:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(config_path)
        if path.suffix in {".yaml", ".yml"}:
            payload = _read_yaml(path)
        elif path.suffix == ".json":
            payload = _read_json(path)
        else:
            raise ValueError(f"unsupported config extension: {path.suffix}")
    cfg = AppConfig(
        venue=(payload.get("venue") or venue).lower(),
        symbol=payload.get("symbol") or symbol,
        mode=(payload.get("mode") or mode).lower(),
        qty=float(payload.get("qty") or qty),
        side=(payload.get("side") or side).lower(),
        price_offset_ticks=int(payload.get("price_offset_ticks") or price_offset_ticks),
        interval_secs=float(payload.get("interval_secs") or interval_secs),
        timeout_secs=float(payload.get("timeout_secs") or timeout_secs),
        reduce_only=int(payload.get("reduce_only") or reduce_only),
        symbol_map={k.upper(): v for k, v in (payload.get("symbol_map") or {}).items()},
    )
    cfg.symbol_map.setdefault(cfg.symbol.upper(), cfg.symbol)
    risk_cfg = payload.get("risk") or {}
    max_position = risk_cfg.get("max_position")
    max_notional = risk_cfg.get("max_notional")
    cfg.risk_limits = RiskLimits(
        max_position=None if max_position is None else Decimal(str(max_position)),
        max_notional=None if max_notional is None else Decimal(str(max_notional)),
    )
    heartbeat_cfg = payload.get("heartbeat") or {}
    if heartbeat_cfg.get("url"):
        cfg.heartbeat_config = HeartbeatConfig(
            url=heartbeat_cfg["url"],
            interval_secs=float(heartbeat_cfg.get("interval_secs", heartbeat_cfg.get("interval", 30.0))),
            timeout_secs=float(heartbeat_cfg.get("timeout_secs", 5.0)),
            bearer_token=heartbeat_cfg.get("token") or heartbeat_cfg.get("bearer_token"),
        )
    return cfg


__all__ = ["AppConfig", "load_config"]
