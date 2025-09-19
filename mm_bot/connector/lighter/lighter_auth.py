import os
from typing import Optional, Tuple


def load_keys_from_file(path: str) -> Tuple[Optional[int], Optional[str], Optional[str], Optional[str]]:
    """
    Parse a simple key file with lines like:
      api key index: 2
      public key: <hex>
      private key: <hex>
      eth private key: 0x...
    Returns: (api_key_index, api_private_key, api_public_key, eth_private_key)
    """
    if not os.path.exists(path):
        return None, None, None, None
    api_key_index = None
    api_priv = None
    api_pub = None
    eth_priv = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            low = s.lower()
            if low.startswith("api key index:"):
                try:
                    api_key_index = int(s.split(":", 1)[1].strip())
                except Exception:
                    pass
            elif low.startswith("private key:") and "eth" not in low:
                api_priv = s.split(":", 1)[1].strip()
            elif low.startswith("public key:"):
                api_pub = s.split(":", 1)[1].strip()
            elif low.startswith("eth private key:"):
                eth_priv = s.split(":", 1)[1].strip()
    return api_key_index, api_priv, api_pub, eth_priv


def env_or(value: Optional[str], env_key: str) -> Optional[str]:
    v = os.getenv(env_key)
    return v if v is not None else value

