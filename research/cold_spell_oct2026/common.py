"""Shared loaders for the Oct 2026 cold-spell research."""
import glob, os
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'data_pipeline' / 'data'
OUT = Path(__file__).resolve().parent / 'outputs'
TRADES = ROOT / 'hestia_backtest' / 'outputs' / 'integrated_trades.csv'
INSTRUMENTS = {'Prometheus': 'CRUDEOILM', 'Selene': 'SILVERMIC', 'Helios': 'GOLDPETAL', 'Typhon': 'NATGASMINI'}


def load_trades() -> pd.DataFrame:
    t = pd.read_csv(TRADES, parse_dates=['entry_ts', 'exit_ts'])
    t['exit_day'] = t.exit_ts.dt.normalize()
    t['entry_day'] = t.entry_ts.dt.normalize()
    return t


def daily_returns(instrument: str) -> pd.Series:
    """Each session's close-to-close move inside ONE contract (the busiest that day), so rolls make no jumps."""
    frames = []
    for p in glob.glob(str(DATA / 'mcx_fyers' / instrument / '*_futures.csv')) + glob.glob(str(DATA / 'mcx' / instrument / '*_futures.csv')):
        d = pd.read_csv(p, usecols=['time_stamp', 'close', 'volume'])
        d['c'] = os.path.basename(p)[:10]
        frames.append(d)
    d = pd.concat(frames)
    d['ts'] = pd.to_datetime(d.time_stamp.str[:19], format='ISO8601')
    d['day'] = d.ts.dt.normalize()
    g = d.sort_values('ts').groupby(['c', 'day']).agg(close=('close', 'last'), vol=('volume', 'sum')).reset_index()
    g['ret'] = g.groupby('c').close.diff()
    g = g.dropna(subset=['ret']).sort_values('vol').groupby('day').tail(1).set_index('day').sort_index()
    return g.ret


def last_close(instrument: str) -> float:
    """Last close of the busiest contract on the latest day in the live tree (used only to anchor a rolled-up level)."""
    best = None
    for f in glob.glob(str(DATA / 'mcx' / instrument / '*_futures.csv')):
        d = pd.read_csv(f, usecols=['time_stamp', 'close', 'volume'])
        d['day'] = pd.to_datetime(d.time_stamp.str[:19], format='ISO8601').dt.normalize()
        day = d.day.max(); x = d[d.day == day]
        key = (day, x.volume.sum())
        if best is None or key > best[0]: best = (key, float(x.close.iloc[-1]))
    return best[1]
