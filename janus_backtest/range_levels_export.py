"""
Prints the range-basis Camarilla levels, session by session, for review: outputs/range_levels_<SYMBOL>.csv. One row per traded session: a range id (one number per
range, counted in order of first use; a range is one contract's setter candle plus its wick extensions, so the id stays while the bounds widen), the range the levels
came from (high, low, setter date, age in sessions, wick extensions, width), the previous session's close, S7..R7 as numbers, the previous-session (control) R3/S3/R4/S4
beside them, the session's own open/high/low/close, and which levels the session touched (a bar's high at or above an R level, or its low at or below an S level; C
is left out). The exact first-touch minute of every touched level is in outputs/range_level_touches_<SYMBOL>.csv, one row per session and level, in time order. `level_basis` says 'range_setter' or 'previous_session' (the fallback when the previous session took no part
in the range history). Levels follow plans/janus-range-levels.md; nothing is recomputed here, the rows are read off janus_data.load_sessions(level_source='range_setter').

    python janus_backtest/range_levels_export.py            # all four symbols
    python janus_backtest/range_levels_export.py CRUDEOILM  # one
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import janus_configs as configs  # noqa: E402
import janus_data as data        # noqa: E402
from levels_export import LEVEL_ORDER  # noqa: E402

CONTROL_LEVELS = ('S4', 'S3', 'R3', 'R4')
TOUCH_ORDER = [k for k in LEVEL_ORDER if k != 'C']


def first_touches(bars: pd.DataFrame, levels: dict) -> list:
    """[(level, first-touch bar timestamp)] in time order. An R level is touched by the first bar whose high is at or above it, an S level by the first bar whose low is at
    or below it; a session that opens beyond a level touches it on its first bar. `bars` is indexed by bar start time."""
    out = []
    high, low = bars['high'].to_numpy(), bars['low'].to_numpy()
    for k in TOUCH_ORDER:
        hit = (high >= levels[k]) if k.startswith('R') else (low <= levels[k])
        if hit.any():
            out.append((k, bars.index[int(hit.argmax())]))
    return sorted(out, key=lambda t: t[1])


def session_row(s: dict) -> dict:
    b, lv, ctl, p, rng = s['bars'], s['levels'], s['levels_prev'], s['prev'], s['range']
    row = {'date': s['date'], 'expiry': s['expiry'], 'data_source': s['data_source'], 'prev_date': p['date'], 'prev_close': p['close'],
           'level_basis': 'range_setter' if rng is not None else 'previous_session',
           'range_high': round(rng['high'], 2) if rng else p['high'], 'range_low': round(rng['low'], 2) if rng else p['low'],
           'setter_date': rng['setter_date'] if rng else '', 'age_sessions': rng['age'] if rng else '', 'extensions': rng['extensions'] if rng else ''}
    row['range_width'] = round(row['range_high'] - row['range_low'], 2)
    row['range_width_pct'] = round(row['range_width'] / p['close'] * 100, 2)
    touches = first_touches(b, lv)
    got = {k for k, _ in touches}
    row['n_touched'] = len(got)
    row['max_R_touched'] = next((k for k in reversed(TOUCH_ORDER) if k.startswith('R') and k in got), '')
    row['max_S_touched'] = next((k for k in TOUCH_ORDER if k.startswith('S') and k in got), '')
    row['levels_touched'] = ' '.join(k for k in TOUCH_ORDER if k in got)
    row.update({k: round(lv[k], 2) for k in LEVEL_ORDER})
    row.update({f'prevsess_{k}': round(ctl[k], 2) for k in CONTROL_LEVELS})
    row.update({'open': float(b['open'].iloc[0]), 'high': float(b['high'].max()), 'low': float(b['low'].min()), 'close': float(b['close'].iloc[-1])})
    return row


def main(argv):
    os.makedirs(configs.OUTPUT_DIR, exist_ok=True)
    for sym in (argv or configs.SYMBOLS):
        sessions = data.load_sessions(sym, level_source='range_setter', live_tail=True)
        ids, rows, touch_rows = {}, [], []
        for s in sessions:
            key = (s['expiry'], s['range']['setter_date']) if s['range'] else ('previous_session', s['date'])
            rid = ids.setdefault(key, len(ids) + 1)
            row = session_row(s)
            rows.append({'range_id': rid, **row})
            for k, ts in first_touches(s['bars'], s['levels']):
                touch_rows.append({'date': s['date'], 'range_id': rid, 'level': k, 'level_price': round(s['levels'][k], 2), 'first_touch_time': ts.strftime('%H:%M'),
                                   'minutes_from_open': int((ts - s['bars'].index[0]).total_seconds() // 60)})
        df, tf = pd.DataFrame(rows), pd.DataFrame(touch_rows)
        path = os.path.join(configs.OUTPUT_DIR, f'range_levels_{sym}.csv')
        tpath = os.path.join(configs.OUTPUT_DIR, f'range_level_touches_{sym}.csv')
        df.to_csv(path, index=False)
        tf.to_csv(tpath, index=False)
        print(f'{sym}: {len(df)} sessions, {df["range_id"].nunique()} ranges, {df["date"].min()} to {df["date"].max()}, '
              f'{(df["level_basis"] == "previous_session").sum()} on the previous-session fallback -> {path}; {len(tf)} first touches -> {tpath}', flush=True)


if __name__ == '__main__':
    main(sys.argv[1:])
