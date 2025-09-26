"""Component builders used by the unified runner."""

from __future__ import annotations

import os
from typing import Any, Dict, List

from .config_utils import get_bool, get_dict, get_float, get_int, get_str


# ---------------------------------------------------------------------------
# Connector builders
# ---------------------------------------------------------------------------

def build_backpack_connector(cfg: Dict[str, Any], general: Dict[str, Any], debug: bool) -> Any:
    from mm_bot.connector.backpack.backpack_exchange import BackpackConfig, BackpackConnector

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
    from mm_bot.connector.lighter.lighter_exchange import LighterConfig, LighterConnector

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
    "grvt": {
        "factory": build_grvt_connector,
        "description": "GRVT exchange connector",
    },
}


# ---------------------------------------------------------------------------
# Strategy builders
# ---------------------------------------------------------------------------

def _symbol_from_general(strategy_cfg: Dict[str, Any], general: Dict[str, Any]) -> Any:
    symbol = get_str(strategy_cfg, "symbol")
    if symbol:
        return symbol
    symbol = get_str(general, "symbol", env="XTB_SYMBOL")
    return symbol


def build_trend_ladder_strategy(cfg: Dict[str, Any], connectors: Dict[str, Any], general: Dict[str, Any]) -> Any:
    from mm_bot.strategy.trend_ladder import TrendAdaptiveLadderStrategy, TrendLadderParams

    base = TrendLadderParams()
    _partial_tp = get_bool(cfg, "partial_tp_enabled", env="XTB_TL_PARTIAL_TP", default=base.partial_tp_enabled)
    _pace_ignore = get_bool(
        cfg,
        "pace_ignore_imbalance_tp",
        env="XTB_TL_PACE_IGNORE_IMBTP",
        default=base.pace_ignore_imbalance_tp,
    )
    _telemetry_enabled = get_bool(
        cfg,
        "telemetry_enabled",
        env="XTB_TL_TELEM_ENABLED",
        default=base.telemetry_enabled,
    )

    params = TrendLadderParams(
        quantity_base=get_float(cfg, "quantity_base", env="XTB_TL_QTY", default=base.quantity_base) or base.quantity_base,
        take_profit_abs=get_float(cfg, "take_profit_abs", env="XTB_TL_TP", default=base.take_profit_abs) or base.take_profit_abs,
        max_orders=get_int(cfg, "max_orders", env="XTB_TL_MAX_ORDERS", default=base.max_orders) or base.max_orders,
        base_wait=get_float(cfg, "base_wait", env="XTB_TL_BASE_WAIT", default=base.base_wait) or base.base_wait,
        min_wait=get_float(cfg, "min_wait", env="XTB_TL_MIN_WAIT", default=base.min_wait) or base.min_wait,
        max_wait=get_float(cfg, "max_wait", env="XTB_TL_MAX_WAIT", default=base.max_wait) or base.max_wait,
        fixed_direction=(get_str(cfg, "fixed_direction", env="XTB_TL_DIRECTION", default=base.fixed_direction) or base.fixed_direction).lower(),
        ema_len=get_int(cfg, "ema_len", env="XTB_TL_EMA", default=base.ema_len) or base.ema_len,
        atr_len=get_int(cfg, "atr_len", env="XTB_TL_ATR", default=base.atr_len) or base.atr_len,
        slope_up=get_float(cfg, "slope_up", env="XTB_TL_SLOPE_UP", default=base.slope_up) or base.slope_up,
        slope_down=get_float(cfg, "slope_down", env="XTB_TL_SLOPE_DOWN", default=base.slope_down) or base.slope_down,
        warmup_minutes=get_int(cfg, "warmup_minutes", env="XTB_TL_WARMUP", default=base.warmup_minutes) or base.warmup_minutes,
        flush_ticks=get_int(cfg, "flush_ticks", env="XTB_TL_FLUSH_TICKS", default=base.flush_ticks) or base.flush_ticks,
        imb_threshold_mult=get_int(cfg, "imb_threshold_mult", env="XTB_TL_IMB_THRESHOLD_MULT", default=base.imb_threshold_mult)
        or base.imb_threshold_mult,
        imb_max_corr_in_10m=get_int(cfg, "imb_max_corr_in_10m", env="XTB_TL_IMB_MAX_CORR_10M", default=base.imb_max_corr_in_10m)
        or base.imb_max_corr_in_10m,
        partial_tp_enabled=_partial_tp if _partial_tp is not None else base.partial_tp_enabled,
        pace_ignore_imbalance_tp=_pace_ignore if _pace_ignore is not None else base.pace_ignore_imbalance_tp,
        requote_ticks=get_int(cfg, "requote_ticks", env="XTB_TL_REQUOTE_TICKS", default=base.requote_ticks) or base.requote_ticks,
        max_requotes_per_tick=get_int(
            cfg,
            "max_requotes_per_tick",
            env="XTB_TL_MAX_REQUOTES",
            default=base.max_requotes_per_tick,
        )
        or base.max_requotes_per_tick,
        requote_abs=get_float(cfg, "requote_abs", env="XTB_TL_REQUOTE_ABS", default=base.requote_abs) or base.requote_abs,
        requote_wait_confirm_secs=get_float(
            cfg,
            "requote_wait_confirm_secs",
            env="XTB_TL_REQUOTE_WAIT_SECS",
            default=base.requote_wait_confirm_secs,
        )
        or base.requote_wait_confirm_secs,
        requote_skip_backoff_secs=get_float(
            cfg,
            "requote_skip_backoff_secs",
            env="XTB_TL_REQUOTE_BACKOFF",
            default=base.requote_skip_backoff_secs,
        )
        or base.requote_skip_backoff_secs,
        requote_skip_backoff_max_secs=get_float(
            cfg,
            "requote_skip_backoff_max_secs",
            env="XTB_TL_REQUOTE_BACKOFF_MAX",
            default=base.requote_skip_backoff_max_secs,
        )
        or base.requote_skip_backoff_max_secs,
        requote_mode=get_str(cfg, "requote_mode", default=base.requote_mode) or base.requote_mode,
        max_requote_dupes=get_int(cfg, "max_requote_dupes", default=base.max_requote_dupes) or base.max_requote_dupes,
        telemetry_enabled=_telemetry_enabled if _telemetry_enabled is not None else base.telemetry_enabled,
        telemetry_url=get_str(cfg, "telemetry_url", env="XTB_TL_TELEM_URL", default=base.telemetry_url) or base.telemetry_url,
        telemetry_interval_secs=get_int(
            cfg,
            "telemetry_interval_secs",
            env="XTB_TL_TELEM_INTERVAL",
            default=base.telemetry_interval_secs,
        )
        or base.telemetry_interval_secs,
    )

    symbol = _symbol_from_general(cfg, general)
    connector = connectors["backpack"]
    return TrendAdaptiveLadderStrategy(connector=connector, symbol=symbol, params=params)


def build_backpack_perp_mm_strategy(cfg: Dict[str, Any], connectors: Dict[str, Any], general: Dict[str, Any]) -> Any:
    from mm_bot.strategy.backpack_perp_market_maker import BackpackPerpMarketMakerStrategy, PerpMarketMakerParams

    base = PerpMarketMakerParams(symbol=_symbol_from_general(cfg, general) or "BTC_USDC_PERP")
    _cancel_before_place = get_bool(cfg, "cancel_before_place", default=base.cancel_before_place)
    _post_only = get_bool(cfg, "post_only", default=base.post_only)

    params = PerpMarketMakerParams(
        symbol=_symbol_from_general(cfg, general) or base.symbol,
        base_spread_pct=get_float(cfg, "base_spread_pct", default=base.base_spread_pct) or base.base_spread_pct,
        order_quantity=get_float(cfg, "order_quantity", default=base.order_quantity) if cfg.get("order_quantity") is not None else base.order_quantity,
        max_orders=get_int(cfg, "max_orders", default=base.max_orders) or base.max_orders,
        target_position=get_float(cfg, "target_position", default=base.target_position) or base.target_position,
        max_position=get_float(cfg, "max_position", default=base.max_position) or base.max_position,
        position_threshold=get_float(cfg, "position_threshold", default=base.position_threshold) or base.position_threshold,
        inventory_skew=get_float(cfg, "inventory_skew", default=base.inventory_skew) or base.inventory_skew,
        tick_interval_secs=get_float(cfg, "tick_interval_secs", default=base.tick_interval_secs) or base.tick_interval_secs,
        cancel_before_place=_cancel_before_place if _cancel_before_place is not None else base.cancel_before_place,
        post_only=_post_only if _post_only is not None else base.post_only,
    )
    connector = connectors["backpack"]
    return BackpackPerpMarketMakerStrategy(connector=connector, params=params)


def build_cross_arb_strategy(cfg: Dict[str, Any], connectors: Dict[str, Any], general: Dict[str, Any]) -> Any:
    from mm_bot.strategy.cross_market_arbitrage import CrossArbParams, CrossMarketArbitrageStrategy

    base = CrossArbParams()
    _allow_market = get_bool(
        cfg,
        "allow_market_fallback",
        env="XTB_ARB_TRACK_MARKET",
        default=base.allow_market_fallback,
    )
    _debug_once = get_bool(cfg, "debug_run_once", env="XTB_ARB_DEBUG_ONCE", default=base.debug_run_once)
    _lighter_market = get_bool(
        cfg,
        "lighter_market_execution",
        env="XTB_ARB_LIGHTER_MARKET",
        default=base.lighter_market_execution,
    )

    params = CrossArbParams(
        entry_threshold_pct=get_float(cfg, "entry_threshold_pct", env="XTB_ARB_ENTRY_PCT", default=base.entry_threshold_pct)
        or base.entry_threshold_pct,
        max_concurrent_positions=get_int(
            cfg,
            "max_concurrent_positions",
            env="XTB_ARB_MAX_POS",
            default=base.max_concurrent_positions,
        )
        or base.max_concurrent_positions,
        cooldown_secs=get_float(cfg, "cooldown_secs", env="XTB_ARB_COOLDOWN", default=base.cooldown_secs) or base.cooldown_secs,
        tp_ratio=get_float(cfg, "tp_ratio", env="XTB_ARB_TP_RATIO", default=base.tp_ratio) or base.tp_ratio,
        sl_ratio=get_float(cfg, "sl_ratio", env="XTB_ARB_SL_RATIO", default=base.sl_ratio) or base.sl_ratio,
        max_hold_secs=get_float(cfg, "max_hold_secs", env="XTB_ARB_MAX_HOLD", default=base.max_hold_secs)
        or base.max_hold_secs,
        maintenance_local_hour=get_int(
            cfg,
            "maintenance_local_hour",
            env="XTB_ARB_MAINT_H",
            default=base.maintenance_local_hour,
        ),
        pre_maint_close_minutes=get_int(
            cfg,
            "pre_maint_close_minutes",
            env="XTB_ARB_PRE_MAINT_M",
            default=base.pre_maint_close_minutes,
        )
        or base.pre_maint_close_minutes,
        poll_interval_ms=get_int(cfg, "poll_interval_ms", env="XTB_ARB_POLL_MS", default=base.poll_interval_ms)
        or base.poll_interval_ms,
        latency_circuit_ms=get_float(
            cfg,
            "latency_circuit_ms",
            env="XTB_ARB_LAT_MS",
            default=base.latency_circuit_ms,
        )
        or base.latency_circuit_ms,
        delta_tolerance=get_float(cfg, "delta_tolerance", env="XTB_ARB_DELTA_TOL", default=base.delta_tolerance)
        or base.delta_tolerance,
        max_delta_failures=get_int(
            cfg,
            "max_delta_failures",
            env="XTB_ARB_DELTA_MAX_FAIL",
            default=base.max_delta_failures,
        )
        or base.max_delta_failures,
        tracking_wait_secs=get_float(
            cfg,
            "tracking_wait_secs",
            env="XTB_ARB_TRACK_WAIT",
            default=base.tracking_wait_secs,
        )
        or base.tracking_wait_secs,
        tracking_max_retries=get_int(
            cfg,
            "tracking_max_retries",
            env="XTB_ARB_TRACK_RETRIES",
            default=base.tracking_max_retries,
        )
        or base.tracking_max_retries,
        allow_market_fallback=_allow_market if _allow_market is not None else base.allow_market_fallback,
        debug_run_once=_debug_once if _debug_once is not None else base.debug_run_once,
        lighter_market_execution=_lighter_market if _lighter_market is not None else base.lighter_market_execution,
    )

    symbol_filters = cfg.get("symbol_filters") or []
    if not isinstance(symbol_filters, list):
        symbol_filters = [str(symbol_filters)]

    return CrossMarketArbitrageStrategy(
        lighter_connector=connectors["lighter"],
        backpack_connector=connectors["backpack"],
        params=params,
        symbol_filters=symbol_filters,
    )


def build_geometric_grid_strategy(cfg: Dict[str, Any], connectors: Dict[str, Any], general: Dict[str, Any]) -> Any:
    from mm_bot.strategy.geometric_grid import GeometricGridParams, GeometricGridStrategy

    base = GeometricGridParams()
    _post_only = get_bool(cfg, "post_only", default=base.post_only)
    _recenter_on_move = get_bool(cfg, "recenter_on_move", default=base.recenter_on_move)
    _telemetry_enabled = get_bool(cfg, "telemetry_enabled", default=base.telemetry_enabled)

    params = GeometricGridParams(
        range_low=get_float(cfg, "range_low", default=base.range_low) or base.range_low,
        range_high=get_float(cfg, "range_high", default=base.range_high) or base.range_high,
        levels=get_int(cfg, "levels", default=base.levels) or base.levels,
        orders_per_side=get_int(cfg, "orders_per_side", default=base.orders_per_side) or base.orders_per_side,
        mode=get_str(cfg, "mode", default=base.mode) or base.mode,
        quote_allocation=get_float(cfg, "quote_allocation", default=base.quote_allocation),
        post_only=_post_only if _post_only is not None else base.post_only,
        recenter_on_move=_recenter_on_move if _recenter_on_move is not None else base.recenter_on_move,
        requote_ticks=get_int(cfg, "requote_ticks", default=base.requote_ticks) or base.requote_ticks,
        telemetry_enabled=_telemetry_enabled if _telemetry_enabled is not None else base.telemetry_enabled,
        telemetry_url=get_str(cfg, "telemetry_url", default=base.telemetry_url) or base.telemetry_url,
        telemetry_interval_secs=get_int(cfg, "telemetry_interval_secs", default=base.telemetry_interval_secs)
        or base.telemetry_interval_secs,
    )

    symbol = _symbol_from_general(cfg, general)
    connector = next(iter(connectors.values())) if connectors else None
    return GeometricGridStrategy(connector=connector, symbol=symbol, params=params)


def build_as_model_strategy(cfg: Dict[str, Any], connectors: Dict[str, Any], general: Dict[str, Any]) -> Any:
    from mm_bot.strategy.as_model import ASParams, AvellanedaStoikovStrategy

    base = ASParams()
    _telemetry_enabled = get_bool(cfg, "telemetry_enabled", default=base.telemetry_enabled)

    params = ASParams(
        gamma=get_float(cfg, "gamma", default=base.gamma) or base.gamma,
        k=get_float(cfg, "k", default=base.k) or base.k,
        tau=get_float(cfg, "tau", default=base.tau) or base.tau,
        beta=get_float(cfg, "beta", default=base.beta) or base.beta,
        window_secs=get_float(cfg, "window_secs", default=base.window_secs) or base.window_secs,
        min_spread_abs=get_float(cfg, "min_spread_abs", default=base.min_spread_abs) or base.min_spread_abs,
        min_spread_bps=get_float(cfg, "min_spread_bps", default=base.min_spread_bps),
        requote_ticks=get_int(cfg, "requote_ticks", default=base.requote_ticks) or base.requote_ticks,
        sigma_ewma_alpha=get_float(cfg, "sigma_ewma_alpha", default=base.sigma_ewma_alpha) or base.sigma_ewma_alpha,
        size_base=get_float(cfg, "size_base", default=base.size_base),
        max_position_base=get_float(cfg, "max_position_base", default=base.max_position_base),
        recover_ratio=get_float(cfg, "recover_ratio", default=base.recover_ratio) or base.recover_ratio,
        gamma_max=get_float(cfg, "gamma_max", default=base.gamma_max),
        beta_max=get_float(cfg, "beta_max", default=base.beta_max),
        telemetry_enabled=_telemetry_enabled if _telemetry_enabled is not None else base.telemetry_enabled,
        telemetry_url=get_str(cfg, "telemetry_url", default=base.telemetry_url) or base.telemetry_url,
        telemetry_interval_secs=get_int(cfg, "telemetry_interval_secs", default=base.telemetry_interval_secs)
        or base.telemetry_interval_secs,
    )

    symbol = _symbol_from_general(cfg, general)
    connector = next(iter(connectors.values()))
    return AvellanedaStoikovStrategy(connector=connector, symbol=symbol, params=params)


def build_liquidation_hedge_strategy(cfg: Dict[str, Any], connectors: Dict[str, Any], general: Dict[str, Any]) -> Any:
    from mm_bot.strategy.liquidation_hedge import LiquidationHedgeParams, LiquidationHedgeStrategy

    params_cfg = get_dict(cfg, "liquidation_hedge") or cfg
    leverage = get_float(params_cfg, "leverage", default=50.0) or 50.0
    price_offset = int(get_float(params_cfg, "price_offset_ticks", default=1) or 1)
    timeout_secs = get_float(params_cfg, "timeout_secs", default=24 * 3600.0) or (24 * 3600.0)
    poll_secs = get_float(params_cfg, "poll_interval_secs", default=5.0) or 5.0
    reduce_buffer = int(get_float(params_cfg, "reduce_only_buffer_ticks", default=5) or 5)
    reverse_on_timeout = get_bool(params_cfg, "reverse_on_timeout", default=True)
    max_cycles = int(get_float(params_cfg, "max_cycles", default=1) or 1)
    tracking_post_only = get_bool(params_cfg, "tracking_post_only", default=True)
    params = LiquidationHedgeParams(
        backpack_symbol=get_str(params_cfg, "backpack_symbol", default="ETH_USDC_PERP") or "ETH_USDC_PERP",
        lighter_symbol=get_str(params_cfg, "lighter_symbol", default="ETH") or "ETH",
        leverage=leverage,
        direction=get_str(params_cfg, "direction", default="long_backpack") or "long_backpack",
        price_offset_ticks=price_offset,
        tracking_cancel_wait_secs=get_float(params_cfg, "tracking_cancel_wait_secs", default=2.0) or 2.0,
        tracking_post_only=tracking_post_only,
        timeout_secs=timeout_secs,
        poll_interval_secs=poll_secs,
        reduce_only_buffer_ticks=reduce_buffer,
        reverse_on_timeout=reverse_on_timeout,
        max_cycles=max_cycles,
        min_collateral=get_float(params_cfg, "min_collateral", default=0.0) or 0.0,
    )
    backpack = connectors.get("backpack")
    lighter = connectors.get("lighter")
    return LiquidationHedgeStrategy(backpack_connector=backpack, lighter_connector=lighter, params=params)


def build_hedge_ladder_strategy(cfg: Dict[str, Any], connectors: Dict[str, Any], general: Dict[str, Any]) -> Any:
    from mm_bot.strategy.hedge_ladder import HedgeLadderParams, HedgeLadderStrategy

    base = HedgeLadderParams()
    _lighter_reduce_only = get_bool(
        cfg,
        "lighter_reduce_only_close",
        env="XTB_HL_HEDGE_RO",
        default=base.lighter_reduce_only_close,
    )
    _debug_flag = get_bool(cfg, "debug", env="XTB_DEBUG", default=base.debug)

    params = HedgeLadderParams(
        backpack_symbol=get_str(cfg, "backpack_symbol", env="XTB_BP_SYMBOL", default=base.backpack_symbol) or base.backpack_symbol,
        lighter_symbol=get_str(cfg, "lighter_symbol", env="XTB_LG_SYMBOL", default=base.lighter_symbol) or base.lighter_symbol,
        quantity_base=get_float(cfg, "quantity_base", env="XTB_HL_QTY", default=base.quantity_base) or base.quantity_base,
        take_profit_abs=get_float(cfg, "take_profit_abs", env="XTB_HL_TP", default=base.take_profit_abs) or base.take_profit_abs,
        max_concurrent_positions=get_int(
            cfg,
            "max_concurrent_positions",
            env="XTB_HL_MAX_POS",
            default=base.max_concurrent_positions,
        )
        or base.max_concurrent_positions,
        entry_interval_secs=get_float(
            cfg,
            "entry_interval_secs",
            env="XTB_HL_ENTRY_INTERVAL",
            default=base.entry_interval_secs,
        )
        or base.entry_interval_secs,
        poll_interval_ms=get_int(cfg, "poll_interval_ms", env="XTB_HL_POLL_MS", default=base.poll_interval_ms)
        or base.poll_interval_ms,
        hedge_trigger_ratio=get_float(
            cfg,
            "hedge_trigger_ratio",
            env="XTB_HL_HEDGE_RATIO",
            default=base.hedge_trigger_ratio,
        )
        or base.hedge_trigger_ratio,
        hedge_rate_limit_per_sec=get_int(
            cfg,
            "hedge_rate_limit_per_sec",
            env="XTB_HL_HEDGE_LIMIT",
            default=base.hedge_rate_limit_per_sec,
        )
        or base.hedge_rate_limit_per_sec,
        hedge_retry=get_int(cfg, "hedge_retry", env="XTB_HL_HEDGE_RETRY", default=base.hedge_retry) or base.hedge_retry,
        hedge_retry_delay=get_float(
            cfg,
            "hedge_retry_delay",
            env="XTB_HL_HEDGE_DELAY",
            default=base.hedge_retry_delay,
        )
        or base.hedge_retry_delay,
        lighter_reduce_only_close=_lighter_reduce_only if _lighter_reduce_only is not None else base.lighter_reduce_only_close,
        debug=_debug_flag if _debug_flag is not None else base.debug,
    )

    return HedgeLadderStrategy(
        backpack_connector=connectors["backpack"],
        lighter_connector=connectors["lighter"],
        params=params,
    )


def build_smoke_test_strategy(cfg: Dict[str, Any], connectors: Dict[str, Any], general: Dict[str, Any]) -> Any:
    from mm_bot.strategy.smoke_test import ConnectorSmokeTestStrategy, SmokeTestParams, ConnectorTestConfig

    connector_cfg = get_dict(cfg, "connectors")
    default_symbol = get_str(general, "symbol", env="XTB_SYMBOL", default="BTC_USDC_PERP")
    mapped: Dict[str, ConnectorTestConfig] = {}
    for name, data in connector_cfg.items():
        if not isinstance(data, dict):
            continue
        tracking_timeout = get_float(data, "tracking_timeout_secs", default=120.0)
        if tracking_timeout is None or tracking_timeout <= 0:
            tracking_timeout = 120.0
        tracking_interval = get_float(data, "tracking_interval_secs", default=10.0)
        if tracking_interval is None or tracking_interval <= 0:
            tracking_interval = 10.0
        settle_timeout = get_float(data, "settle_timeout_secs", default=10.0)
        if settle_timeout is None:
            settle_timeout = 10.0
        market_timeout = get_float(data, "market_timeout_secs", default=30.0)
        if market_timeout is None or market_timeout <= 0:
            market_timeout = 30.0
        price_offset = get_int(data, "price_offset_ticks", default=0)
        if price_offset is None:
            price_offset = 0
        cancel_wait = get_float(data, "cancel_wait_secs", default=2.0)
        if cancel_wait is None or cancel_wait <= 0:
            cancel_wait = 2.0
        mapped[name] = ConnectorTestConfig(
            symbol=str(data.get("symbol", default_symbol)),
            side=str(data.get("side", "buy")),
            tracking_timeout_secs=tracking_timeout,
            tracking_interval_secs=tracking_interval,
            settle_timeout_secs=settle_timeout,
            market_timeout_secs=market_timeout,
            price_offset_ticks=price_offset,
            cancel_wait_secs=cancel_wait,
        )
    pause_between = get_float(cfg, "pause_between_tests_secs", default=2.0)
    if pause_between is None:
        pause_between = 2.0
    params = SmokeTestParams(
        connectors=mapped,
        pause_between_tests_secs=pause_between,
    )
    active_connectors = {name: connectors[name] for name in mapped.keys() if name in connectors}
    return ConnectorSmokeTestStrategy(connectors=active_connectors, params=params)


async def prepare_smoke_test(connectors: Dict[str, Any], strategy_cfg: Dict[str, Any], general: Dict[str, Any]) -> None:
    connector_cfg = get_dict(strategy_cfg, "connectors")
    default_symbol = get_str(general, "symbol", env="XTB_SYMBOL", default="BTC_USDC_PERP")
    for name, connector in connectors.items():
        if not connector:
            continue
        start_ws = getattr(connector, "start_ws_state", None)
        if not callable(start_ws):
            continue
        cfg = connector_cfg.get(name, {}) if isinstance(connector_cfg, dict) else {}
        symbol = cfg.get("symbol") if isinstance(cfg, dict) else None
        targets: List[str] = []
        if symbol:
            targets = [str(symbol)]
        elif default_symbol:
            targets = [str(default_symbol)]
        try:
            await start_ws(targets)
        except TypeError:
            await start_ws()


async def prepare_cross_arb(connectors: Dict[str, Any], strategy_cfg: Dict[str, Any], general: Dict[str, Any]) -> None:
    lighter = connectors.get("lighter")
    backpack = connectors.get("backpack")
    if lighter and hasattr(lighter, "start_ws_state"):
        await lighter.start_ws_state()
    if backpack and hasattr(backpack, "start_ws_state"):
        await backpack.start_ws_state([])


def _resolve_single_connector(strategy_cfg: Dict[str, Any], general: Dict[str, Any], *, default: str, choices: List[str]) -> List[str]:
    name = get_str(general, "connector", env="XTB_CONNECTOR", default=default) or default
    name = name.lower()
    if name not in choices:
        name = default
    return [name]


def _resolve_smoke_connectors(strategy_cfg: Dict[str, Any], general: Dict[str, Any]) -> List[str]:
    connectors_cfg = get_dict(strategy_cfg, "connectors")
    return [str(k).lower() for k in connectors_cfg.keys()]


STRATEGY_BUILDERS: Dict[str, Dict[str, Any]] = {
    "trend_ladder": {
        "factory": build_trend_ladder_strategy,
        "requires": ["backpack"],
        "description": "Backpack trend-following ladder market maker",
    },
    "backpack_perp_mm": {
        "factory": build_backpack_perp_mm_strategy,
        "requires": ["backpack"],
        "description": "Backpack perp passive market maker",
    },
    "cross_arb": {
        "factory": build_cross_arb_strategy,
        "requires": ["lighter", "backpack"],
        "prepare": prepare_cross_arb,
        "description": "Cross-exchange arbitrage between Backpack and Lighter",
    },
    "grid_geometric": {
        "factory": build_geometric_grid_strategy,
        "requires": [],
        "resolve_connectors": lambda cfg, general: _resolve_single_connector(cfg, general, default="lighter", choices=["lighter", "grvt"]),
        "description": "Geometric price ladder grid strategy",
    },
    "as_model": {
        "factory": build_as_model_strategy,
        "requires": [],
        "resolve_connectors": lambda cfg, general: _resolve_single_connector(cfg, general, default="lighter", choices=["lighter", "grvt"]),
        "description": "Avellaneda-Stoikov model maker",
    },
    "liquidation_hedge": {
        "factory": build_liquidation_hedge_strategy,
        "requires": ["backpack", "lighter"],
        "description": "Backpack-Lighter liquidation hedge cycle",
    },
    "hedge_ladder": {
        "factory": build_hedge_ladder_strategy,
        "requires": ["backpack", "lighter"],
        "description": "Backpack-Lighter hedge ladder strategy",
    },
    "smoke_test": {
        "factory": build_smoke_test_strategy,
        "requires": [],
        "resolve_connectors": _resolve_smoke_connectors,
        "prepare": prepare_smoke_test,
        "description": "Connector smoke test",
    },
}
