"""Utility helpers for pulling strongly-typed values from config dicts with env fallbacks."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional


def _from_cfg(cfg: Optional[Dict[str, Any]], key: str) -> Any:
    if not cfg:
        return None
    if key in cfg and cfg[key] is not None:
        return cfg[key]
    return None


def _from_env(env_var: Optional[str]) -> Any:
    if not env_var:
        return None
    if env_var in os.environ:
        return os.environ[env_var]
    return None


def get_str(cfg: Optional[Dict[str, Any]], key: str, *, env: Optional[str] = None, default: Optional[str] = None) -> Optional[str]:
    raw = _from_cfg(cfg, key)
    if raw is None and env:
        raw = _from_env(env)
    if raw is None:
        return default
    return str(raw)


def get_int(cfg: Optional[Dict[str, Any]], key: str, *, env: Optional[str] = None, default: Optional[int] = None) -> Optional[int]:
    raw = _from_cfg(cfg, key)
    if raw is None and env:
        raw = _from_env(env)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def get_float(cfg: Optional[Dict[str, Any]], key: str, *, env: Optional[str] = None, default: Optional[float] = None) -> Optional[float]:
    raw = _from_cfg(cfg, key)
    if raw is None and env:
        raw = _from_env(env)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def get_bool(cfg: Optional[Dict[str, Any]], key: str, *, env: Optional[str] = None, default: Optional[bool] = None) -> Optional[bool]:
    raw = _from_cfg(cfg, key)
    if raw is None and env:
        raw = _from_env(env)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    try:
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return default


def get_dict(cfg: Optional[Dict[str, Any]], key: str) -> Dict[str, Any]:
    raw = _from_cfg(cfg, key)
    return raw if isinstance(raw, dict) else {}


__all__ = [
    "get_str",
    "get_int",
    "get_float",
    "get_bool",
    "get_dict",
]
