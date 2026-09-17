"""
Step 2: for each of the 27 paired options cycles, fetch the full CE/PE
strike-symbol ladder (Get Expired Contracts) and cache it as JSON. Cheap --
27 API calls total, one per cycle -- and needed before the ATM schedule can
be built, since we match against REAL listed strikes, not an assumed fixed
spacing.
"""
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'data_pipeline'))

import configs_iv as cfg
from fyers_options_api import get_expired_option_contracts
from data_downloader_fyers_mcx import resolve_anchor_symbol

anchor = resolve_anchor_symbol(cfg.INSTRUMENT)
calendar = pd.read_csv(cfg.EXPIRY_CALENDAR_FILE)

Path(cfg.STRIKE_LADDERS_DIR).mkdir(parents=True, exist_ok=True)

for _, row in calendar.iterrows():
    opt_exp = row['options_expiry']
    out_path = Path(cfg.STRIKE_LADDERS_DIR) / f'{opt_exp}.json'
    if out_path.exists():
        print(f'{opt_exp}: cached, skipping')
        continue
    symbols = get_expired_option_contracts(anchor, opt_exp)
    ce = sorted(s for s in symbols if s.endswith('CE'))
    pe = sorted(s for s in symbols if s.endswith('PE'))
    print(f'{opt_exp}: {len(ce)} CE, {len(pe)} PE')
    out_path.write_text(json.dumps({'options_expiry': opt_exp, 'ce': ce, 'pe': pe}))

print(f'\nStrike ladders cached -> {cfg.STRIKE_LADDERS_DIR}')
