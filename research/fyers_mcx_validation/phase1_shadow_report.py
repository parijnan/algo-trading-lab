"""
Phase 1 report for plans/hestia-fyers-candle-source.md: what the Fyers shadow recorded in one session (hestia_data/shadow/), judged on the
two questions the later phases depend on.

  1. Availability: after each minute tick, how soon did each source have the just-closed minute, and where Angel One exhausted its retries
     (the user's reason for all of this: AB1021 and slow flip detection), did Fyers have it? Boundary minutes (xx:00/15/30/45) are reported
     separately because those are the minutes that decide flips.
  2. Flips: do Fyers-built 15-minute bars give the Supertrend the engines ACTUALLY ACTED ON? The reference is Hestia's own log (the
     `<symbol>: 15m boundary HH:MM close= ST= trend= flip=` lines), not the pipeline files (those come from a later historical fetch). The
     Fyers series is ~20 days of Fyers history for the seed plus the day's shadow minutes (what a live Fyers poll really returned).

    python research/fyers_mcx_validation/phase1_shadow_report.py --date 2026-10-06 [--log logs/hestia_20261006.log] [--no-seed]

Run it where the files are (Delos); reads only, writes nothing but stdout. `--no-seed` skips the Fyers history fetch (the flip section is then
skipped).
"""

from __future__ import annotations

import argparse
import importlib
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

BOUNDARY_RE = re.compile(r'^(\d{4}-\d{2}-\d{2}) [\d:,]+ INFO hestia_live_data: (\S+): 15m boundary (\d{2}:\d{2}) '
                         r'close=(\S+) ST=(\S+) trend=(\w+) flip=(True|False)')
ENGINES = {'prometheus': 'CRUDEOILM', 'selene': 'SILVERMIC', 'helios': 'GOLDPETAL', 'typhon': 'NATGASMINI'}


def parse_boundaries(text: str) -> pd.DataFrame:
    """Hestia's own record of every 15-minute boundary it computed: bar start (the HH:MM in the line), close, Supertrend, trend, flip."""
    rows = []
    for line in text.splitlines():
        m = BOUNDARY_RE.match(line)
        if not m:
            continue
        day, symbol, hhmm, close, st, trend, flip = m.groups()
        rows.append({'symbol': symbol, 'bar_start': pd.Timestamp(f'{day} {hhmm}'), 'close': float(close),
                     'st': float('nan') if st == 'None' else float(st), 'trend': trend, 'flip': flip == 'True'})
    return pd.DataFrame(rows, columns=['symbol', 'bar_start', 'close', 'st', 'trend', 'flip'])


def angel_to_fyers(symbol: str):
    """SILVERMIC30NOV26FUT -> MCX:SILVERMIC26NOVFUT (the same mapping as hestia_core.fyers_shadow.fyers_symbol); None for anything else."""
    m = re.match(r'^([A-Z]+)(\d{2})([A-Z]{3})(\d{2})FUT$', symbol)
    return None if not m else f'MCX:{m.group(1)}{m.group(4)}{m.group(3)}FUT'


def settled_minutes(df: pd.DataFrame) -> pd.DataFrame:
    """The recorder writes a minute again when a later poll shows different values, so a minute file holds each minute's history. The settled
    value is the last row per minute (by sighting time); the first-seen value is the first."""
    d = df.sort_values('seen_at', kind='stable')
    return d.drop_duplicates('time_stamp', keep='last').sort_values('time_stamp').reset_index(drop=True)


def provisional_stats(df: pd.DataFrame) -> dict:
    """How often was the first-seen value of a minute not the settled one? (Fyers's first answer, ~0.07 s after the minute closes, is provisional.)"""
    d = df.sort_values('seen_at', kind='stable')
    first, last = d.drop_duplicates('time_stamp', keep='first').set_index('time_stamp'), d.drop_duplicates('time_stamp', keep='last').set_index('time_stamp')
    cols = ['open', 'high', 'low', 'close', 'volume']
    changed = (first[cols] != last[cols]).any(axis=1)
    return {'minutes': len(last), 'changed_after_first_seen': int(changed.sum()), 'changed_pct': round(100 * float(changed.mean()), 1) if len(last) else None,
            'close_changed_pct': round(100 * float((first['close'] != last['close']).mean()), 1) if len(last) else None}


def pct(series: pd.Series, q: float):
    s = series.dropna()
    return None if s.empty else round(float(s.quantile(q)), 2)


def latency_summary(polls: pd.DataFrame) -> pd.DataFrame:
    """Per side, all minutes and boundary minutes: polls, success rate, and latency percentiles (seconds after the tick until the just-closed
    minute was present) over the polls where it was."""
    rows = []
    for side, g in polls.groupby('side'):
        for label, sub in (('all', g), ('boundary', g[g['boundary'] == 1])):
            if sub.empty:
                continue
            got = sub[sub['expected_present'] == 1]['after_s']
            rows.append({'side': side, 'minutes': label, 'polls': len(sub), 'got_minute_pct': round(100 * len(got) / len(sub), 1),
                         'p50_s': pct(got, .5), 'p90_s': pct(got, .9), 'p99_s': pct(got, .99), 'max_s': None if got.empty else round(float(got.max()), 2),
                         'mean_attempts': round(float(sub['attempts'].mean()), 2)})
    return pd.DataFrame(rows)


def head_to_head(polls: pd.DataFrame) -> dict:
    """Tick by tick: did Fyers rescue an Angel One exhaustion, how often was Fyers earlier, and where did Angel One have it and Fyers not."""
    key = ['tick', 'token']
    a = polls[polls['side'] == 'angel'].drop_duplicates(key, keep='last').set_index(key)
    f = polls[polls['side'] == 'fyers'].drop_duplicates(key, keep='last').set_index(key)
    j = a.join(f, how='inner', lsuffix='_a', rsuffix='_f')
    if j.empty:
        return {'ticks_compared': 0}
    a_miss = (j['expected_present_a'] == 0)
    f_got = (j['expected_present_f'] == 1)
    both = j[(j['expected_present_a'] == 1) & f_got]
    diff = (both['after_s_f'] - both['after_s_a'])
    return {'ticks_compared': len(j), 'angel_missing_minute': int(a_miss.sum()), 'angel_exhausted': int((j['exhausted_a'] == 1).sum()),
            'fyers_had_it_when_angel_did_not': int((a_miss & f_got).sum()),
            'fyers_missing_when_angel_had_it': int(((j['expected_present_a'] == 1) & ~f_got).sum()),
            'fyers_earlier_pct': None if both.empty else round(100 * float((diff < 0).mean()), 1),
            'fyers_minus_angel_s_p50': pct(diff, .5), 'fyers_minus_angel_s_p90': pct(diff, .9)}


def fyers_series(minutes_shadow: pd.DataFrame, history: pd.DataFrame, day: pd.Timestamp) -> pd.DataFrame:
    """~20 days of Fyers history before `day`, then the day's minutes as the shadow saw them live."""
    past = history[history['time_stamp'] < day]
    today = minutes_shadow[(minutes_shadow['time_stamp'] >= day) & (minutes_shadow['time_stamp'] < day + pd.Timedelta(days=1))]
    return pd.concat([past[['time_stamp', 'open', 'high', 'low', 'close', 'volume']],
                      today[['time_stamp', 'open', 'high', 'low', 'close', 'volume']]]).drop_duplicates('time_stamp').sort_values('time_stamp')


def compare_flips(fyers_1m: pd.DataFrame, boundaries: pd.DataFrame, period: int, mult: float, day: pd.Timestamp) -> dict:
    """Fyers-built bars through the engine's own Supertrend, against the boundary lines the engine acted on that day."""
    from hestia_core.history import resample_1m
    from hestia_core.indicators import compute_st
    from hestia_core.mcx_market import closing_time_str
    end = fyers_1m['time_stamp'].max() + timedelta(minutes=1)
    bars = resample_1m(fyers_1m, 15, end, '09:00', closing_time_str)
    st = compute_st(bars, period, mult).rename(columns={'time_stamp': 'bar_start', 'close': 'close_f', 'supertrend': 'st_f'})
    st['trend_f'] = st['trend'].map(lambda v: 'bullish' if v is True else ('bearish' if v is False else None))
    j = boundaries.merge(st[['bar_start', 'close_f', 'st_f', 'trend_f', 'trend_flip']], on='bar_start', how='inner').dropna(subset=['st_f'])
    j = j[j['bar_start'] >= day]
    if j.empty:
        return {'boundaries_compared': 0}
    bad = j[j['trend'].str.lower() != j['trend_f']]
    flips_e, flips_f = j[j['flip']]['bar_start'], j[j['trend_flip'].astype(bool)]['bar_start']
    return {'boundaries_compared': len(j), 'trend_mismatch': len(bad), 'engine_flips': len(flips_e), 'fyers_flips': len(flips_f),
            'flips_on_same_bar': len(set(flips_e) & set(flips_f)),
            'engine_flips_missing_in_fyers': sorted(str(t) for t in set(flips_e) - set(flips_f)),
            'fyers_flips_not_acted_on': sorted(str(t) for t in set(flips_f) - set(flips_e)),
            'close_max_abs_diff': round(float((j['close'] - j['close_f']).abs().max()), 4),
            'st_max_abs_diff': round(float((j['st'] - j['st_f']).abs().max()), 4)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', required=True)
    ap.add_argument('--shadow-dir', default=str(REPO / 'hestia_data' / 'shadow'))
    ap.add_argument('--log')
    ap.add_argument('--no-seed', action='store_true')
    ap.add_argument('--seed-days', type=int, default=20)
    args = ap.parse_args()
    d = args.date
    sd = Path(args.shadow_dir)
    pfile = sd / f'polls_{d}.csv'
    if not pfile.exists():
        print(f'no polls file for {d} in {sd}: shadow was not on, or the token was not usable all day')
        return 1
    polls = pd.read_csv(pfile)
    pd.set_option('display.width', 220)
    pd.set_option('display.max_columns', 30)
    print(f'== availability, {d} ==')
    print('(a Fyers after_s is the time until its FIRST answer holding the just-closed minute; that first answer is provisional, see "first-seen vs settled" below)')
    for symbol, g in polls.groupby('symbol'):
        print(f'\n{symbol}')
        print(latency_summary(g).to_string(index=False))
        print('  head to head:', head_to_head(g))
    if args.no_seed:
        return 0
    log_path = Path(args.log) if args.log else REPO / 'logs' / f"hestia_{d.replace('-', '')}.log"
    boundaries = parse_boundaries(log_path.read_text(errors='ignore'))
    from hestia_core.fyers_shadow import FyersClient, TokenGate
    st = TokenGate(REPO / 'hestia_data' / 'fyers_token.json').check()
    if not st.ok:
        print(f'\nflip comparison skipped: no usable Fyers token here ({st.reason})')
        return 0
    client = FyersClient(timeout_s=20.0)
    day = pd.Timestamp(d)
    by_name = {v: k for k, v in ENGINES.items()}
    print(f'\n== flips against what the engines acted on, {d} ==')
    boundaries['fyers_symbol'] = boundaries['symbol'].map(angel_to_fyers)
    for f in sorted(sd.glob(f'MCX_*_fyers_1m_{d}.csv')):
        fy_symbol = f.name.split('_fyers_1m_')[0].replace('MCX_', 'MCX:', 1)
        m = re.match(r'MCX:([A-Z]+)\d{2}[A-Z]{3}FUT', fy_symbol)
        engine = by_name.get(m.group(1)) if m else None
        if engine is None:
            continue
        cfg = importlib.import_module(f'{engine}_engine.engine_configs').DEFAULT
        res = client.minutes(fy_symbol, (day - timedelta(days=args.seed_days)).to_pydatetime(),
                             (day + timedelta(hours=23, minutes=35)).to_pydatetime(), st.auth)
        if res.kind != 'ok':
            print(f'{fy_symbol}: history fetch failed ({res.kind})')
            continue
        raw = pd.read_csv(f, parse_dates=['time_stamp'])
        print(fy_symbol, 'first-seen vs settled:', provisional_stats(raw))
        series = fyers_series(settled_minutes(raw), res.frame, day)
        print(fy_symbol, compare_flips(series, boundaries[boundaries['fyers_symbol'] == fy_symbol], cfg.st_period, cfg.st_multiplier, day))
    return 0


if __name__ == '__main__':
    sys.path.insert(0, str(REPO / 'data_pipeline'))
    sys.exit(main())
