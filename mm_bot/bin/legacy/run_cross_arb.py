import asyncio
import logging
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mm_bot.logger.logger import setup_logging
from mm_bot.core.trading_core import TradingCore
from mm_bot.connector.lighter.lighter_exchange import LighterConnector, LighterConfig
from mm_bot.connector.backpack.backpack_exchange import BackpackConnector, BackpackConfig
from mm_bot.strategy.cross_market_arbitrage import CrossMarketArbitrageStrategy, CrossArbParams
from mm_bot.conf.config import load_config


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


async def main():
    cfg_env = os.getenv("XTB_CONFIG")
    default_cfg = os.path.join(ROOT, "mm_bot", "conf", "cross_arb.yaml")
    cfg_path = cfg_env or default_cfg
    cfg = load_config(cfg_path)
    log_cfg = ((cfg.get("general") or {}).get("log_config")) or os.path.join(ROOT, "mm_bot", "conf", "logging.yaml")
    setup_logging(log_cfg)
    log = logging.getLogger("mm_bot.bin.run_cross_arb")

    general = cfg.get("general") or {}
    connector_cfg = cfg.get("connector") or {}
    strat_cfg = ((cfg.get("strategy") or {}).get("cross_arb")) or {}

    tick_size = float(general.get("tick_size", os.getenv("XTB_TICK_SIZE", 0.5)))

    # Lighter
    rpm = int(connector_cfg.get("rpm", os.getenv("XTB_LIGHTER_RPM", 4000)))
    base_url = connector_cfg.get("base_url", os.getenv("XTB_LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"))
    keys_file = connector_cfg.get("keys_file", os.getenv("XTB_LIGHTER_KEYS_FILE", os.path.join(os.getcwd(), "Lighter_key.txt")))
    account_index = connector_cfg.get("account_index", os.getenv("XTB_LIGHTER_ACCOUNT_INDEX"))
    if account_index is not None:
        try:
            account_index = int(account_index)
        except Exception:
            account_index = None

    # Backpack
    bp_keys_file = (connector_cfg.get("backpack_keys_file") or os.getenv("XTB_BACKPACK_KEYS_FILE", os.path.join(os.getcwd(), "Backpack_key.txt")))
    bp_rpm = int((connector_cfg.get("backpack_rpm")) or os.getenv("XTB_BACKPACK_RPM", 300))

    params = CrossArbParams(
        entry_threshold_pct=float(strat_cfg.get("entry_threshold_pct", os.getenv("XTB_ARB_ENTRY_PCT", 0.2))),
        max_concurrent_positions=int(strat_cfg.get("max_concurrent_positions", os.getenv("XTB_ARB_MAX_POS", 3))),
        cooldown_secs=float(strat_cfg.get("cooldown_secs", os.getenv("XTB_ARB_COOLDOWN", 3.0))),
        tp_ratio=float(strat_cfg.get("tp_ratio", os.getenv("XTB_ARB_TP_RATIO", 1.0))),
        sl_ratio=float(strat_cfg.get("sl_ratio", os.getenv("XTB_ARB_SL_RATIO", 1.0))),
        max_hold_secs=float(strat_cfg.get("max_hold_secs", os.getenv("XTB_ARB_MAX_HOLD", 600.0))),
        maintenance_local_hour=int(strat_cfg.get("maintenance_local_hour", os.getenv("XTB_ARB_MAINT_H", 23))),
        pre_maint_close_minutes=int(strat_cfg.get("pre_maint_close_minutes", os.getenv("XTB_ARB_PRE_MAINT_M", 10))),
        poll_interval_ms=int(strat_cfg.get("poll_interval_ms", os.getenv("XTB_ARB_POLL_MS", 250))),
        latency_circuit_ms=float(strat_cfg.get("latency_circuit_ms", os.getenv("XTB_ARB_LAT_MS", 1500.0))),
        delta_tolerance=float(strat_cfg.get("delta_tolerance", os.getenv("XTB_ARB_DELTA_TOL", 0.01))),
        max_delta_failures=int(strat_cfg.get("max_delta_failures", os.getenv("XTB_ARB_DELTA_MAX_FAIL", 3))),
        tracking_wait_secs=float(strat_cfg.get("tracking_wait_secs", os.getenv("XTB_ARB_TRACK_WAIT", 10.0))),
        tracking_max_retries=int(strat_cfg.get("tracking_max_retries", os.getenv("XTB_ARB_TRACK_RETRIES", 3))),
        allow_market_fallback=_as_bool(strat_cfg.get("allow_market_fallback", os.getenv("XTB_ARB_TRACK_MARKET", "1")), True),
        debug_run_once=_as_bool(strat_cfg.get("debug_run_once", os.getenv("XTB_ARB_DEBUG_ONCE")), False),
        lighter_market_execution=_as_bool(strat_cfg.get("lighter_market_execution", os.getenv("XTB_ARB_LIGHTER_MARKET", "1")), True),
    )

    symbol_filters = strat_cfg.get("symbol_filters") or []

    core = TradingCore(tick_size=tick_size, debug=os.getenv("XTB_DEBUG") in {"1","true","yes","on"})
    lg = LighterConnector(config=LighterConfig(base_url=base_url, keys_file=keys_file, account_index=account_index, rpm=rpm), debug=False)
    bp = BackpackConnector(config=BackpackConfig(keys_file=bp_keys_file, rpm=bp_rpm))
    lg.start()
    bp.start()
    await lg.start_ws_state()
    await bp.start_ws_state([])
    core.add_connector("lighter", lg)
    core.add_connector("backpack", bp)

    strat = CrossMarketArbitrageStrategy(
        lighter_connector=lg,
        backpack_connector=bp,
        params=params,
        symbol_filters=symbol_filters,
    )
    core.set_strategy(strat)

    await core.start()

    log.info(
        f"Cross-Exchange Arb started: entry>={params.entry_threshold_pct:.3f}% max_pos={params.max_concurrent_positions} "
        f"tp_ratio={params.tp_ratio} sl_ratio={params.sl_ratio} poll={params.poll_interval_ms}ms"
    )
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        log.info("Cross-Exchange Arb interrupted; stopping…")
    finally:
        await core.stop(cancel_orders=True)
        await core.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
