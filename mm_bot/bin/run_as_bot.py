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
from mm_bot.strategy.as_model import AvellanedaStoikovStrategy, ASParams
from mm_bot.conf.config import load_config


async def main():
    cfg = load_config()
    log_cfg = ((cfg.get("general") or {}).get("log_config")) or os.path.join(ROOT, "mm_bot", "conf", "logging.yaml")
    setup_logging(log_cfg)
    log = logging.getLogger("mm_bot.bin.run_as_bot")

    general = cfg.get("general") or {}
    connector_cfg = cfg.get("connector") or {}
    strat_cfg = ((cfg.get("strategy") or {}).get("as_model")) or {}

    tick_size = float(general.get("tick_size", os.getenv("XTB_TICK_SIZE", 5.0)))
    symbol = general.get("symbol", os.getenv("XTB_SYMBOL"))

    rpm = int(connector_cfg.get("rpm", os.getenv("XTB_LIGHTER_RPM", 4000)))
    base_url = connector_cfg.get("base_url", os.getenv("XTB_LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"))
    keys_file = connector_cfg.get("keys_file", os.getenv("XTB_LIGHTER_KEYS_FILE", os.path.join(os.getcwd(), "Lighter_key.txt")))
    account_index = connector_cfg.get("account_index", os.getenv("XTB_LIGHTER_ACCOUNT_INDEX"))
    if account_index is not None:
        try:
            account_index = int(account_index)
        except Exception:
            account_index = None

    params = ASParams(
        gamma=float(strat_cfg.get("gamma", os.getenv("XTB_AS_GAMMA", 0.02))),
        k=float(strat_cfg.get("k", os.getenv("XTB_AS_K", 1.5))),
        tau=float(strat_cfg.get("tau", os.getenv("XTB_AS_TAU", 6.0))),
        beta=float(strat_cfg.get("beta", os.getenv("XTB_AS_BETA", 1.4))),
        window_secs=float(strat_cfg.get("window_secs", os.getenv("XTB_AS_WINDOW", 60.0))),
        min_spread_abs=float(strat_cfg.get("min_spread_abs", os.getenv("XTB_AS_MIN_SPREAD", 5.0))),
    )

    core = TradingCore(tick_size=tick_size, debug=os.getenv("XTB_DEBUG") in {"1","true","yes","on"})
    conn = LighterConnector(config=LighterConfig(base_url=base_url, keys_file=keys_file, account_index=account_index, rpm=rpm), debug=False)
    conn.start()
    core.add_connector("lighter", conn)

    strat = AvellanedaStoikovStrategy(connector=conn, symbol=symbol, params=params)
    core.set_strategy(strat)

    await core.start()

    # run indefinitely until Ctrl+C
    log.info(f"AS bot started: gamma={gamma} k={k} tau={tau} beta={beta} window={window}s tick={tick_size}s")
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        log.info("AS bot interrupted; stopping…")
    finally:
        await core.stop(cancel_orders=True)
        await core.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
