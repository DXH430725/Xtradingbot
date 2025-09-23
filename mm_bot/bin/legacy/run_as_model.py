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
from mm_bot.strategy.as_model import AvellanedaStoikovStrategy, ASParams
from mm_bot.conf.config import load_config


async def main():
    # Default to AS model config unless XTB_CONFIG overrides
    default_cfg = os.path.join(ROOT, "mm_bot", "conf", "as_model.yaml")
    cfg_path = os.getenv("XTB_CONFIG") or default_cfg
    cfg = load_config(cfg_path)
    log_cfg = ((cfg.get("general") or {}).get("log_config")) or os.path.join(ROOT, "mm_bot", "conf", "logging.yaml")
    setup_logging(log_cfg)
    log = logging.getLogger("mm_bot.bin.run_as_model")

    general = cfg.get("general") or {}
    connector_cfg = cfg.get("connector") or {}
    strat_cfg = ((cfg.get("strategy") or {}).get("as_model")) or {}

    tick_size = float(general.get("tick_size", os.getenv("XTB_TICK_SIZE", 2.0)))
    symbol = general.get("symbol", os.getenv("XTB_SYMBOL"))

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

    params = ASParams(
        gamma=float(strat_cfg.get("gamma", 0.02)),
        k=float(strat_cfg.get("k", 1.5)),
        tau=float(strat_cfg.get("tau", 6.0)),
        beta=float(strat_cfg.get("beta", 1.4)),
        window_secs=float(strat_cfg.get("window_secs", 60.0)),
        min_spread_abs=float(strat_cfg.get("min_spread_abs", 5.0)),
        min_spread_bps=(float(strat_cfg.get("min_spread_bps")) if strat_cfg.get("min_spread_bps") is not None else None),
        requote_ticks=int(strat_cfg.get("requote_ticks", 1)),
        sigma_ewma_alpha=float(strat_cfg.get("sigma_ewma_alpha", 0.2)),
        size_base=(float(strat_cfg.get("size_base")) if strat_cfg.get("size_base") is not None else None),
        max_position_base=(float(strat_cfg.get("max_position_base")) if strat_cfg.get("max_position_base") is not None else None),
        recover_ratio=float(strat_cfg.get("recover_ratio", 2.0/3.0)),
        gamma_max=(float(strat_cfg.get("gamma_max")) if strat_cfg.get("gamma_max") is not None else None),
        beta_max=(float(strat_cfg.get("beta_max")) if strat_cfg.get("beta_max") is not None else None),
        telemetry_enabled=bool(int(strat_cfg.get("telemetry_enabled", 0))),
        telemetry_url=strat_cfg.get("telemetry_url"),
        telemetry_interval_secs=int(strat_cfg.get("telemetry_interval_secs", 30)),
    )

    strat = AvellanedaStoikovStrategy(connector=conn, symbol=symbol, params=params)
    core.set_strategy(strat)

    await core.start()
    log.info("AS model bot started")
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        log.info("AS model bot interrupted; stopping…")
    finally:
        await core.stop(cancel_orders=True)
        await core.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
