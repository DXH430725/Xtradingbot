"""Configuration system with dataclass support.

Target: < 250 lines.
"""

from __future__ import annotations

import os
import yaml
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List


@dataclass
class GeneralConfig:
    """General system configuration."""
    debug: bool = False
    tick_ms: float = 1000.0
    log_level: str = "INFO"


@dataclass
class ConnectorConfig:
    """Configuration for a single connector."""
    venue_name: str = ""
    connector_type: str = ""  # backpack, lighter, grvt, mock
    keys_file: Optional[str] = None
    base_url: Optional[str] = None
    ws_url: Optional[str] = None
    broker_id: Optional[int] = None
    rpm: Optional[int] = None
    account_index: Optional[int] = None
    debug: bool = False

    # Add other common connector parameters as needed
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyConfig:
    """Strategy configuration."""
    name: str
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SystemConfig:
    """Complete system configuration."""
    general: GeneralConfig = field(default_factory=GeneralConfig)
    connectors: Dict[str, ConnectorConfig] = field(default_factory=dict)
    strategy: Optional[StrategyConfig] = None

    @classmethod
    def from_yaml(cls, file_path: str) -> 'SystemConfig':
        """Load configuration from YAML file.

        Args:
            file_path: Path to YAML configuration file

        Returns:
            SystemConfig instance

        Raises:
            FileNotFoundError: If config file doesn't exist
            yaml.YAMLError: If YAML parsing fails
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Config file not found: {file_path}")

        with open(file_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}

        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SystemConfig':
        """Create config from dictionary.

        Args:
            data: Configuration dictionary

        Returns:
            SystemConfig instance
        """
        # Parse general config
        general_data = data.get('general', {})
        general = GeneralConfig(
            debug=general_data.get('debug', False),
            tick_ms=general_data.get('tick_ms', 1000.0),
            log_level=general_data.get('log_level', 'INFO')
        )

        # Parse connector configs
        connectors = {}
        connectors_data = data.get('connectors', {})
        for name, conn_data in connectors_data.items():
            known_fields = ['venue_name', 'connector_type', 'keys_file', 'base_url',
                          'ws_url', 'broker_id', 'rpm', 'account_index', 'debug']
            connector = ConnectorConfig(
                venue_name=conn_data.get('venue_name', name),
                connector_type=conn_data.get('connector_type', name),
                keys_file=conn_data.get('keys_file'),
                base_url=conn_data.get('base_url'),
                ws_url=conn_data.get('ws_url'),
                broker_id=conn_data.get('broker_id'),
                rpm=conn_data.get('rpm'),
                account_index=conn_data.get('account_index'),
                debug=conn_data.get('debug', general.debug),
                extra={k: v for k, v in conn_data.items() if k not in known_fields}
            )
            connectors[name] = connector

        # Parse strategy config
        strategy = None
        strategy_data = data.get('strategy')
        if strategy_data:
            strategy = StrategyConfig(
                name=strategy_data.get('name', ''),
                params=strategy_data.get('params', {})
            )

        return cls(
            general=general,
            connectors=connectors,
            strategy=strategy
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert config to dictionary.

        Returns:
            Configuration as dictionary
        """
        return {
            'general': {
                'debug': self.general.debug,
                'tick_ms': self.general.tick_ms,
                'log_level': self.general.log_level
            },
            'connectors': {
                name: {
                    'venue_name': conn.venue_name,
                    'connector_type': conn.connector_type,
                    'keys_file': conn.keys_file,
                    'base_url': conn.base_url,
                    'ws_url': conn.ws_url,
                    'broker_id': conn.broker_id,
                    'rpm': conn.rpm,
                    'account_index': conn.account_index,
                    'debug': conn.debug,
                    **conn.extra
                }
                for name, conn in self.connectors.items()
            },
            'strategy': {
                'name': self.strategy.name,
                'params': self.strategy.params
            } if self.strategy else None
        }


@dataclass
class CLIConfig:
    """Configuration from CLI arguments - direct path."""
    venue: str
    symbol: str
    mode: str = "tracking_limit"
    side: str = "buy"
    size_multiplier: float = 1.0
    price_offset_ticks: int = 2
    interval_secs: float = 10.0
    timeout_secs: float = 120.0
    max_attempts: int = 3
    debug: bool = False

    # Connector settings for CLI mode
    keys_file: Optional[str] = None
    base_url: Optional[str] = None

    def to_system_config(self) -> SystemConfig:
        """Convert CLI config to SystemConfig for unified processing.

        Returns:
            SystemConfig equivalent
        """
        # Create connector config
        connector_config = ConnectorConfig(
            keys_file=self.keys_file,
            base_url=self.base_url
        )

        # Create strategy config (smoke test)
        strategy_config = StrategyConfig(
            name="smoke_test",
            params={
                "venue": self.venue,
                "symbol": self.symbol,
                "mode": self.mode,
                "side": self.side,
                "size_multiplier": self.size_multiplier,
                "price_offset_ticks": self.price_offset_ticks,
                "interval_secs": self.interval_secs,
                "timeout_secs": self.timeout_secs,
                "max_attempts": self.max_attempts
            }
        )

        # Create general config
        general_config = GeneralConfig(
            debug=self.debug,
            tick_ms=500.0,  # Faster ticks for CLI
            log_level="DEBUG" if self.debug else "INFO"
        )

        return SystemConfig(
            general=general_config,
            connectors={self.venue: connector_config},
            strategy=strategy_config
        )


def resolve_keys_file(venue: str, specified_path: Optional[str] = None) -> Optional[str]:
    """Resolve keys file path for venue.

    Args:
        venue: Venue name
        specified_path: Explicitly specified path

    Returns:
        Resolved keys file path or None
    """
    if specified_path:
        return specified_path

    # Try standard naming conventions
    standard_names = [
        f"{venue.title()}_key.txt",
        f"{venue.lower()}_key.txt",
        f"{venue}_keys.txt",
    ]

    for name in standard_names:
        if os.path.exists(name):
            return name

    return None


def get_default_config_paths() -> List[str]:
    """Get list of default configuration file paths to try.

    Returns:
        List of config file paths in order of preference
    """
    return [
        "xbot.yaml",
        "xbot.yml",
        "config.yaml",
        "config.yml",
        os.path.expanduser("~/.xbot/config.yaml"),
        "/etc/xbot/config.yaml"
    ]


def find_config_file() -> Optional[str]:
    """Find first existing config file from default paths.

    Returns:
        Path to existing config file or None
    """
    for path in get_default_config_paths():
        if os.path.exists(path):
            return path
    return None