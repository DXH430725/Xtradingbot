import os
import json
from typing import Any, Dict, Optional


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """
    Load bot configuration from YAML or JSON.
    Priority: explicit path -> env XTB_CONFIG -> default mm_bot/conf/bot.yaml.
    If both YAML and JSON fail, return empty dict.
    """
    cfg_path = path or os.getenv("XTB_CONFIG")
    if not cfg_path:
        # default to mm_bot/conf/bot.yaml relative to cwd
        default_yaml = os.path.join(os.getcwd(), "mm_bot", "conf", "bot.yaml")
        default_yml = os.path.join(os.getcwd(), "mm_bot", "conf", "bot.yml")
        if os.path.exists(default_yaml):
            cfg_path = default_yaml
        elif os.path.exists(default_yml):
            cfg_path = default_yml
        else:
            # try example
            example = os.path.join(os.getcwd(), "mm_bot", "conf", "bot.example.yaml")
            cfg_path = example if os.path.exists(example) else None

    if not cfg_path or not os.path.exists(cfg_path):
        return {}

    # attempt YAML then JSON
    if cfg_path.endswith((".yml", ".yaml")):
        try:
            import yaml  # type: ignore
            with open(cfg_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

