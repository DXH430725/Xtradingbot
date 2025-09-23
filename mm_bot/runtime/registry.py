"""Central registry for connectors and strategies used by the new runner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from . import builders

ConnectorFactory = Callable[[Dict[str, Any], Dict[str, Any], bool], Any]
StrategyFactory = Callable[[Dict[str, Any], Dict[str, Any], Dict[str, Any]], Any]
PrepareHook = Callable[[Dict[str, Any], Dict[str, Any], Dict[str, Any]], Awaitable[None]]
ConnectorsResolver = Callable[[Dict[str, Any], Dict[str, Any]], List[str]]


@dataclass
class ConnectorSpec:
    name: str
    factory: ConnectorFactory
    description: str = ""


@dataclass
class StrategySpec:
    name: str
    factory: StrategyFactory
    required_connectors: List[str]
    description: str = ""
    prepare: Optional[PrepareHook] = None
    resolve_connectors: Optional[ConnectorsResolver] = None


_CONNECTORS: Dict[str, ConnectorSpec] = {}
_STRATEGIES: Dict[str, StrategySpec] = {}


def _bootstrap() -> None:
    global _CONNECTORS, _STRATEGIES
    if _CONNECTORS:
        return

    for name, entry in builders.CONNECTOR_BUILDERS.items():
        _CONNECTORS[name] = ConnectorSpec(
            name=name,
            factory=entry["factory"],
            description=entry.get("description", ""),
        )

    for name, entry in builders.STRATEGY_BUILDERS.items():
        _STRATEGIES[name] = StrategySpec(
            name=name,
            factory=entry["factory"],
            required_connectors=list(entry.get("requires", [])),
            description=entry.get("description", ""),
            prepare=entry.get("prepare"),
            resolve_connectors=entry.get("resolve_connectors"),
        )


def get_connector_spec(name: str) -> ConnectorSpec:
    _bootstrap()
    key = name.lower()
    if key not in _CONNECTORS:
        raise KeyError(f"Unknown connector '{name}'")
    return _CONNECTORS[key]


def get_strategy_spec(name: str) -> StrategySpec:
    _bootstrap()
    key = name.lower()
    if key not in _STRATEGIES:
        raise KeyError(f"Unknown strategy '{name}'")
    return _STRATEGIES[key]


def list_strategies() -> List[StrategySpec]:
    _bootstrap()
    return list(_STRATEGIES.values())


def list_connectors() -> List[ConnectorSpec]:
    _bootstrap()
    return list(_CONNECTORS.values())


__all__ = [
    "ConnectorSpec",
    "StrategySpec",
    "get_connector_spec",
    "get_strategy_spec",
    "list_strategies",
    "list_connectors",
]
