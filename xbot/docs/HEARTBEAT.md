# Heartbeat Service

The heartbeat service publishes periodic JSON snapshots to an HTTP endpoint once any strategy is live.

## Configuration
```yaml
heartbeat:
  url: "https://ops.example.com/xbot"
  interval_secs: 30    # optional, default 30
  timeout_secs: 5      # optional, default 5
  token: "bearer-token" # optional, used as Authorization: Bearer
```

Values can be specified in a config file (`--config`) or injected via environment variables when extending the loader.

## Payload
```json
{
  "ts": 1730000000,
  "strategy": "tracking_limit",
  "venue": "lighter",
  "positions": [
    {"symbol": "SOL", "net_size": "0.20", "entry_price": "224.55", "unrealized_pnl": "1.12"}
  ],
  "margin": {
    "collateral": "1100.0",
    "available_balance": "950.0",
    "total_asset_value": "1125.4"
  }
}
```
- `positions` and `margin` are sourced from the active connector on every tick.
- Failures (network timeouts, HTTP errors) are swallowed after logging; they never halt trading.
- Tokens are sent as `Authorization: Bearer <token>`; customise header injection in `core/heartbeat.py` if a different scheme is required.

## Extending
- Implement exponential backoff or structured logging by subclassing `HeartbeatService`.
- For signed requests, pre-compute headers in a wrapper that decorates the service before `start()`.
- If multiple strategies run concurrently, instantiate one heartbeat per venue or aggregate payloads in a supervisor process.
