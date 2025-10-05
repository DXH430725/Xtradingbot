from __future__ import annotations

import argparse
import asyncio
from typing import Dict

from xbot.connector.factory import build_connector
from xbot.core.clock import WallClock
from xbot.core.lifecycle import LifecycleController
from xbot.core.heartbeat import HeartbeatService
from xbot.execution.market_data_service import MarketDataService
from xbot.execution.order_service import OrderService
from xbot.execution.position_service import PositionService
from xbot.execution.risk_service import RiskService
from xbot.execution.tracking_limit import TrackingLimitEngine
from xbot.execution.router import ExecutionRouter
from xbot.strategy.base import StrategyConfig
from xbot.strategy.market import MarketOrderStrategy
from xbot.strategy.tracking_limit import TrackingLimitStrategy
from xbot.strategy.diagnostic import DiagnosticStrategy
from xbot.utils.logging import get_logger, setup_logging
from .config import AppConfig, load_config


STRATEGY_REGISTRY: Dict[str, str] = {
    "market": "market",
    "tracking_limit": "tracking_limit",
    "diagnostic": "diagnostic",
}


async def run(cfg: AppConfig, log_level: str) -> None:
    setup_logging(log_level)
    logger = get_logger(__name__)
    connector = build_connector(cfg.venue)
    market_data = MarketDataService(connector=connector, symbol_map=cfg.symbol_map)
    position_service = PositionService()
    risk_service = RiskService(market_data=market_data, position_service=position_service, limits=cfg.risk_limits)
    tracking_engine = TrackingLimitEngine(
        market_data=market_data,
        default_interval_secs=cfg.interval_secs,
        default_timeout_secs=cfg.timeout_secs,
    )
    order_service = OrderService(
        connector=connector,
        market_data=market_data,
        risk_service=risk_service,
        tracking_engine=tracking_engine,
    )
    router = ExecutionRouter(
        order_service=order_service,
        position_service=position_service,
        risk_service=risk_service,
        market_data=market_data,
    )
    lifecycle = LifecycleController(connector=connector)
    clock = WallClock()
    heartbeat: HeartbeatService | None = None
    strategy_cfg = StrategyConfig(
        symbol=cfg.symbol,
        mode=cfg.mode,
        qty=cfg.qty,
        side=cfg.side,
        reduce_only=cfg.reduce_only,
        price_offset_ticks=cfg.price_offset_ticks,
        interval_secs=cfg.interval_secs,
        timeout_secs=cfg.timeout_secs,
    )
    if cfg.mode == "tracking_limit":
        strategy = TrackingLimitStrategy(router=router, clock=clock, config=strategy_cfg)
    elif cfg.mode == "market":
        strategy = MarketOrderStrategy(router=router, clock=clock, config=strategy_cfg)
    elif cfg.mode == "diagnostic":
        strategy = DiagnosticStrategy(router=router, clock=clock, config=strategy_cfg)
    else:
        raise ValueError(f"unsupported mode: {cfg.mode}")

    await lifecycle.start()
    try:
        if cfg.heartbeat_config:
            heartbeat = HeartbeatService(
                connector=connector,
                router=router,
                clock=clock,
                strategy_name=strategy_cfg.mode,
                venue=cfg.venue,
                config=cfg.heartbeat_config,
            )
            await heartbeat.start()
        logger.info("strategy_start", extra={"venue": cfg.venue, "mode": cfg.mode, "symbol": cfg.symbol})
        await strategy.start()
    finally:
        logger.info("strategy_stop", extra={"venue": cfg.venue})
        if heartbeat:
            await heartbeat.stop()
        await lifecycle.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="xbot multi-exchange strategy runner")
    parser.add_argument("--venue", required=True, help="venue identifier, e.g. backpack")
    parser.add_argument("--symbol", required=True, help="canonical symbol, e.g. SOL")
    parser.add_argument("--mode", default="market", choices=sorted(STRATEGY_REGISTRY.keys()))
    parser.add_argument("--qty", type=float, required=True, help="order quantity in base units")
    parser.add_argument("--side", default="buy", choices=["buy", "sell"])
    parser.add_argument("--price-offset-ticks", type=int, default=0)
    parser.add_argument("--interval-secs", type=float, default=10.0)
    parser.add_argument("--timeout-secs", type=float, default=120.0)
    parser.add_argument("--reduce-only", type=int, default=0)
    parser.add_argument("--config", dest="config_path")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(
        venue=args.venue,
        symbol=args.symbol,
        qty=args.qty,
        side=args.side,
        mode=args.mode,
        price_offset_ticks=args.price_offset_ticks,
        interval_secs=args.interval_secs,
        timeout_secs=args.timeout_secs,
        reduce_only=args.reduce_only,
        config_path=args.config_path,
    )
    asyncio.run(run(cfg, args.log_level))


if __name__ == "__main__":
    main()
