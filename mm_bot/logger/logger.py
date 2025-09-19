import logging
import logging.config
import os
from typing import Optional


def default_logging_dict(log_dir: str) -> dict:
    os.makedirs(log_dir, exist_ok=True)
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": os.getenv("XTB_LOG_LEVEL", "INFO"),
                "formatter": "standard",
                "stream": "ext://sys.stdout",
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": os.getenv("XTB_FILE_LOG_LEVEL", "DEBUG"),
                "formatter": "standard",
                "filename": os.path.join(log_dir, "bot.log"),
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 5,
                "encoding": "utf-8",
            },
        },
        "root": {
            "level": os.getenv("XTB_ROOT_LOG_LEVEL", "INFO"),
            "handlers": ["console", "file"],
        },
        "loggers": {
            # keep noisy libs quieter by default
            "asyncio": {"level": "WARNING"},
            "urllib3": {"level": "WARNING"},
            "websockets.client": {"level": "WARNING"},
            "websockets.protocol": {"level": "WARNING"},
        },
    }


def setup_logging(config_path: Optional[str] = None) -> None:
    """
    Initialize logging using a YAML/JSON config file if provided;
    otherwise use a sensible default with console and rotating file.
    Env overrides: XTB_LOG_DIR, XTB_LOG_LEVEL, XTB_FILE_LOG_LEVEL, XTB_ROOT_LOG_LEVEL
    """
    cfg = None
    if config_path and os.path.exists(config_path):
        try:
            if config_path.endswith((".yml", ".yaml")):
                try:
                    import yaml  # type: ignore
                except Exception:
                    yaml = None
                if yaml is not None:
                    with open(config_path, "r", encoding="utf-8") as f:
                        cfg = yaml.safe_load(f)
            else:
                import json
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
        except Exception:
            cfg = None

    log_dir = os.getenv("XTB_LOG_DIR", os.path.abspath(os.path.join(os.getcwd(), "logs")))
    if cfg is None:
        cfg = default_logging_dict(log_dir)

    # ensure file handler path exists if present
    try:
        logging.config.dictConfig(cfg)
    except Exception:
        # last resort
        logging.basicConfig(
            level=os.getenv("XTB_LOG_LEVEL", "INFO"),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

