"""
Phase 0b of plans/hestia-fyers-candle-source.md: do Fyers-sourced and Angel One-sourced 15-minute bars give the engines the SAME Supertrend
flips? For each instrument's front contract, over the last ~60 days: build 15-minute bars from both sources with Hestia's own resampler,
run the engine's own Supertrend (period and multiplier from its config), and compare flip bars. The first 10 days are skipped (Supertrend
is path dependent, so the two series need a little time to settle onto the same state). Read-only, Fyers History calls only.

    python research/fyers_mcx_validation/phase0_flip_agreement.py
"""
import sys
from pathlib import Path
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parents[1]))
import importlib
from datetime import timedelta
import pandas as pd
import phase0_hestia_candles as p0
from hestia_core.indicators import compute_st

rec = p0.ftr.check_token_file(p0.ftr.DEFAULT_TOKEN_FILE)[1]
auth = f"{rec['app_id']}:{rec['access_token']}"
DAYS = 60
master = pd.read_csv(p0.MASTER); master['exp'] = pd.to_datetime(master['expiry'], format='%d%b%Y')
today = pd.Timestamp.now().normalize()
tot = {'flips_a': 0, 'same': 0, 'off1': 0, 'miss': 0}
for engine, name in p0.ENGINES.items():
    cfg = importlib.import_module(f'{engine}_engine.engine_configs').DEFAULT
    c = master[(master['name'] == name) & (master['exp'] >= today)].sort_values('exp').iloc[0]
    path = p0.PIPELINE / name / f'{c.exp:%Y-%m-%d}_futures.csv'
    last = pd.to_datetime(pd.read_csv(path, usecols=['time_stamp'])['time_stamp'].iloc[-1][:19])
    end = last.normalize() + timedelta(hours=23, minutes=35)
    start = end - timedelta(days=DAYS)
    f, msg = p0.fyers_minutes(p0.fyers_symbol(name, c.exp), start, end, auth)
    a = p0.angel_minutes(path, start, end)
    lo, hi = max(f.time_stamp.min(), a.time_stamp.min()), min(f.time_stamp.max(), a.time_stamp.max())
    f, a = f[(f.time_stamp >= lo) & (f.time_stamp <= hi)], a[(a.time_stamp >= lo) & (a.time_stamp <= hi)]
    now = hi + timedelta(minutes=1)
    fs, as_ = compute_st(p0.bars(f, now), cfg.st_period, cfg.st_multiplier), compute_st(p0.bars(a, now), cfg.st_period, cfg.st_multiplier)
    # skip the first ~10 days: Supertrend warm-up and trend-state divergence settle (state is path dependent)
    cut = lo + timedelta(days=10)
    fl_f = list(fs[(fs.trend_flip) & (fs.time_stamp >= cut)].time_stamp)
    fl_a = list(as_[(as_.trend_flip) & (as_.time_stamp >= cut)].time_stamp)
    fa_set, ff_set = set(fl_a), set(fl_f)
    same = sum(1 for t in fl_a if t in ff_set)
    off1 = sum(1 for t in fl_a if t not in ff_set and any(abs((t - u).total_seconds()) <= 900 for u in fl_f))
    miss = len(fl_a) - same - off1
    extra_f = sum(1 for u in fl_f if u not in fa_set and not any(abs((u - t).total_seconds()) <= 900 for t in fl_a))
    # direction agreement overall
    j = fs.merge(as_, on='time_stamp', suffixes=('_f', '_a')); j = j[j.time_stamp >= cut].dropna(subset=['supertrend_f', 'supertrend_a'])
    print(f'{engine:10s} {c.symbol:22s} {lo:%m-%d}..{hi:%m-%d}  bars {len(j):5d}  flips(angel) {len(fl_a):3d} flips(fyers) {len(fl_f):3d}  '
          f'same bar {same:3d}  1 bar off {off1:3d}  angel-only {miss:3d}  fyers-only {extra_f:3d}  trend differs on {int((j.trend_f != j.trend_a).sum())} bars')
    tot['flips_a'] += len(fl_a); tot['same'] += same; tot['off1'] += off1; tot['miss'] += miss
print(tot)
