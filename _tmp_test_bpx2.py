import asyncio
from pathlib import Path
from xbot.connector.backpack import BackpackConnector

async def main():
    c = BackpackConnector(key_path=Path('Backpack_key.txt'))
    await c.start()
    book = await c._public.get_depth('SOL_USDC')
    print(book['bids'][:1], book['asks'][:1])
    await c.stop()

asyncio.run(main())
