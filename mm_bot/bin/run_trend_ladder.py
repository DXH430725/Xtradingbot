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
from mm_bot.strategy.trend_ladder import TrendAdaptiveLadderStrategy, TrendLadderParams
from mm_bot.conf.config import load_config


async def main():
    cfg = load_config()
    log_cfg = ((cfg.get("general") or {}).get("log_config")) or os.path.join(ROOT, "mm_bot", "conf", "logging.yaml")
    setup_logging(log_cfg)
    log = logging.getLogger("mm_bot.bin.run_trend_ladder")

    general = cfg.get("general") or {}
    connector_cfg = cfg.get("connector") or {}
    strat_cfg = ((cfg.get("strategy") or {}).get("trend_ladder")) or {}

    tick_size = float(general.get("tick_size", os.getenv("XTB_TICK_SIZE", 2.0)))
    # symbol priority: strategy.trend_ladder.symbol -> general.symbol -> env XTB_SYMBOL -> None (auto-pick BTC)
    symbol = strat_cfg.get("symbol") or general.get("symbol") or os.getenv("XTB_SYMBOL")

    rpm = int(connector_cfg.get("rpm", os.getenv("XTB_LIGHTER_RPM", 4000)))
    base_url = connector_cfg.get("base_url", os.getenv("XTB_LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai"))
    keys_file = connector_cfg.get("keys_file", os.getenv("XTB_LIGHTER_KEYS_FILE", os.path.join(os.getcwd(), "Lighter_key.txt")))
    account_index = connector_cfg.get("account_index", os.getenv("XTB_LIGHTER_ACCOUNT_INDEX"))
    if account_index is not None:
        try:
            account_index = int(account_index)
        except Exception:
            account_index = None

    params = TrendLadderParams(
        quantity_base=float(strat_cfg.get("quantity_base", os.getenv("XTB_TL_QTY", 0.8))),
        take_profit_abs=float(strat_cfg.get("take_profit_abs", os.getenv("XTB_TL_TP", 0.9))),
        max_orders=int(strat_cfg.get("max_orders", os.getenv("XTB_TL_MAX_ORDERS", 40))),
        base_wait=float(strat_cfg.get("base_wait", os.getenv("XTB_TL_BASE_WAIT", 450.0))),
        min_wait=float(strat_cfg.get("min_wait", os.getenv("XTB_TL_MIN_WAIT", 60.0))),
        max_wait=float(strat_cfg.get("max_wait", os.getenv("XTB_TL_MAX_WAIT", 900.0))),
        fixed_direction=str(strat_cfg.get("fixed_direction", os.getenv("XTB_TL_DIRECTION", "long"))).lower(),
        ema_len=int(strat_cfg.get("ema_len", os.getenv("XTB_TL_EMA", 50))),
        atr_len=int(strat_cfg.get("atr_len", os.getenv("XTB_TL_ATR", 14))),
        slope_up=float(strat_cfg.get("slope_up", os.getenv("XTB_TL_SLOPE_UP", 0.2))),
        slope_down=float(strat_cfg.get("slope_down", os.getenv("XTB_TL_SLOPE_DOWN", -0.2))),
        warmup_minutes=int(strat_cfg.get("warmup_minutes", os.getenv("XTB_TL_WARMUP", 50))),
        flush_ticks=int(strat_cfg.get("flush_ticks", os.getenv("XTB_TL_FLUSH_TICKS", 1))),
        imb_threshold_mult=int(strat_cfg.get("imb_threshold_mult", os.getenv("XTB_TL_IMB_THRESHOLD_MULT", 3))),
        imb_max_corr_in_10m=int(strat_cfg.get("imb_max_corr_in_10m", os.getenv("XTB_TL_IMB_MAX_CORR_10M", 3))),
        partial_tp_enabled=bool(int(strat_cfg.get("partial_tp_enabled", os.getenv("XTB_TL_PARTIAL_TP", 1)))),
        pace_ignore_imbalance_tp=bool(int(strat_cfg.get("pace_ignore_imbalance_tp", os.getenv("XTB_TL_PACE_IGNORE_IMBTP", 0)))),
        requote_ticks=int(strat_cfg.get("requote_ticks", os.getenv("XTB_TL_REQUOTE_TICKS", 5))),
        max_requotes_per_tick=int(strat_cfg.get("max_requotes_per_tick", os.getenv("XTB_TL_MAX_REQUOTES", 2))),
        requote_abs=float(strat_cfg.get("requote_abs", os.getenv("XTB_TL_REQUOTE_ABS", 0.0))),
        requote_wait_confirm_secs=float(strat_cfg.get("requote_wait_confirm_secs", os.getenv("XTB_TL_REQUOTE_WAIT_SECS", 5.0))),
        requote_skip_backoff_secs=float(strat_cfg.get("requote_skip_backoff_secs", os.getenv("XTB_TL_REQUOTE_BACKOFF", 15.0))),
        requote_skip_backoff_max_secs=float(strat_cfg.get("requote_skip_backoff_max_secs", os.getenv("XTB_TL_REQUOTE_BACKOFF_MAX", 120.0))),
        telemetry_enabled=bool(int(strat_cfg.get("telemetry_enabled", os.getenv("XTB_TL_TELEM_ENABLED", 0)))) ,
        telemetry_url=strat_cfg.get("telemetry_url", os.getenv("XTB_TL_TELEM_URL")),
        telemetry_interval_secs=int(strat_cfg.get("telemetry_interval_secs", os.getenv("XTB_TL_TELEM_INTERVAL", 60))),
    )

    core = TradingCore(tick_size=tick_size, debug=os.getenv("XTB_DEBUG") in {"1","true","yes","on"})
    # choose connector
    connector_name = str(general.get("connector", os.getenv("XTB_CONNECTOR", "lighter")).lower())
    if connector_name == "grvt":
        conn = GrvtConnector(config=GrvtConfig(), debug=False)
        conn.start()
        core.add_connector("grvt", conn)
    else:
        conn = LighterConnector(config=LighterConfig(base_url=base_url, keys_file=keys_file, account_index=account_index, rpm=rpm), debug=False)
        conn.start()
        core.add_connector("lighter", conn)

    strat = TrendAdaptiveLadderStrategy(connector=conn, symbol=symbol, params=params)
    core.set_strategy(strat)

    await core.start()
    log.info("TrendLadder bot started")
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        log.info("TrendLadder bot interrupted; stopping…")
    finally:
        await core.stop(cancel_orders=True)
        await core.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
