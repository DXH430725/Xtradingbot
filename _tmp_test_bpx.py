import asyncio
from pathlib import Path
from xbot.connector.backpack import BackpackConnector

async def main():
    c = BackpackConnector(key_path=Path('Backpack_key.txt'))
    await c.start()
    dec = await c.get_price_size_decimals('SOL_USDC')
    tob = await c.get_top_of_book('SOL_USDC')
    print('decimals', dec)
    print('best_bid_ask', tob[:2])
    await c.stop()

asyncio.run(main())
