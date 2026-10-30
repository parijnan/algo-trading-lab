"""
Parameters of the MCX option quote recorder (plans/mcx-option-hedging.md, Tier C). Every number the recorder uses lives here.

The recorder listens to the Fyers data websocket and makes no REST data requests of its own, so it cannot use Hestia's REST budget (Hestia's
candle source runs Fyers first). The one exception is inside the Fyers SDK: each subscribe() makes one POST to /data/symbol-token to
resolve symbols, so the recorder subscribes twice per window and closes the connection at the window end instead of unsubscribing
(about 6 such calls a day, against 5,000 allowed on the Standard plan).
"""

from datetime import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# ---- what is recorded ------------------------------------------------------------------------------------------------------------
UNDERLYINGS = ('CRUDEOIL', 'CRUDEOILM', 'NATURALGAS', 'NATGASMINI', 'SILVERM', 'GOLDM')
EXPIRIES_PER_UNDERLYING = 2           # the nearest listed expiries that have not passed
BAND_PCT = 3.0                        # strikes within this percentage of the reference future, calls and puts
MAX_SYMBOLS = 96                      # futures plus options per connection; the Standard plan allows 100 symbols per user across connections
FUTURES_WAIT_S = 45                   # how long to wait for the reference futures' first prices before giving up on a window

# ---- when it is recorded (IST) ----------------------------------------------------------------------------------------------------
# (name, start offset from the session open in minutes, end offset)
OPEN_BUCKETS = (
    ('open_15', 0, 15),
    ('open_45', 15, 45),
)
CLOSE_BUCKET = ('close', time(22, 45))            # from this time to the session close

# ---- what is written -------------------------------------------------------------------------------------------------------------
HEARTBEAT_S = 60                      # a symbol whose quote has not changed is still written once in this many seconds
DRAIN_S = 1.0                         # how often received messages are filtered and written

# ---- the connection --------------------------------------------------------------------------------------------------------------
CONNECT_WAIT_S = 20
STALE_FEED_S = 30                     # no message at all for this long inside a window: warn
RESTART_AFTER_S = 90                  # no message for this long: close and open the connection again
MAX_RESTARTS_PER_WINDOW = 3
TOKEN_RECHECK_S = 30
SDK_RECONNECT_RETRY = 5

# ---- files (everything the recorder writes is under OUT_DIR and LOG_DIR) --------------------------------------------------------
OUT_DIR = ROOT / 'data_pipeline' / 'data' / 'mcx_option_quotes'      # gitignored with the rest of data_pipeline/data
LOG_DIR = ROOT / 'logs'
SDK_LOG_DIR = ROOT / 'logs' / 'fyers_sdk'
TOKEN_FILE = ROOT / 'hestia_data' / 'fyers_token.json'                # read only
HOLIDAYS_FILE = ROOT / 'data_pipeline' / 'data' / 'mcx_holidays.csv'
MASTER_URL = 'https://public.fyers.in/sym_details/MCX_COM_sym_master.json'
MASTER_FALLBACK = ROOT / 'data_pipeline' / 'data' / 'mcx_com_sym_master.json'
OFF_FLAG_NAME = 'option_recorder_off.flag'                            # in OUT_DIR; present means idle
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
