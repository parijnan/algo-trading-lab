"""
configs.py — Artemis Production Configuration
All parameters loaded from data/ files in this directory.

Change from original: chdir removed entirely.
The wrapper sets os.chdir(ARTEMIS_DIR) before importing Artemis modules,
so all relative paths (data/contracts.csv etc.) resolve correctly without
any chdir inside the module.
"""

import pandas as pd
from datetime import time

# Session hours
opening_time = time(9, 15)
closing_time  = time(15, 30)

# Scrip master URL — same as before
scrip_master_url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# Load contracts list, holidays list and trade settings
# Paths are relative — wrapper sets cwd to artemis_production/ before import
contracts_df      = pd.read_csv('data/contracts.csv',     parse_dates=['expiry', 'entry', 'elm_time', 'cutoff_time'])
holidays_df       = pd.read_csv('data/holidays.csv',      parse_dates=['date'])
holidays_df['date'] = pd.to_datetime(holidays_df['date']).apply(lambda x: x.date())
trade_settings_df = pd.read_csv('data/trade_settings.csv')

# Credentials — loaded from data/user_credentials.csv
# data/user_credentials.csv is a symlink to ../../data/user_credentials.csv (shared)
user_credentials_df = pd.read_csv('data/user_credentials.csv')
api_key     = user_credentials_df.iloc[0].loc['api_key']
user_name   = user_credentials_df.iloc[0].loc['user_name']
password    = str(user_credentials_df.iloc[0].loc['password'])
slack_token = user_credentials_df.iloc[0].loc['slack_token']
bot_token   = user_credentials_df.iloc[0].loc['bot_token']
bot_id      = str(user_credentials_df.iloc[0].loc['bot_id'])
channel_id  = user_credentials_df.iloc[0].loc['channel_id']
qr_code     = user_credentials_df.iloc[0].loc['qr_code']

# Trade settings
qty_freeze               = trade_settings_df.iloc[0].loc['qty_freeze']
lot_size                 = trade_settings_df.iloc[0].loc['lot_size']
lot_count                = trade_settings_df.iloc[0].loc['lot_count']
lot_capital              = trade_settings_df.iloc[0].loc['lot_capital']
lot_calc                 = trade_settings_df.iloc[0].loc['lot_calc']
expected_option_premium  = trade_settings_df.iloc[0].loc['expected_premium']
strike_values_iterator   = trade_settings_df.iloc[0].loc['strike_iterator']
monitor_frequency        = trade_settings_df.iloc[0].loc['monitor_frequency']
order_limit              = trade_settings_df.iloc[0].loc['order_limit']
poll_limit               = trade_settings_df.iloc[0].loc['poll_limit']
strike_iteration_interval = trade_settings_df.iloc[0].loc['strike_iteration_interval']
hedge_points             = trade_settings_df.iloc[0].loc['hedge_points']
sl_4_dte                 = trade_settings_df.iloc[0].loc['sl_4_dte']
sl_3_dte                 = trade_settings_df.iloc[0].loc['sl_3_dte']
sl_2_dte                 = trade_settings_df.iloc[0].loc['sl_2_dte']
sl_1_dte                 = trade_settings_df.iloc[0].loc['sl_1_dte']
sl_0_dte                 = trade_settings_df.iloc[0].loc['sl_0_dte']
adjustment_distance      = trade_settings_df.iloc[0].loc['adj_dist']
instrument               = trade_settings_df.iloc[0].loc['instrument']
underlying_token         = str(trade_settings_df.iloc[0].loc['underlying_token'])
exchange_segment         = trade_settings_df.iloc[0].loc['exch_seg']
fo_exchange_segment      = trade_settings_df.iloc[0].loc['fo_exch_seg']
minimum_gap              = trade_settings_df.iloc[0].loc['min_gap']
minimum_gap_iterator     = trade_settings_df.iloc[0].loc['min_gap_iterator']
index_sl_offset          = trade_settings_df.iloc[0].loc['index_sl_offset']
vix_threshold            = trade_settings_df.iloc[0].loc['vix_threshold']
entry_window_minutes     = int(trade_settings_df.iloc[0].loc['entry_window_minutes'])

poll_counter  = 0
order_counter = 0