"""
Step 1: fetch the full paired futures/options expiry calendar for
CRUDEOILM, HISTORY_START -> yesterday, and save it. One row per futures
expiry cycle, with its paired options expiry (positional pairing --
verified below that both lists come back the same length and chronologically
sorted, so index i of one pairs with index i of the other; the gap size is
printed for every row as a direct sanity check rather than trusting the
pairing blindly).
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'data_pipeline'))

import configs_iv as cfg
from fyers_options_api import get_expiry_dates_paired
from data_downloader_fyers_mcx import resolve_anchor_symbol

anchor = resolve_anchor_symbol(cfg.INSTRUMENT)
today = datetime.now().date()
range_to = (today - timedelta(days=1)).strftime('%Y-%m-%d')
print(f'Anchor: {anchor}, range {cfg.HISTORY_START} -> {range_to}')

futures, options = get_expiry_dates_paired(anchor, cfg.HISTORY_START, range_to)
print(f'{len(futures)} futures expiries, {len(options)} options expiries')

if len(futures) != len(options):
    print(f'WARNING: count mismatch ({len(futures)} vs {len(options)}) -- positional pairing '
          f'is not safe as-is, needs manual inspection before proceeding.')

# Pair by actual date, not list position/index -- confirmed necessary: the
# two lists don't share a common start date (options history starts much
# later than futures history), so naive zip() silently mispairs early
# futures cycles against late options cycles. Each options expiry pairs
# with the SMALLEST futures expiry on or after it (the future it can
# devolve into).
fut_ts = sorted(pd.Timestamp(f) for f in futures)
rows = []
for o in options:
    o_ts = pd.Timestamp(o)
    matching_fut = min((f for f in fut_ts if f >= o_ts), default=None)
    if matching_fut is None:
        print(f'  {o}  ->  NO FUTURES MATCH FOUND  <-- SUSPICIOUS')
        continue
    gap_days = (matching_fut - o_ts).days
    rows.append({'futures_expiry': matching_fut.strftime('%Y-%m-%d'), 'options_expiry': o,
                 'gap_days': gap_days})
    flag = '' if 0 <= gap_days <= 7 else '  <-- SUSPICIOUS GAP'
    print(f'  {o}  ->  {matching_fut.date()}   (gap {gap_days}d){flag}')

Path(cfg.DATA_DIR).mkdir(parents=True, exist_ok=True)
pd.DataFrame(rows).to_csv(cfg.EXPIRY_CALENDAR_FILE, index=False)
print(f'\nSaved -> {cfg.EXPIRY_CALENDAR_FILE} ({len(rows)} paired cycles)')

paired_futures = {r['futures_expiry'] for r in rows}
unpaired = sorted(set(futures) - paired_futures)
if unpaired:
    print(f'\n{len(unpaired)} futures expiries with NO options counterpart at all '
          f'(options history does not reach this far back): {unpaired}')
