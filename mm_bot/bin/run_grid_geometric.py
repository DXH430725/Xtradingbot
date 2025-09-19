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
from mm_bot.connector.grvt.grvt_exchange import GrvtConnector, GrvtConfig
from mm_bot.strategy.geometric_grid import GeometricGridParams, GeometricGridStrategy
from mm_bot.conf.config import load_config


async def main():
    # Default to geometric grid config unless XTB_CONFIG overrides
    default_cfg = os.path.join(ROOT, "mm_bot", "conf", "grid_geometric.yaml")
    cfg_path = os.getenv("XTB_CONFIG") or default_cfg
    cfg = load_config(cfg_path)
    log_cfg = ((cfg.get("general") or {}).get("log_config")) or os.path.join(ROOT, "mm_bot", "conf", "logging.yaml")
    setup_logging(log_cfg)
    log = logging.getLogger("mm_bot.bin.run_grid_geometric")

    general = cfg.get("general") or {}
    connector_cfg = cfg.get("connector") or {}
    strat_cfg = ((cfg.get("strategy") or {}).get("grid_geometric")) or {}

    tick_size = float(general.get("tick_size", os.getenv("XTB_TICK_SIZE", 2.0)))
    # symbol priority: strategy.grid_geometric.symbol -> general.symbol -> env XTB_SYMBOL -> None
    symbol = strat_cfg.get("symbol") or general.get("symbol") or os.getenv("XTB_SYMBOL")

    core = TradingCore(tick_size=tick_size, debug=os.getenv("XTB_DEBUG") in {"1","true","yes","on"})

    connector_name = str(general.get("connector", os.getenv("XTB_CONNECTOR", "lighter"))).lower()
    if connector_name == "grvt":
        conn = GrvtConnector(config=GrvtConfig(), debug=False)
        conn.start()
        core.add_connector("grvt", conn)
    else:
        # lighter defaults from env
        rpm = int(connector_cfg.get("rpm", os.getenv("XTB_LIGHTER_RPM", 4000)))
        base_url = connector_cfg.get("base_url", os.getenv("XTB_LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"))
        keys_file = connector_cfg.get("keys_file", os.getenv("XTB_LIGHTER_KEYS_FILE", os.path.join(os.getcwd(), "Lighter_key.txt")))
        account_index = connector_cfg.get("account_index", os.getenv("XTB_LIGHTER_ACCOUNT_INDEX"))
        if account_index is not None:
            try:
                account_index = int(account_index)
            except Exception:
                account_index = None
        conn = LighterConnector(config=LighterConfig(base_url=base_url, keys_file=keys_file, account_index=account_index, rpm=rpm), debug=False)
        conn.start()
        core.add_connector("lighter", conn)

    params = GeometricGridParams(
        range_low=float(strat_cfg.get("range_low", 100.0)),
        range_high=float(strat_cfg.get("range_high", 200.0)),
        levels=int(strat_cfg.get("levels", 20)),
        orders_per_side=int(strat_cfg.get("orders_per_side", 5)),
        mode=str(strat_cfg.get("mode", "neutral")).lower(),
        quote_allocation=(float(strat_cfg.get("quote_allocation")) if strat_cfg.get("quote_allocation") is not None else None),
        post_only=bool(int(strat_cfg.get("post_only", 1))),
        recenter_on_move=bool(int(strat_cfg.get("recenter_on_move", 0))),
        requote_ticks=int(strat_cfg.get("requote_ticks", 1)),
        telemetry_enabled=bool(int(strat_cfg.get("telemetry_enabled", 0))),
        telemetry_url=strat_cfg.get("telemetry_url"),
        telemetry_interval_secs=int(strat_cfg.get("telemetry_interval_secs", 30)),
    )

    strat = GeometricGridStrategy(connector=conn, symbol=symbol, params=params)
    core.set_strategy(strat)

    await core.start()
    log.info("Geometric Grid bot started")
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        log.info("Geometric Grid bot interrupted; stopping…")
    finally:
        await core.stop(cancel_orders=True)
        await core.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
