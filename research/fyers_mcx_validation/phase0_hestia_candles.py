"""
Phase 0 of plans/hestia-fyers-candle-source.md: are Fyers's one-minute candles a safe substitute for Angel One's in Hestia?

For each instrument Hestia trades, take its front and next live contract, fetch ~3 weeks of one-minute candles from Fyers's regular
History API, and compare them with the Angel One files the data pipeline already holds (`data_pipeline/data/mcx/<INSTR>/`):

  1. symbol resolution: does the constructed Fyers symbol (MCX:<UNDERLYING><YY><MON>FUT) answer?
  2. minute level: minutes only one source has (zero-volume placeholders?), OHLC agreement, volume agreement
  3. the thing that matters: build 15-minute bars with Hestia's own resampler and run the engine's own Supertrend (period and
     multiplier from the engine config) on both series, then compare trend and flips bar by bar

Read-only: Fyers History calls only (no Angel One login, no Delos, nothing written but a results CSV). The token is read from
hestia_data/fyers_token.json and never printed.

    python research/fyers_mcx_validation/phase0_hestia_candles.py [--days 21]
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'data_pipeline'))
import fyers_token_refresh as ftr                                            # noqa: E402
from hestia_core.history import resample_1m                                  # noqa: E402
from hestia_core.indicators import compute_st                                # noqa: E402
from hestia_core.mcx_market import closing_time_str                          # noqa: E402

ENGINES = {'prometheus': 'CRUDEOILM', 'selene': 'SILVERMIC', 'helios': 'GOLDPETAL', 'typhon': 'NATGASMINI'}
MASTER = REPO / 'data_pipeline' / 'data' / 'mcx_instrument_master.csv'
PIPELINE = REPO / 'data_pipeline' / 'data' / 'mcx'
OUT = Path(__file__).parent / 'phase0_results.csv'
IST = ftr.IST


def fyers_symbol(name: str, expiry: pd.Timestamp) -> str:
    return f'MCX:{name}{expiry:%y%b}FUT'.upper()


def fyers_minutes(symbol: str, start: datetime, end: datetime, auth: str) -> tuple:
    """(frame or None, message). Fyers epochs are UTC seconds; the frame is tz-naive IST like every series in Hestia."""
    body = ftr._get(ftr.HISTORY_URL, {'symbol': symbol, 'resolution': '1', 'date_format': '0', 'cont_flag': '0',
                                      'range_from': int(start.replace(tzinfo=IST).timestamp()),
                                      'range_to': int(end.replace(tzinfo=IST).timestamp())}, auth)
    if body.get('s') != 'ok':
        return None, f"{body.get('s')} code={body.get('code')} {body.get('message')}"
    df = pd.DataFrame(body.get('candles', []), columns=['epoch', 'open', 'high', 'low', 'close', 'volume'])
    df['time_stamp'] = pd.to_datetime(df['epoch'], unit='s', utc=True).dt.tz_convert(IST).dt.tz_localize(None)
    return df[['time_stamp', 'open', 'high', 'low', 'close', 'volume']].drop_duplicates('time_stamp').sort_values('time_stamp'), 'ok'


def angel_minutes(path: Path, start: datetime, end: datetime) -> pd.DataFrame:
    df = pd.read_csv(path)
    df['time_stamp'] = pd.to_datetime(df['time_stamp'].str.slice(0, 19))          # '2026-10-01T23:29:00+05:30' -> naive IST
    return df[(df['time_stamp'] >= start) & (df['time_stamp'] <= end)].drop_duplicates('time_stamp').sort_values('time_stamp')


def minute_stats(f: pd.DataFrame, a: pd.DataFrame) -> dict:
    m = f.merge(a, on='time_stamp', how='outer', suffixes=('_f', '_a'), indicator=True)
    both = m[m['_merge'] == 'both']
    only_f, only_a = m[m['_merge'] == 'left_only'], m[m['_merge'] == 'right_only']
    exact = (both['open_f'] == both['open_a']) & (both['high_f'] == both['high_a']) & (both['low_f'] == both['low_a']) \
        & (both['close_f'] == both['close_a'])
    return {'minutes_both': len(both), 'minutes_fyers_only': len(only_f),
            'fyers_only_zero_volume': int((only_f['volume_f'] == 0).sum()), 'minutes_angel_only': len(only_a),
            'ohlc_exact_pct': round(100 * exact.mean(), 2) if len(both) else None,
            'close_max_abs_diff': float((both['close_f'] - both['close_a']).abs().max()) if len(both) else None,
            'volume_exact_pct': round(100 * (both['volume_f'] == both['volume_a']).mean(), 2) if len(both) else None}


def bars(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    return resample_1m(df, 15, now, '09:00', closing_time_str)


def st_stats(fb: pd.DataFrame, ab: pd.DataFrame, period: int, mult: float) -> dict:
    fs, as_ = compute_st(fb, period, mult), compute_st(ab, period, mult)
    j = fs.merge(as_, on='time_stamp', suffixes=('_f', '_a')).dropna(subset=['supertrend_f', 'supertrend_a'])
    cols = j.columns
    tf, ta = ('trend_f', 'trend_a') if 'trend_f' in cols else (None, None)
    out = {'bars_compared': len(j)}
    if tf is None:
        return out
    out['trend_mismatch_bars'] = int((j[tf] != j[ta]).sum())
    ff, fa = ('trend_flip_f', 'trend_flip_a') if 'trend_flip_f' in cols else (None, None)
    if ff:
        out['flip_mismatch_bars'] = int((j[ff] != j[fa]).sum())
        out['flips_fyers'], out['flips_angel'] = int(j[ff].sum()), int(j[fa].sum())
    sv = [c for c in cols if c.startswith('st') and c.endswith('_f')]
    if sv:
        base = sv[0][:-2]
        out['st_max_abs_diff'] = float((j[base + '_f'] - j[base + '_a']).abs().max())
    out['close_max_abs_diff_15m'] = float((j['close_f'] - j['close_a']).abs().max())
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=21)
    args = ap.parse_args()
    problems, rec = ftr.check_token_file(ftr.DEFAULT_TOKEN_FILE)
    if rec is None or problems:
        print('token file not usable:', problems)
        return 1
    auth = f"{rec['app_id']}:{rec['access_token']}"
    master = pd.read_csv(MASTER)
    master['exp'] = pd.to_datetime(master['expiry'], format='%d%b%Y')
    today = pd.Timestamp.now().normalize()
    rows = []
    for engine, name in ENGINES.items():
        cfg = importlib.import_module(f'{engine}_engine.engine_configs').DEFAULT
        live = master[(master['name'] == name) & (master['exp'] >= today)].sort_values('exp').head(2)
        for _, c in live.iterrows():
            expiry = c['exp']
            sym, ang_sym = fyers_symbol(name, expiry), c['symbol']
            path = PIPELINE / name / f'{expiry:%Y-%m-%d}_futures.csv'
            row = {'engine': engine, 'contract': ang_sym, 'fyers_symbol': sym, 'st': f'({cfg.st_period}, {cfg.st_multiplier})'}
            if not path.exists():
                rows.append({**row, 'note': 'no pipeline file'})
                continue
            full = pd.read_csv(path, usecols=['time_stamp'])
            last = pd.to_datetime(full['time_stamp'].iloc[-1][:19])
            end = last.normalize() + timedelta(hours=23, minutes=35)
            start = end - timedelta(days=args.days)
            f, msg = fyers_minutes(sym, start, end, auth)
            time.sleep(0.3)
            if f is None:
                rows.append({**row, 'note': f'fyers: {msg}'})
                continue
            a = angel_minutes(path, start, end)
            lo, hi = max(f['time_stamp'].min(), a['time_stamp'].min()), min(f['time_stamp'].max(), a['time_stamp'].max())
            f, a = f[(f['time_stamp'] >= lo) & (f['time_stamp'] <= hi)], a[(a['time_stamp'] >= lo) & (a['time_stamp'] <= hi)]
            row.update(window=f'{lo:%m-%d %H:%M} .. {hi:%m-%d %H:%M}', fyers_rows=len(f), angel_rows=len(a), **minute_stats(f, a))
            fb, ab = bars(f, hi + timedelta(minutes=1)), bars(a, hi + timedelta(minutes=1))
            if len(fb) > 3 and len(ab) > 3:
                row.update(st_stats(fb, ab, cfg.st_period, cfg.st_multiplier))
            rows.append(row)
    df = pd.DataFrame(rows)
    df.to_csv(OUT, index=False)
    with pd.option_context('display.width', 250, 'display.max_columns', 40, 'display.max_colwidth', 40):
        print(df.drop(columns=['window'], errors='ignore').to_string(index=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
