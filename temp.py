# quick_check_bp_market.py
import asyncio, os, sys
sys.path.append(os.getcwd())
from mm_bot.runtime.builders import build_backpack_connector

async def main():
      cfg = {"base_url": "https://api.backpack.exchange",
             "ws_url": "wss://ws.backpack.exchange",
             "keys_file": "Backpack_key.txt"}
      conn = build_backpack_connector(cfg, {}, False)
      conn.start()
      await conn.start_ws_state()
      await conn._ensure_markets()
      try:
          size_i = 0.0001
          tracker = await conn.submit_market_order(
              symbol="ETH_USDC_PERP",
              client_order_index=123456,
              base_amount=size_i,
              is_ask=False,
              reduce_only=0,
          )
          await tracker.wait_final(timeout=30)
          print("state:", tracker.state)
          print("positions:", await conn.get_positions())
      finally:
          await conn.close()

asyncio.run(main())