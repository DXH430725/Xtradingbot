"""Unified CLI entrypoint - supports both direct CLI and YAML modes.

Target: < 300 lines.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Optional

from .config import CLIConfig, SystemConfig, find_config_file, resolve_keys_file
from ..core import TradingCore
from ..connector import MockConnector, MockConfig
from ..strategy import SmokeTestStrategy, SmokeTestConfig


def setup_logging(level: str = "INFO", debug: bool = False) -> None:
    """Setup logging configuration.

    Args:
        level: Log level string
        debug: Enable debug mode
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    if debug:
        log_level = logging.DEBUG

    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    )


def create_argument_parser() -> argparse.ArgumentParser:
    """Create command line argument parser.

    Returns:
        Configured ArgumentParser
    """
    parser = argparse.ArgumentParser(
        description="XBot - Cross-exchange arbitrage trading system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # CLI direct mode (smoke test)
  python -m xbot.app.main --venue lighter --symbol BTC --mode tracking_limit \\
    --side buy --timeout 120 --debug

  # YAML configuration mode
  python -m xbot.app.main --config config.yaml

  # List available modes
  python -m xbot.app.main --help
        """
    )

    # Config file mode
    parser.add_argument(
        '--config', '-c',
        help='Path to YAML configuration file'
    )

    # CLI direct mode
    parser.add_argument(
        '--venue',
        help='Exchange venue (backpack, lighter, grvt, mock)'
    )

    parser.add_argument(
        '--symbol',
        help='Trading symbol (BTC, ETH, SOL)'
    )

    parser.add_argument(
        '--mode',
        choices=['tracking_limit', 'limit_once', 'market'],
        default='tracking_limit',
        help='Test mode (default: tracking_limit)'
    )

    parser.add_argument(
        '--side',
        choices=['buy', 'sell'],
        default='buy',
        help='Order side (default: buy)'
    )

    parser.add_argument(
        '--size-multiplier',
        type=float,
        default=1.0,
        help='Size multiplier for minimum order size (default: 1.0)'
    )

    parser.add_argument(
        '--price-offset',
        type=int,
        default=2,
        help='Price offset in ticks from top of book (default: 2)'
    )

    parser.add_argument(
        '--interval',
        type=float,
        default=10.0,
        help='Tracking interval in seconds (default: 10.0)'
    )

    parser.add_argument(
        '--timeout',
        type=float,
        default=120.0,
        help='Total timeout in seconds (default: 120.0)'
    )

    parser.add_argument(
        '--max-attempts',
        type=int,
        default=3,
        help='Maximum attempts (default: 3)'
    )

    # Connector settings
    parser.add_argument(
        '--keys-file',
        help='Path to keys file'
    )

    parser.add_argument(
        '--base-url',
        help='Base URL for exchange API'
    )

    # Global settings
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug mode'
    )

    parser.add_argument(
        '--list-venues',
        action='store_true',
        help='List supported venues and exit'
    )

    return parser


def list_supported_venues() -> None:
    """Print list of supported venues."""
    venues = [
        ("mock", "Mock connector for testing"),
        ("backpack", "Backpack exchange (requires keys)"),
        ("lighter", "Lighter exchange (requires keys)"),
        ("grvt", "GRVT exchange (requires keys)")
    ]

    print("Supported venues:")
    for venue, description in venues:
        print(f"  {venue:<12} - {description}")


def create_connector(venue: str, config: SystemConfig) -> Optional[any]:
    """Create connector for venue.

    Args:
        venue: Venue name
        config: System configuration

    Returns:
        Connector instance or None if unsupported
    """
    venue_key = venue.lower()
    connector_config = config.connectors.get(venue_key)

    if venue_key == "mock":
        mock_config = MockConfig(
            venue_name=venue,
            latency_ms=50.0,
            auto_fill_rate=0.8
        )
        return MockConnector(mock_config, debug=config.general.debug)

    # TODO: Add real connector implementations
    # elif venue_key == "backpack":
    #     return create_backpack_connector(connector_config)
    # elif venue_key == "lighter":
    #     return create_lighter_connector(connector_config)
    # elif venue_key == "grvt":
    #     return create_grvt_connector(connector_config)

    print(f"Error: Venue '{venue}' not yet implemented")
    return None


async def run_with_config(config: SystemConfig) -> int:
    """Run system with configuration.

    Args:
        config: System configuration

    Returns:
        Exit code (0 for success)
    """
    # Setup logging
    setup_logging(config.general.log_level, config.general.debug)
    log = logging.getLogger("xbot.app.main")

    if not config.strategy:
        log.error("No strategy configured")
        return 1

    # Create trading core
    core = TradingCore(
        tick_interval_ms=config.general.tick_ms,
        debug=config.general.debug
    )

    # Create and add connectors
    for venue_name in config.connectors.keys():
        connector = create_connector(venue_name, config)
        if not connector:
            log.error(f"Failed to create connector for {venue_name}")
            return 1

        core.add_connector(venue_name, connector)
        log.info(f"Added connector: {venue_name}")

    # Create strategy
    if config.strategy.name == "smoke_test":
        smoke_config = SmokeTestConfig(
            venue=config.strategy.params["venue"],
            symbol=config.strategy.params["symbol"],
            mode=config.strategy.params.get("mode", "tracking_limit"),
            side=config.strategy.params.get("side", "buy"),
            size_multiplier=config.strategy.params.get("size_multiplier", 1.0),
            price_offset_ticks=config.strategy.params.get("price_offset_ticks", 2),
            interval_secs=config.strategy.params.get("interval_secs", 10.0),
            timeout_secs=config.strategy.params.get("timeout_secs", 120.0),
            max_attempts=config.strategy.params.get("max_attempts", 3),
            debug=config.general.debug
        )

        strategy = SmokeTestStrategy(smoke_config, core.router)
        core.set_strategy(strategy)
        log.info("Created smoke test strategy")

    else:
        log.error(f"Unknown strategy: {config.strategy.name}")
        return 1

    # Run the system
    try:
        log.info("Starting trading system...")
        await core.start()

        log.info("System running, waiting for completion...")
        await core.wait_until_stopped()

        # Check results for smoke test
        if hasattr(strategy, 'overall_success'):
            if strategy.overall_success:
                log.info("Strategy completed successfully")
                return 0
            else:
                log.error(f"Strategy failed: {strategy.failure_reason}")
                return 1
        else:
            log.info("Strategy completed")
            return 0

    except KeyboardInterrupt:
        log.info("Interrupted by user")
        return 130

    except Exception as e:
        log.error(f"System error: {e}")
        if config.general.debug:
            log.exception("Full error details")
        return 1

    finally:
        log.info("Shutting down...")
        await core.shutdown()


async def main() -> int:
    """Main entry point.

    Returns:
        Exit code
    """
    parser = create_argument_parser()
    args = parser.parse_args()

    # Handle special commands
    if args.list_venues:
        list_supported_venues()
        return 0

    # Determine mode: YAML config or CLI direct
    if args.config:
        # YAML configuration mode
        try:
            config = SystemConfig.from_yaml(args.config)
            print(f"Loaded configuration from: {args.config}")
        except Exception as e:
            print(f"Error loading config file: {e}")
            return 1

    elif args.venue and args.symbol:
        # CLI direct mode
        cli_config = CLIConfig(
            venue=args.venue,
            symbol=args.symbol,
            mode=args.mode,
            side=args.side,
            size_multiplier=args.size_multiplier,
            price_offset_ticks=args.price_offset,
            interval_secs=args.interval,
            timeout_secs=args.timeout,
            max_attempts=args.max_attempts,
            debug=args.debug,
            keys_file=resolve_keys_file(args.venue, args.keys_file),
            base_url=args.base_url
        )

        config = cli_config.to_system_config()
        print(f"CLI mode: {args.venue}:{args.symbol} ({args.mode})")

    else:
        # Try to find default config file
        config_file = find_config_file()
        if config_file:
            try:
                config = SystemConfig.from_yaml(config_file)
                print(f"Using default config: {config_file}")
            except Exception as e:
                print(f"Error loading default config: {e}")
                return 1
        else:
            print("Error: No configuration provided")
            print("Either specify --config <file> or --venue <venue> --symbol <symbol>")
            print("Use --help for more information")
            return 1

    # Run the system
    return await run_with_config(config)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))