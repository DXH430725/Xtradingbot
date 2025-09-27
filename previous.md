# Progress Summary (2025-09-27)
- Refactored liquidation hedge into tri-venue workflow (Backpack + lighter1 + lighter2), added telemetry heartbeats and Telegram notifications, COI/nonce handling, and market-based BP rebalance.
- Built dual-target lighter_min_order diagnostic strategy plus config to probe nonce/volume issues across both Lighter API keys.
- Improved telemetry dashboard: new prune endpoint, grouped BP/L1/L2 cards, and auth-aware controls; added shared telemetry config JSON for strategies.
- Verified Lighter position signs and adjusted connector reads (`sign * position`), ensuring hedger detects short exposure correctly.
- Updated configs/builders to surface new telemetry settings and dual-connector requirements.

Next: run hedge against live venues (with distinct API key indices) to confirm L2 entry + BP rebalance flow, monitor dashboard, and iterate on size/permission issues if Lighter still trims fills.
