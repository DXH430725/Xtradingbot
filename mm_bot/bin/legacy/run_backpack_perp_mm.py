import asyncio
import logging
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mm_bot.logger.logger import setup_logging
from mm_bot.core.trading_core import TradingCore
from mm_bot.connector.backpack.backpack_exchange import BackpackConnector, BackpackConfig
from mm_bot.strategy.backpack_perp_market_maker import (
    BackpackPerpMarketMakerStrategy,
    PerpMarketMakerParams,
)
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


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def main():
    cfg_env = os.getenv("XTB_CONFIG")
    default_cfg = os.path.join(ROOT, "mm_bot", "conf", "backpack_perp_mm.yaml")
    cfg_path = cfg_env or default_cfg
    cfg = load_config(cfg_path)

    general = cfg.get("general") or {}
    connector_cfg = cfg.get("connector") or {}
    strat_cfg = ((cfg.get("strategy") or {}).get("backpack_perp_mm")) or {}

    log_cfg = general.get("log_config") or os.path.join(ROOT, "mm_bot", "conf", "logging.yaml")
    setup_logging(log_cfg)
    log = logging.getLogger("mm_bot.bin.run_backpack_perp_mm")

    tick_size = _as_float(general.get("tick_size", os.getenv("XTB_TICK_SIZE", 1.0)), 1.0)

    keys_file = connector_cfg.get("keys_file") or os.getenv(
        "XTB_BACKPACK_KEYS_FILE", os.path.join(os.getcwd(), "Backpack_key.txt")
    )
    base_url = connector_cfg.get("base_url") or os.getenv("XTB_BACKPACK_BASE_URL", "https://api.backpack.exchange")
    ws_url = connector_cfg.get("ws_url") or os.getenv("XTB_BACKPACK_WS_URL", "wss://ws.backpack.exchange")
    rpm = _as_int(connector_cfg.get("rpm", os.getenv("XTB_BACKPACK_RPM", 300)), 300)
    window_ms = _as_int(connector_cfg.get("window_ms", os.getenv("XTB_BACKPACK_WINDOW_MS", 5000)), 5000)
    broker_id = connector_cfg.get("broker_id") or os.getenv("XTB_BACKPACK_BROKER_ID", "1500")

    symbol = str(strat_cfg.get("symbol") or os.getenv("XTB_BP_MM_SYMBOL", "SOL_USDC_PERP")).upper()
    params = PerpMarketMakerParams(
        symbol=symbol,
        base_spread_pct=_as_float(strat_cfg.get("base_spread_pct", os.getenv("XTB_BP_MM_SPREAD", 0.2)), 0.2),
        order_quantity=(
            _as_float(strat_cfg.get("order_quantity", os.getenv("XTB_BP_MM_QTY")))
            if strat_cfg.get("order_quantity") is not None or os.getenv("XTB_BP_MM_QTY") is not None
            else None
        ),
        max_orders=_as_int(strat_cfg.get("max_orders", os.getenv("XTB_BP_MM_MAX_ORDERS", 3)), 3),
        target_position=_as_float(strat_cfg.get("target_position", os.getenv("XTB_BP_MM_TARGET_POS", 0.0)), 0.0),
        max_position=_as_float(strat_cfg.get("max_position", os.getenv("XTB_BP_MM_MAX_POS", 1.0)), 1.0),
        position_threshold=_as_float(
            strat_cfg.get("position_threshold", os.getenv("XTB_BP_MM_POS_THRESHOLD", 0.1)),
            0.1,
        ),
        inventory_skew=_as_float(strat_cfg.get("inventory_skew", os.getenv("XTB_BP_MM_SKEW", 0.0)), 0.0),
        tick_interval_secs=_as_float(
            strat_cfg.get("tick_interval_secs", os.getenv("XTB_BP_MM_TICK_INTERVAL", 15.0)),
            15.0,
        ),
        cancel_before_place=_as_bool(
            strat_cfg.get("cancel_before_place", os.getenv("XTB_BP_MM_CANCEL_BEFORE", "1")),
            True,
        ),
        post_only=_as_bool(strat_cfg.get("post_only", os.getenv("XTB_BP_MM_POST_ONLY", "1")), True),
    )

    core = TradingCore(tick_size=tick_size, debug=os.getenv("XTB_DEBUG") in {"1", "true", "yes", "on"})

    backpack_cfg = BackpackConfig(
        base_url=base_url,
        ws_url=ws_url,
        keys_file=keys_file,
        window_ms=window_ms,
        rpm=rpm,
        broker_id=broker_id,
    )
    backpack = BackpackConnector(config=backpack_cfg)
    backpack.start()
    await backpack.start_ws_state([symbol])

    core.add_connector("backpack", backpack)

    strategy = BackpackPerpMarketMakerStrategy(
        connector=backpack,
        params=params,
    )
    core.set_strategy(strategy)

    await core.start()
    log.info(
        "Backpack perp MM started symbol=%s spread=%.4f%% qty=%s", symbol, params.base_spread_pct, params.order_quantity
    )

    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        log.info("Backpack perp MM interrupted; shutting down...")
    finally:
        await core.stop(cancel_orders=True)
        await core.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
