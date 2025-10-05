import asyncio
from pathlib import Path
from xbot.connector.backpack import BackpackConnector

async def main():
    c = BackpackConnector(key_path=Path('Backpack_key.txt'))
    await c.start()
    bid, ask, scale = await c.get_top_of_book('SOL_USDC_PERP')
    print('best_i', bid, ask, 'scale', scale)
    await c.stop()

asyncio.run(main())
