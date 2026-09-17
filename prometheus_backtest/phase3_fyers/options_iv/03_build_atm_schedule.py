"""
Step 3: build the 15-min ATM-strike schedule.

For every 15-min boundary in CRUDEOILM's own continuous front-month price
series (the SAME series -- same contract_expiry-per-row, same Fyers+
AngelOne-gap-fill splice -- that produced the 2,476-trade bespoke_trade_
summary.csv this IV series is meant to correlate against, via
data_loader_fyers.load_futures_1min): resolve which options cycle pairs
with that boundary's own held futures contract (expiry_calendar.csv), then
pick the REAL listed strike nearest to that boundary's own resampled
futures price from that cycle's strike ladder (cached by step 2) -- not a
computed/assumed strike.

Boundaries whose futures contract has no options cycle behind it (the 14
pre-2024-06-14 cycles, see step 1's own finding) are kept in the output
with atm_strike=NaN, not dropped -- so downstream steps can report exactly
which trades/boundaries have no IV rather than silently shrinking the
dataset.
"""
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # phase3_fyers, for data_loader_fyers

import configs_iv as cfg
from resample_utils import resample_1m_to_Nmin_historical
import data_loader_fyers

STRIKE_RE = re.compile(r'MCX:[A-Z]+\d{2}[A-Z]{3}(\d+)(CE|PE)$')


def parse_strike(symbol: str) -> int:
    m = STRIKE_RE.search(symbol)
    if not m:
        raise ValueError(f'Could not parse strike from {symbol!r}')
    return int(m.group(1))


print('Loading CRUDEOILM 1-min front-month series (same source as the bespoke trade set)...')
df_1m = data_loader_fyers.load_futures_1min('CRUDEOILM').reset_index()
print(f'{len(df_1m):,} 1-min rows, {df_1m["time_stamp"].min()} -> {df_1m["time_stamp"].max()}')

print(f'Resampling to {cfg.GRANULARITY_MINUTES}-min (DST-safe, per-day resolved)...')
df_15m = resample_1m_to_Nmin_historical(
    df_1m[['time_stamp', 'open', 'high', 'low', 'close', 'volume', 'contract_expiry', 'data_source']],
    minutes=cfg.GRANULARITY_MINUTES,
)
print(f'{len(df_15m):,} boundaries.')

calendar = pd.read_csv(cfg.EXPIRY_CALENDAR_FILE, dtype=str)
fut_to_opt = dict(zip(calendar['futures_expiry'], calendar['options_expiry']))

# contract_expiry in df_1m/df_15m is the raw string from the futures filename
# (e.g. '2024-06-18'), already in 'YYYY-MM-DD' form -- matches the calendar's
# own futures_expiry column format directly.
df_15m['options_expiry'] = df_15m['contract_expiry'].map(fut_to_opt)

no_options = df_15m['options_expiry'].isna()
print(f'{no_options.sum():,} / {len(df_15m):,} boundaries have no options cycle available '
      f'({df_15m.loc[no_options, "time_stamp"].min()} -> {df_15m.loc[no_options, "time_stamp"].max()}).')

# Load all strike ladders once, keyed by options_expiry
ladders = {}
for f in Path(cfg.STRIKE_LADDERS_DIR).glob('*.json'):
    import json
    d = json.loads(f.read_text())
    ce_strikes = sorted({parse_strike(s) for s in d['ce']})
    pe_strikes = sorted({parse_strike(s) for s in d['pe']})
    common = sorted(set(ce_strikes) & set(pe_strikes))   # need BOTH legs for a straddle
    ladders[d['options_expiry']] = {
        'strikes': common,
        'ce_by_strike': {parse_strike(s): s for s in d['ce']},
        'pe_by_strike': {parse_strike(s): s for s in d['pe']},
    }


def nearest_strike(options_expiry, price):
    if pd.isna(options_expiry) or options_expiry not in ladders:
        return None
    strikes = ladders[options_expiry]['strikes']
    if not strikes:
        return None
    return min(strikes, key=lambda k: abs(k - price))


print('Matching nearest real listed strike per boundary...')
df_15m['atm_strike'] = df_15m.apply(
    lambda r: nearest_strike(r['options_expiry'], r['close']), axis=1)

matched = df_15m['atm_strike'].notna()
print(f'{matched.sum():,} / {len(df_15m):,} boundaries matched to a real strike.')


def ce_symbol(r):
    if pd.isna(r['atm_strike']):
        return None
    return ladders[r['options_expiry']]['ce_by_strike'].get(int(r['atm_strike']))


def pe_symbol(r):
    if pd.isna(r['atm_strike']):
        return None
    return ladders[r['options_expiry']]['pe_by_strike'].get(int(r['atm_strike']))


df_15m['ce_symbol'] = df_15m.apply(ce_symbol, axis=1)
df_15m['pe_symbol'] = df_15m.apply(pe_symbol, axis=1)

df_15m.to_csv(cfg.ATM_SCHEDULE_FILE, index=False)
print(f'\nSaved full schedule -> {cfg.ATM_SCHEDULE_FILE}')

# Unique contract list needed for download
needed = pd.concat([
    df_15m.loc[matched, ['options_expiry', 'ce_symbol']].rename(columns={'ce_symbol': 'symbol'}),
    df_15m.loc[matched, ['options_expiry', 'pe_symbol']].rename(columns={'pe_symbol': 'symbol'}),
]).drop_duplicates().sort_values(['options_expiry', 'symbol'])
needed_path = Path(cfg.DATA_DIR) / 'needed_contracts.csv'
needed.to_csv(needed_path, index=False)
print(f'{len(needed)} unique option contracts needed for download -> {needed_path}')
print(f'\nStrike drift check (contracts needed per cycle):')
print(needed.groupby('options_expiry').size().to_string())
