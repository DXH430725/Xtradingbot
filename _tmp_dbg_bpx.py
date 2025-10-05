import asyncio
from pathlib import Path
from xbot.connector.backpack import BackpackConnector

async def main():
    c = BackpackConnector(key_path=Path('Backpack_key.txt'))
    await c.start()
    dec = await c.get_price_size_decimals('SOL_USDC_PERP')
    min_size = await c.get_min_size_i('SOL_USDC_PERP')
    bidask = await c.get_top_of_book('SOL_USDC_PERP')
    print('decimals', dec, 'min_size_i', min_size)
    print('top_of_book', bidask)
    # direct SDK calls
    book = await c._public.get_depth('SOL_USDC_PERP')
    print('raw_depth_sample', book.get('bids', [])[:2], book.get('asks', [])[:2])
    ticker = await c._public.get_ticker('SOL_USDC_PERP')
    print('ticker', ticker)
    marks = await c._public.get_all_mark_prices('SOL_USDC_PERP')
    print('mark_prices', marks)
    await c.stop()

asyncio.run(main())
