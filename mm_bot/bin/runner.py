"""Unified entrypoint for running XTradingBot strategies."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any, Dict, List

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mm_bot.conf.config import load_config
from mm_bot.core.trading_core import TradingCore
from mm_bot.logger.logger import setup_logging
from mm_bot.runtime.config_utils import get_bool, get_float, get_dict
from mm_bot.runtime.registry import get_connector_spec, get_strategy_spec, list_strategies


def _default_logging_path() -> str:
    return os.path.join(ROOT, "mm_bot", "conf", "logging.yaml")


def _resolve_strategy_name(strategy_section: Dict[str, Any], override: str | None) -> str:
    if override:
        return override.lower()
    if not isinstance(strategy_section, dict):
        raise ValueError("strategy section missing or invalid; provide --strategy")
    name = strategy_section.get("name")
    if name:
        return str(name).lower()
    candidates = [k for k in strategy_section.keys() if k != "name"]
    if len(candidates) == 1:
        return str(candidates[0]).lower()
    raise ValueError("unable to resolve strategy name; specify strategy.name or use --strategy")


def _prefixed_slice(legacy: Dict[str, Any], prefixes: List[str], include_unprefixed: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in legacy.items():
        matched = False
        for prefix in prefixes:
            if key.startswith(prefix):
                out[key[len(prefix):]] = value
                matched = True
                break
        if matched:
            continue
        if include_unprefixed and "_" not in key:
            out[key] = value
    return out


def _connector_configs(cfg: Dict[str, Any], required: List[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    connectors_section = cfg.get("connectors")
    if isinstance(connectors_section, dict):
        for name in required:
            section = connectors_section.get(name)
            if isinstance(section, dict):
                out[name] = section
            else:
                # try case-insensitive lookup
                for key, value in connectors_section.items():
                    if isinstance(value, dict) and key.lower() == name.lower():
                        out[name] = value
                        break
    if len(out) == len(required):
        return out

    legacy = cfg.get("connector")
    if not isinstance(legacy, dict):
        legacy = {}
    if not required:
        return out
    if len(required) == 1:
        name = required[0]
        out.setdefault(name, legacy)
        return out

    primary = required[0]
    for name in required:
        prefixes = [f"{name}_"]
        if name == "backpack":
            prefixes.append("bp_")
        if name == "lighter":
            prefixes.append("lg_")
        include_unprefixed = name == primary
        slice_dict = _prefixed_slice(legacy, prefixes, include_unprefixed)
        out.setdefault(name, slice_dict)
    return out


def _print_available_strategies() -> None:
    print("Available strategies:")
    for spec in list_strategies():
        req = ", ".join(spec.required_connectors or (spec.resolve_connectors([], {}) if spec.resolve_connectors else []))
        meta = f" (connectors: {req})" if req else ""
        print(f"  - {spec.name}{meta}")


def _build_core(general_cfg: Dict[str, Any], strategy_name: str, debug_flag: bool) -> TradingCore:
    tick_size = get_float(general_cfg, "tick_size", env="XTB_TICK_SIZE", default=1.0) or 1.0
    core = TradingCore(tick_size=tick_size, debug=debug_flag)
    core.dbg(f"Core initialized for {strategy_name} tick_size={tick_size}")
    return core


def _resolve_debug(general_cfg: Dict[str, Any]) -> bool:
    flag = get_bool(general_cfg, "debug", env="XTB_DEBUG", default=None)
    return bool(flag) if flag is not None else False


def _setup_logging(general_cfg: Dict[str, Any]) -> None:
    log_cfg = general_cfg.get("log_config") or _default_logging_path()
    setup_logging(log_cfg)


async def main(argv: List[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Unified runner for XTradingBot strategies")
    parser.add_argument("--config", help="Path to YAML/JSON config file", default=os.getenv("XTB_CONFIG"))
    parser.add_argument("--strategy", help="Override strategy name defined in config")
    parser.add_argument("--list", action="store_true", help="List available strategies and exit")
    args = parser.parse_args(argv)

    if args.list:
        _print_available_strategies()
        return

    cfg_path = args.config
    if not cfg_path:
        default_path = os.path.join(ROOT, "mm_bot", "conf", "bot.yaml")
        cfg_path = default_path if os.path.exists(default_path) else None
    if not cfg_path:
        raise SystemExit("No configuration file provided")

    config = load_config(cfg_path)
    if not config:
        raise SystemExit(f"Failed to load configuration from {cfg_path}")

    general_cfg = config.get("general") or {}
    strategy_section = config.get("strategy") or {}
    strategy_name = _resolve_strategy_name(strategy_section, args.strategy)
    strategy_spec = get_strategy_spec(strategy_name)
    strategy_cfg = get_dict(strategy_section, strategy_name)

    _setup_logging(general_cfg)
    log = logging.getLogger("mm_bot.runner")
    debug_flag = _resolve_debug(general_cfg)
    if debug_flag:
        log.setLevel(logging.DEBUG)

    log.info("Bootstrapping strategy '%s' from %s", strategy_name, cfg_path)

    required_connectors = strategy_spec.resolve_connectors(strategy_cfg, general_cfg) if strategy_spec.resolve_connectors else strategy_spec.required_connectors
    required_connectors = [c.lower() for c in (required_connectors or [])]
    connector_cfg_map = _connector_configs(config, required_connectors)

    connectors: Dict[str, Any] = {}
    for connector_name in required_connectors:
        spec = get_connector_spec(connector_name)
        cfg = connector_cfg_map.get(connector_name, {})
        connector = spec.factory(cfg, general_cfg, debug_flag)
        start = getattr(connector, "start", None)
        if callable(start):
            start()
        connectors[connector_name] = connector
        log.debug("Connector '%s' instantiated", connector_name)

    if strategy_spec.prepare:
        await strategy_spec.prepare(connectors, strategy_cfg, general_cfg)

    core = _build_core(general_cfg, strategy_name, debug_flag)
    for name, connector in connectors.items():
        core.add_connector(name, connector)

    strategy = strategy_spec.factory(strategy_cfg, connectors, general_cfg)
    core.set_strategy(strategy)

    await core.start()
    log.info("Strategy '%s' started", strategy_name)
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        log.info("Interrupt received; shutting down")
    finally:
        await core.stop(cancel_orders=True)
        await core.shutdown(cancel_orders=True)
        log.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
