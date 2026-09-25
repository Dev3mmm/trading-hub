"""Data source switch. Local PC: Binance USDT-M futures (default). Hosted on GitHub Actions (HOSTED=1): Binance spot public mirror,
because futures/api.binance.com return 451 from US cloud IPs. Same kline shape either way."""
import os
HOSTED = os.environ.get("HOSTED") == "1"
SPOT = "https://data-api.binance.vision/api/v3"
FUT = "https://fapi.binance.com/fapi/v1"
API = SPOT if HOSTED else FUT
