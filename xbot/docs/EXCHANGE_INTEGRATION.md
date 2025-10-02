# Exchange Integration Guide

This framework treats each venue as an implementation of `connector.interface.IConnector`. To plug in a new exchange:

1. **Collect reference material**
   - REST + WS endpoints, authentication scheme, precision/lot info, and supported order types.
   - Decide on the canonical→venue symbol mapping (exposed through `execution.MarketDataService`).

2. **Create the connector module**
   - Drop a new file inside `connector/` (≤600 lines). Subclass `connector.base.BaseConnector` or implement the protocol directly.
   - Implement every method from the protocol. All prices and sizes must use integer scaling; connectors are responsible for converting to exchange-native units.
   - Adopt structured error handling: raise descriptive `RuntimeError`/`ValueError` variants and surface raw payloads via `info` dictionaries.

3. **Bootstrap metadata**
   - Cache price/size decimals and minimum size. Use this information to implement `get_price_size_decimals`, `get_min_size_i`, and `get_top_of_book`.
   - Ensure order idempotency: map `client_order_index` to the venue’s id system and return a human readable identifier.

4. **Streaming + reconciliation**
   - Subscribe to order/position feeds during `start()`. Route updates into `execution.order_service.OrderService.ingest_update` and `execution.position_service.PositionService.ingest`.
   - Implement reconcilers for `get_order`, `get_positions`, and `get_margin` so the heartbeat and risk layer remain consistent.

5. **Register with the factory**
   - Update `connector/factory.py` with the new venue slug, key-file discovery, and any environment flags.
   - Extend unit/integration tests with connector-specific stubs or replay fixtures.

6. **Document operational notes**
   - Add credential/permission requirements and known edge cases to this document or `docs/STRATEGY_GUIDE.md`.

Following this flow keeps the execution stack agnostic of venue quirks while preserving the mandated single-source tracking-limit implementation.
