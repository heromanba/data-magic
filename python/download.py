import datetime

from binance_historical_data import BinanceDataDumper

data_dumper = BinanceDataDumper(
    path_dir_where_to_dump=".", # specify your download folder here
    asset_class="spot", # options: spot, um (USD-M Futures), cm (COIN-M Futures)
    data_type="klines", # options: aggTrades, klines, trades
    data_frequency="1m" # frequency like 1m, 1h, 1d, etc.
)

# Download all available tickers for the specified date range
data_dumper.dump_data(
    tickers=None, 
    date_start=datetime.date(2026, 1, 1), 
    date_end=datetime.date(2026, 6, 1), 
    is_to_update_existing=False
)
