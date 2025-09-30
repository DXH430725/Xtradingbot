"""Component builders used by the unified runner."""

from __future__ import annotations

import os
from typing import Any, Dict, List

from .config_utils import get_bool, get_dict, get_float, get_int, get_str


# ---------------------------------------------------------------------------
# Connector builders
# ---------------------------------------------------------------------------

def build_backpack_connector(cfg: Dict[str, Any], general: Dict[str, Any], debug: bool) -> Any:
    from mm_bot.connector.backpack import BackpackConfig, BackpackConnector

    defaults = BackpackConfig()
    keys_default = os.path.join(os.getcwd(), defaults.keys_file)
    config = BackpackConfig(
        base_url=get_str(cfg, "base_url", env="XTB_BACKPACK_BASE_URL", default=defaults.base_url) or defaults.base_url,
        ws_url=get_str(cfg, "ws_url", env="XTB_BACKPACK_WS_URL", default=defaults.ws_url) or defaults.ws_url,
        keys_file=get_str(cfg, "keys_file", env="XTB_BACKPACK_KEYS_FILE", default=keys_default) or keys_default,
        window_ms=get_int(cfg, "window_ms", env="XTB_BACKPACK_WINDOW_MS", default=defaults.window_ms) or defaults.window_ms,
        rpm=get_int(cfg, "rpm", env="XTB_BACKPACK_RPM", default=defaults.rpm) or defaults.rpm,
        broker_id=get_str(cfg, "broker_id", env="XTB_BACKPACK_BROKER_ID", default=defaults.broker_id) or defaults.broker_id,
    )
    return BackpackConnector(config=config, debug=debug)


def build_lighter_connector(cfg: Dict[str, Any], general: Dict[str, Any], debug: bool) -> Any:
    from mm_bot.connector.lighter import LighterConfig, LighterConnector

    defaults = LighterConfig()
    keys_default = get_str(cfg, "keys_file") or defaults.keys_file or os.path.join(os.getcwd(), "Lighter_key.txt")
    account_index = get_int(cfg, "account_index", env="XTB_LIGHTER_ACCOUNT_INDEX", default=None)
    config = LighterConfig(
        base_url=get_str(cfg, "base_url", env="XTB_LIGHTER_BASE_URL", default=defaults.base_url) or defaults.base_url,
        keys_file=keys_default,
        account_index=account_index if account_index is not None else defaults.account_index,
        rpm=get_int(cfg, "rpm", env="XTB_LIGHTER_RPM", default=defaults.rpm) or defaults.rpm,
    )
    return LighterConnector(config=config, debug=debug)


def build_grvt_connector(cfg: Dict[str, Any], general: Dict[str, Any], debug: bool) -> Any:
    from mm_bot.connector.grvt.grvt_exchange import GrvtConfig, GrvtConnector

    defaults = GrvtConfig()
    config = GrvtConfig(
        base_url=get_str(cfg, "base_url", env="XTB_GRVT_BASE_URL", default=defaults.base_url) or defaults.base_url,
    )
    return GrvtConnector(config=config, debug=debug)


CONNECTOR_BUILDERS: Dict[str, Dict[str, Any]] = {
    "backpack": {
        "factory": build_backpack_connector,
        "description": "Backpack REST/WS connector",
    },
    "lighter": {
        "factory": build_lighter_connector,
        "description": "Lighter exchange connector",
    },
    "lighter1": {
        "factory": build_lighter_connector,
        "description": "Lighter exchange connector #1",
    },
    "lighter2": {
        "factory": build_lighter_connector,
        "description": "Lighter exchange connector #2",
    },
    "grvt": {
        "factory": build_grvt_connector,
        "description": "GRVT exchange connector",
    },
}


# ---------------------------------------------------------------------------
# Strategy builders
# ---------------------------------------------------------------------------


def build_liquidation_hedge_strategy(cfg: Dict[str, Any], connectors: Dict[str, Any], general: Dict[str, Any]) -> Any:
    from mm_bot.strategy.liquidation_hedge import LiquidationHedgeParams, LiquidationHedgeStrategy

    params_cfg = get_dict(cfg, "liquidation_hedge") or cfg
    base_params = LiquidationHedgeParams()

    def pick_float(key: str, default: float) -> float:
        val = get_float(params_cfg, key, default=None)
        return float(default if val is None else val)

    def pick_int(key: str, default: int) -> int:
        val = get_float(params_cfg, key, default=None)
        return int(default if val is None else val)

    test_mode_cfg = get_bool(params_cfg, "test_mode")

    params = LiquidationHedgeParams(
        backpack_symbol=get_str(params_cfg, "backpack_symbol", default=base_params.backpack_symbol) or base_params.backpack_symbol,
        lighter_symbol=get_str(params_cfg, "lighter_symbol", default=base_params.lighter_symbol) or base_params.lighter_symbol,
        leverage=pick_float("leverage", base_params.leverage),
        direction=get_str(params_cfg, "direction", default=base_params.direction) or base_params.direction,
        price_offset_ticks=pick_int("price_offset_ticks", base_params.price_offset_ticks),
        tracking_cancel_wait_secs=pick_float("tracking_cancel_wait_secs", base_params.tracking_cancel_wait_secs),
        tracking_post_only=get_bool(params_cfg, "tracking_post_only", default=base_params.tracking_post_only),
        timeout_secs=pick_float("timeout_secs", base_params.timeout_secs),
        poll_interval_secs=pick_float("poll_interval_secs", base_params.poll_interval_secs),
        reduce_only_buffer_ticks=pick_int("reduce_only_buffer_ticks", base_params.reduce_only_buffer_ticks),
        reverse_on_timeout=get_bool(params_cfg, "reverse_on_timeout", default=base_params.reverse_on_timeout),
        max_cycles=pick_int("max_cycles", base_params.max_cycles),
        liquidation_tolerance=pick_float("liquidation_tolerance", base_params.liquidation_tolerance),
        min_collateral=pick_float("min_collateral", base_params.min_collateral),
        backpack_entry_max_attempts=pick_int("backpack_entry_max_attempts", base_params.backpack_entry_max_attempts),
        backpack_retry_delay_secs=pick_float("backpack_retry_delay_secs", base_params.backpack_retry_delay_secs),
        lighter_max_slippage=pick_float("lighter_max_slippage", base_params.lighter_max_slippage),
        lighter_hedge_max_attempts=pick_int("lighter_hedge_max_attempts", base_params.lighter_hedge_max_attempts),
        lighter_retry_delay_secs=pick_float("lighter_retry_delay_secs", base_params.lighter_retry_delay_secs),
        wait_min_secs=pick_float("wait_min_secs", base_params.wait_min_secs),
        wait_max_secs=pick_float("wait_max_secs", base_params.wait_max_secs),
        confirmation_timeout_secs=pick_float("confirmation_timeout_secs", base_params.confirmation_timeout_secs),
        confirmation_poll_secs=pick_float("confirmation_poll_secs", base_params.confirmation_poll_secs),
        rebalance_max_attempts=pick_int("rebalance_max_attempts", base_params.rebalance_max_attempts),
        rebalance_retry_delay_secs=pick_float("rebalance_retry_delay_secs", base_params.rebalance_retry_delay_secs),
        telemetry_enabled=get_bool(params_cfg, "telemetry_enabled", default=base_params.telemetry_enabled),
        telemetry_config_path=get_str(
            params_cfg,
            "telemetry_config_path",
            default=base_params.telemetry_config_path,
        )
        or base_params.telemetry_config_path,
        telemetry_interval_secs=pick_float("telemetry_interval_secs", base_params.telemetry_interval_secs),
        telegram_enabled=get_bool(params_cfg, "telegram_enabled", default=base_params.telegram_enabled),
        telegram_keys_path=get_str(params_cfg, "telegram_keys_path", default=base_params.telegram_keys_path) or base_params.telegram_keys_path,
        test_mode=test_mode_cfg if test_mode_cfg is not None else base_params.test_mode,
    )
    backpack = connectors.get("backpack")
    lighter1 = connectors.get("lighter1") or connectors.get("lighter")
    lighter2 = connectors.get("lighter2")
    if lighter1 is None or lighter2 is None:
        raise RuntimeError("Liquidation hedge requires lighter1 and lighter2 connectors")
    return LiquidationHedgeStrategy(backpack_connector=backpack, lighter1_connector=lighter1, lighter2_connector=lighter2, params=params)


def build_simple_hedge_strategy(cfg: Dict[str, Any], connectors: Dict[str, Any], general: Dict[str, Any]) -> Any:
    from mm_bot.strategy.simple_bp_lighter_hedge import SimpleBackpackLighterHedgeStrategy, SimpleHedgeParams

    params_cfg = get_dict(cfg, "simple_hedge") or cfg
    base = SimpleHedgeParams()
    params = SimpleHedgeParams(
        backpack_symbol=get_str(params_cfg, "backpack_symbol", default=base.backpack_symbol) or base.backpack_symbol,
        lighter_symbol=get_str(params_cfg, "lighter_symbol", default=base.lighter_symbol) or base.lighter_symbol,
        direction=get_str(params_cfg, "direction", default=base.direction) or base.direction,
        leverage=get_float(params_cfg, "leverage", default=base.leverage) or base.leverage,
        tracking_interval_secs=get_float(params_cfg, "tracking_interval_secs", default=base.tracking_interval_secs)
        or base.tracking_interval_secs,
        tracking_timeout_secs=get_float(params_cfg, "tracking_timeout_secs", default=base.tracking_timeout_secs)
        or base.tracking_timeout_secs,
        price_offset_ticks=get_int(params_cfg, "price_offset_ticks", default=base.price_offset_ticks)
        or base.price_offset_ticks,
        hold_min_secs=get_float(params_cfg, "hold_min_secs", default=base.hold_min_secs) or base.hold_min_secs,
        hold_max_secs=get_float(params_cfg, "hold_max_secs", default=base.hold_max_secs) or base.hold_max_secs,
        cooldown_min_secs=get_float(params_cfg, "cooldown_min_secs", default=base.cooldown_min_secs)
        or base.cooldown_min_secs,
        cooldown_max_secs=get_float(params_cfg, "cooldown_max_secs", default=base.cooldown_max_secs)
        or base.cooldown_max_secs,
        margin_threshold=get_float(params_cfg, "margin_threshold", default=base.margin_threshold)
        or base.margin_threshold,
        telegram_enabled=get_bool(params_cfg, "telegram_enabled", default=base.telegram_enabled),
        telegram_keys_path=get_str(params_cfg, "telegram_keys_path", default=base.telegram_keys_path) or base.telegram_keys_path,
        test_mode=get_bool(params_cfg, "test_mode", default=base.test_mode),
    )
    backpack = connectors.get("backpack")
    lighter = connectors.get("lighter") or connectors.get("lighter1")
    if backpack is None or lighter is None:
        raise RuntimeError("Simple hedge requires backpack and lighter connectors")
    return SimpleBackpackLighterHedgeStrategy(backpack_connector=backpack, lighter_connector=lighter, params=params)


def build_connector_test_strategy(cfg: Dict[str, Any], connectors: Dict[str, Any], general: Dict[str, Any]) -> Any:
    from mm_bot.strategy.smoke_test import ConnectorTestStrategy, ConnectorTestParams, ConnectorTestTask

    params_cfg = get_dict(cfg, "connector_test") or cfg
    tasks_cfg = params_cfg.get("tasks", [])
    tasks: List[ConnectorTestTask] = []
    default_symbol = get_str(general, "symbol", env="XTB_SYMBOL", default="BTC_USDC_PERP")
    if isinstance(tasks_cfg, list):
        for item in tasks_cfg:
            if not isinstance(item, dict):
                continue
            venue = get_str(item, "venue")
            symbol = get_str(item, "symbol", default=default_symbol)
            if not venue or not symbol:
                continue
            tasks.append(
                ConnectorTestTask(
                    venue=str(venue),
                    symbol=str(symbol),
                    order_type=get_str(item, "order_type", default="market") or "market",
                    side=get_str(item, "side", default="buy") or "buy",
                    min_multiplier=get_float(item, "min_multiplier", default=1.0) or 1.0,
                    price_offset_ticks=get_int(item, "price_offset_ticks", default=0) or 0,
                    tracking_interval_secs=get_float(item, "tracking_interval_secs", default=10.0) or 10.0,
                    tracking_timeout_secs=get_float(item, "tracking_timeout_secs", default=120.0) or 120.0,
                    cancel_wait_secs=get_float(item, "cancel_wait_secs", default=2.0) or 2.0,
                )
            )
    pause_between = get_float(params_cfg, "pause_between_tests_secs", default=2.0) or 2.0
    test_mode_cfg = get_bool(params_cfg, "test_mode", default=False)
    params = ConnectorTestParams(tasks=tasks, pause_between_tests_secs=pause_between, test_mode=bool(test_mode_cfg))
    if not connectors:
        raise RuntimeError("connector_test requires at least one connector; verify resolve_connectors configuration")
    return ConnectorTestStrategy(connectors=connectors, params=params)


def build_connector_diagnostics_strategy(cfg: Dict[str, Any], connectors: Dict[str, Any], general: Dict[str, Any]) -> Any:
    from mm_bot.strategy.connector_diagnostics import ConnectorDiagnostics, DiagnosticParams, DiagnosticTask

    # The cfg comes from get_dict(strategy_section, "connector_diagnostics")
    # So it should contain the strategy parameters directly
    params_cfg = cfg

    tasks_cfg = params_cfg.get("tasks", [])
    tasks: List[DiagnosticTask] = []
    default_symbol = get_str(general, "symbol", env="XTB_SYMBOL", default="BTC")

    if isinstance(tasks_cfg, list):
        for item in tasks_cfg:
            if not isinstance(item, dict):
                continue
            venue = get_str(item, "venue")
            symbol = get_str(item, "symbol", default=default_symbol)
            if not venue or not symbol:
                continue
            tasks.append(
                DiagnosticTask(
                    venue=str(venue),
                    symbol=str(symbol),
                    mode=get_str(item, "mode", default="tracking_limit") or "tracking_limit",
                    side=get_str(item, "side", default="buy") or "buy",
                    min_multiplier=get_float(item, "min_multiplier", default=1.0) or 1.0,
                    price_offset_ticks=get_int(item, "price_offset_ticks", default=0) or 0,
                    tracking_interval_secs=get_float(item, "tracking_interval_secs", default=10.0) or 10.0,
                    tracking_timeout_secs=get_float(item, "tracking_timeout_secs", default=120.0) or 120.0,
                    cancel_wait_secs=get_float(item, "cancel_wait_secs", default=2.0) or 2.0,
                    reduce_only_probe=get_bool(item, "reduce_only_probe", default=False),
                )
            )

    pause_between = get_float(params_cfg, "pause_between_tests_secs", default=2.0) or 2.0
    test_mode_cfg = get_bool(params_cfg, "test_mode", default=False)
    params = DiagnosticParams(
        tasks=tasks,
        pause_between_tests_secs=pause_between,
        test_mode=bool(test_mode_cfg)
    )

    if not connectors:
        raise RuntimeError(f"connector_diagnostics requires at least one connector, but got: {list(connectors.keys())}")
    return ConnectorDiagnostics(connectors=connectors, params=params)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _resolve_connector_test_connectors(strategy_cfg: Dict[str, Any], general: Dict[str, Any]) -> List[str]:
    params_cfg = get_dict(strategy_cfg, "connector_test") or strategy_cfg
    if not isinstance(params_cfg, dict):
        return []
    tasks_cfg = params_cfg.get("tasks", [])
    venues: List[str] = []
    if isinstance(tasks_cfg, list):
        for item in tasks_cfg:
            if not isinstance(item, dict):
                continue
            venue = get_str(item, "venue")
            if venue:
                venues.append(str(venue).lower())
    return list(dict.fromkeys(venues))


def _resolve_connector_diagnostics_connectors(strategy_cfg: Dict[str, Any], general: Dict[str, Any]) -> List[str]:
    # The strategy_cfg comes from get_dict(strategy_section, "connector_diagnostics")
    # So it should contain the strategy parameters directly
    if not isinstance(strategy_cfg, dict):
        return []

    tasks_cfg = strategy_cfg.get("tasks", [])
    venues: List[str] = []

    if isinstance(tasks_cfg, list):
        for item in tasks_cfg:
            if not isinstance(item, dict):
                continue
            venue = get_str(item, "venue")
            if venue:
                venues.append(str(venue).lower())

    return list(dict.fromkeys(venues))


STRATEGY_BUILDERS: Dict[str, Dict[str, Any]] = {
    "simple_hedge": {
        "factory": build_simple_hedge_strategy,
        "requires": ["backpack", "lighter"],
        "description": "Simple Backpack/Lighter hedging loop",
    },
    "liquidation_hedge": {
        "factory": build_liquidation_hedge_strategy,
        "requires": ["backpack", "lighter1", "lighter2"],
        "description": "Backpack-Lighter liquidation hedge cycle",
    },
    "connector_test": {
        "factory": build_connector_test_strategy,
        "requires": [],
        "resolve_connectors": _resolve_connector_test_connectors,
        "description": "Generic connector operation test",
    },
    "connector_diagnostics": {
        "factory": build_connector_diagnostics_strategy,
        "requires": [],
        "resolve_connectors": _resolve_connector_diagnostics_connectors,
        "description": "Enhanced connector diagnostics with observability",
    },
}


__all__ = ["CONNECTOR_BUILDERS", "STRATEGY_BUILDERS"]
