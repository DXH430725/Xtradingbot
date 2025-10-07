from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional
import logging

import websockets


# Make vendored SDK importable (prefer sdk/lighter-python)
REPO_ROOT = Path(__file__).resolve().parents[2]
for p in [REPO_ROOT / "sdk" / "lighter-python", REPO_ROOT / "sdk" / "lighter"]:
    if p.exists():
        sp = str(p)
        if sp not in sys.path:
            sys.path.insert(0, sp)


def load_keys(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    content = path.read_text(encoding="utf-8")
    pairs = [line.split(":", 1) for line in content.splitlines() if ":" in line]
    out = {k.strip(): v.strip() for k, v in pairs}
    # normalize common variants
    norm: Dict[str, str] = {}
    for k, v in out.items():
        norm[k.strip().lower()] = v.strip()
    return norm


def key_get(keys: Dict[str, str], *names: str) -> Optional[str]:
    for n in names:
        n_low = n.lower()
        if n_low in keys and keys[n_low]:
            return keys[n_low]
    return None


async def resolve_market_id(symbol: str, base_url: str) -> int:
    import lighter  # type: ignore
    api = lighter.ApiClient(configuration=lighter.Configuration(host=base_url))
    try:
        ob = await lighter.OrderApi(api).order_books()
        for m in getattr(ob, "order_books", []) or []:
            if getattr(m, "symbol", None) == symbol:
                return int(getattr(m, "market_id"))
        raise RuntimeError(f"symbol {symbol} not found in order_books")
    finally:
        await api.close()


async def build_auth_token(base_url: str, key_file: Path, *, account_index: int, api_key_index: int) -> Optional[str]:
    try:
        import lighter  # type: ignore
        keys = load_keys(key_file)
        priv = key_get(keys, "api_key_private_key", "apikeyprivatekey", "private_key", "privatekey") or ""
        signer = lighter.SignerClient(
            url=base_url,
            private_key=priv,
            account_index=account_index,
            api_key_index=api_key_index,
        )
        deadline = int(time.time() + 10 * 60)
        token, err = signer.create_auth_token_with_expiry(deadline)
        try:
            if getattr(signer, "api_client", None) is not None:
                await signer.api_client.close()  # type: ignore[attr-defined]
        except Exception:
            pass
        if err is not None:
            print(f"[auth_error] {err}")
            return None
        return token
    except Exception as exc:
        print(f"[auth_exception] {exc}")
        return None


def _configure_logging() -> None:
    # Silence noisy DEBUG logs from websockets and asyncio
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("websockets.client").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


async def probe(symbol: str, *, base_url: str, ws_url: str, key_file: Path, log_path: Path, only_account: bool = True) -> None:
    market_id = await resolve_market_id(symbol, base_url)
    keys = load_keys(key_file)
    acct_s = key_get(keys, "account_index", "accountindex") or "0"
    api_k_s = key_get(keys, "api_key_index", "apikeyindex") or "0"
    account_index = int(acct_s)
    api_key_index = int(api_k_s)
    print(f"[init] symbol={symbol} market_id={market_id} account_index={account_index} api_key_index={api_key_index}")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    
    async def run_once() -> None:
        token = await build_auth_token(base_url, key_file, account_index=account_index, api_key_index=api_key_index)
        streams = [] if only_account else [f"order_book/{market_id}", f"trade/{market_id}"]
        if account_index > 0:
            if token:
                streams.append(f"account_orders/{market_id}/{account_index}")
            streams.append(f"account_all/{account_index}")
        print(f"[connect] ws={ws_url} streams={streams} has_token={bool(token)}")
        async with websockets.connect(ws_url, ping_interval=55, ping_timeout=10, max_size=2 ** 22) as ws:
            if not only_account:
                await ws.send(json.dumps({"type": "subscribe", "channel": f"order_book/{market_id}"}))
                try:
                    await ws.send(json.dumps({"type": "subscribe", "channel": f"trade/{market_id}"}))
                except Exception:
                    pass
            if account_index > 0:
                if token:
                    await ws.send(json.dumps({"type": "subscribe", "channel": f"account_orders/{market_id}/{account_index}", "auth": token}))
                await ws.send(json.dumps({"type": "subscribe", "channel": f"account_all/{account_index}"}))

            async for raw in ws:
                try:
                    msg: Dict[str, Any] = json.loads(raw)
                except Exception:
                    continue
                et = msg.get("type") or msg.get("event")
                # Compact printing focused on account/orders/positions
                if et and ("account" in et or et.startswith("order_update")):
                    print(f"[msg] type={et} keys={list(msg.keys())}")
                    orders = None
                    if isinstance(msg.get("orders"), list):
                        orders = msg["orders"]
                    elif isinstance(msg.get("data"), dict) and isinstance(msg["data"].get("orders"), list):
                        orders = msg["data"]["orders"]
                    elif isinstance(msg.get("account"), dict) and isinstance(msg["account"].get("orders"), list):
                        orders = msg["account"]["orders"]
                    if isinstance(orders, list):
                        print(f"  orders={len(orders)}")
                        for o in orders[:3]:
                            coi = o.get("client_order_index") or o.get("client_order_id") or o.get("coi")
                            oi = o.get("order_index") or o.get("orderId")
                            st = o.get("status") or o.get("state")
                            fb = o.get("filled_base_amount") or o.get("filled")
                            rb = o.get("remaining_base_amount") or o.get("remaining")
                            print(f"    coi={coi} oi={oi} status={st} filled={fb} remaining={rb}")
                    positions = None
                    if isinstance(msg.get("positions"), list):
                        positions = msg["positions"]
                    elif isinstance(msg.get("data"), dict) and isinstance(msg["data"].get("positions"), list):
                        positions = msg["data"]["positions"]
                    elif isinstance(msg.get("account"), dict) and isinstance(msg["account"].get("positions"), list):
                        positions = msg["account"]["positions"]
                    if isinstance(positions, list):
                        print(f"  positions={len(positions)}")
                        for p in positions[:3]:
                            sym = p.get("symbol")
                            pos = p.get("position") or p.get("net_size")
                            print(f"    {sym} pos={pos}")
                # Append full JSON to log
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": time.time(), "msg": msg}, ensure_ascii=False) + "\n")

    backoff = 3.0
    while True:
        try:
            await run_once()
            backoff = 3.0
        except asyncio.CancelledError:
            break
        except Exception as exc:
            print(f"[ws_error] {exc}; reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Probe Lighter WS streams and log raw messages")
    p.add_argument("--symbol", required=True, help="venue symbol, e.g., BTC")
    p.add_argument("--base-url", default="https://mainnet.zklighter.elliot.ai")
    p.add_argument("--ws-url", default="wss://mainnet.zklighter.elliot.ai/stream")
    p.add_argument("--key-file", default=str(Path.cwd() / "Lighter_key.txt"))
    p.add_argument("--log", default=str(Path.cwd() / "logs" / "lighter_ws_probe.jsonl"))
    p.add_argument("--include-public", action="store_true", help="also subscribe to public streams (order_book/trade)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    _configure_logging()
    only_account = not args.include_public
    asyncio.run(
        probe(
            args.symbol,
            base_url=args.base_url,
            ws_url=args.ws_url,
            key_file=Path(args.key_file),
            log_path=Path(args.log),
            only_account=only_account,
        )
    )


if __name__ == "__main__":
    main()
