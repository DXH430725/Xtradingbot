import asyncio
import logging
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mm_bot.conf.config import load_config
from mm_bot.core.trading_core import TradingCore
from mm_bot.logger.logger import setup_logging
from mm_bot.connector.backpack.backpack_exchange import BackpackConnector, BackpackConfig
from mm_bot.connector.lighter.lighter_exchange import LighterConnector, LighterConfig
from mm_bot.strategy.hedge_ladder import HedgeLadderStrategy, HedgeLadderParams


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"true", "1", "y", "yes", "on"}


def _as_float(value, default=0.0):
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _as_int(value, default=0):
    try:
        if value is None:
            return int(default)
        return int(value)
    except Exception:
        return int(default)


async def main():
    cfg_env = os.getenv("XTB_CONFIG")
    default_cfg = os.path.join(ROOT, "mm_bot", "conf", "hedge_ladder.yaml")
    cfg_path = cfg_env or default_cfg
    cfg = load_config(cfg_path)

    general = cfg.get("general", {})
    connector_cfg = cfg.get("connector", {})
    strat_cfg = cfg.get("strategy", {}).get("hedge_ladder", {})

    log_cfg = general.get("log_config") or os.path.join(ROOT, "mm_bot", "conf", "logging.yaml")
    setup_logging(log_cfg)
    log = logging.getLogger("mm_bot.bin.run_hedge_ladder")

    tick_size = _as_float(general.get("tick_size") or os.getenv("XTB_TICK_SIZE", 1.0), 1.0)

    # Backpack connector configuration
    bp_cfg = BackpackConfig(
        base_url=connector_cfg.get("backpack_base_url", os.getenv("XTB_BACKPACK_BASE_URL", "https://api.backpack.exchange")),
        ws_url=connector_cfg.get("backpack_ws_url", os.getenv("XTB_BACKPACK_WS_URL", "wss://ws.backpack.exchange")),
        keys_file=connector_cfg.get("backpack_keys_file", os.getenv("XTB_BACKPACK_KEYS_FILE", os.path.join(os.getcwd(), "Backpack_key.txt"))),
        rpm=_as_int(connector_cfg.get("backpack_rpm", os.getenv("XTB_BACKPACK_RPM", 300)), 300),
        window_ms=_as_int(connector_cfg.get("backpack_window_ms", os.getenv("XTB_BACKPACK_WINDOW_MS", 5000)), 5000),
    )

    # Lighter connector configuration
    lighter_cfg = LighterConfig(
        base_url=connector_cfg.get("lighter_base_url", os.getenv("XTB_LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")),
        keys_file=connector_cfg.get("lighter_keys_file", os.getenv("XTB_LIGHTER_KEYS_FILE", os.path.join(os.getcwd(), "Lighter_key.txt"))),
        account_index=_as_int(connector_cfg.get("lighter_account_index", os.getenv("XTB_LIGHTER_ACCOUNT_INDEX")) or -1, -1) if connector_cfg.get("lighter_account_index") or os.getenv("XTB_LIGHTER_ACCOUNT_INDEX") else None,
        rpm=_as_int(connector_cfg.get("lighter_rpm", os.getenv("XTB_LIGHTER_RPM", 4000)), 4000),
    )

    params = HedgeLadderParams(
        backpack_symbol=strat_cfg.get("backpack_symbol", os.getenv("XTB_BP_SYMBOL", "BTC_USDC_PERP")),
        lighter_symbol=strat_cfg.get("lighter_symbol", os.getenv("XTB_LG_SYMBOL", "BTC")),
        quantity_base=_as_float(strat_cfg.get("quantity_base", os.getenv("XTB_HL_QTY", 0.0002)), 0.0002),
        take_profit_abs=_as_float(strat_cfg.get("take_profit_abs", os.getenv("XTB_HL_TP", 5.0)), 5.0),
        max_concurrent_positions=_as_int(strat_cfg.get("max_concurrent_positions", os.getenv("XTB_HL_MAX_POS", 10)), 10),
        entry_interval_secs=_as_float(strat_cfg.get("entry_interval_secs", os.getenv("XTB_HL_ENTRY_INTERVAL", 60)), 60.0),
        poll_interval_ms=_as_int(strat_cfg.get("poll_interval_ms", os.getenv("XTB_HL_POLL_MS", 500)), 500),
        hedge_trigger_ratio=_as_float(strat_cfg.get("hedge_trigger_ratio", os.getenv("XTB_HL_HEDGE_RATIO", 0.99)), 0.99),
        hedge_rate_limit_per_sec=_as_int(strat_cfg.get("hedge_rate_limit_per_sec", os.getenv("XTB_HL_HEDGE_LIMIT", 10)), 10),
        hedge_retry=_as_int(strat_cfg.get("hedge_retry", os.getenv("XTB_HL_HEDGE_RETRY", 3)), 3),
        hedge_retry_delay=_as_float(strat_cfg.get("hedge_retry_delay", os.getenv("XTB_HL_HEDGE_DELAY", 0.2)), 0.2),
        lighter_reduce_only_close=_as_bool(strat_cfg.get("lighter_reduce_only_close", os.getenv("XTB_HL_HEDGE_RO", "1")), True),
        debug=_as_bool(strat_cfg.get("debug", os.getenv("XTB_DEBUG", "0")), False),
    )

    core = TradingCore(tick_size=tick_size, debug=_as_bool(os.getenv("XTB_DEBUG", "0"), False))

    backpack = BackpackConnector(config=bp_cfg)
    lighter = LighterConnector(config=lighter_cfg, debug=_as_bool(os.getenv("XTB_LIGHTER_DEBUG", "0"), False))

    backpack.start()
    lighter.start()

    core.add_connector("backpack", backpack)
    core.add_connector("lighter", lighter)

    strategy = HedgeLadderStrategy(
        backpack_connector=backpack,
        lighter_connector=lighter,
        params=params,
    )
    core.set_strategy(strategy)

    await core.start()
    log.info(
        "HedgeLadder started: bp=%s lg=%s qty=%.6f tp=%.2f hedge_ratio=%.4f",
        params.backpack_symbol,
        params.lighter_symbol,
        params.quantity_base,
        params.take_profit_abs,
        params.hedge_trigger_ratio,
    )

    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        log.info("HedgeLadder interrupted; stopping…")
    finally:
        await core.stop(cancel_orders=True)
        try:
            await strategy.shutdown()
        except Exception as exc:
            log.warning("strategy_shutdown_failed err=%s", exc)
        await core.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
