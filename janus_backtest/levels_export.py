"""
Prints the actual Camarilla levels, session by session, for review: outputs/levels_<SYMBOL>.csv (one row per session: the previous session's
H/L/C the levels came from, S7..R7 as numbers, the session's own open/high/low/close, and the outermost level each side reached), and
outputs/levels_latest.txt (the levels for the NEXT session of each instrument, from the last session in the data).

    python janus_backtest/levels_export.py            # all four symbols
    python janus_backtest/levels_export.py CRUDEOILM  # one
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import janus_configs as configs  # noqa: E402
import janus_data as data        # noqa: E402
from janus_levels import camarilla_levels  # noqa: E402

LEVEL_ORDER = [f'S{k}' for k in range(7, 0, -1)] + ['C'] + [f'R{k}' for k in range(1, 8)]
MAX_K = max(configs.TOUCH_LEVELS)


def _outermost(bars: pd.DataFrame, lv: dict, side: str) -> str:
    """Highest R (or lowest S) level the session's price reached, '' if it reached none of R1/S1 and beyond."""
    reached = ''
    for k in range(1, MAX_K + 1):
        if (side == 'R' and bars['high'].max() >= lv[f'R{k}']) or (side == 'S' and bars['low'].min() <= lv[f'S{k}']):
            reached = f'{side}{k}'
    return reached


def session_row(s: dict) -> dict:
    b, lv, p = s['bars'], s['levels'], s['prev']
    row = {'date': s['date'], 'expiry': s['expiry'], 'prev_date': p['date'], 'prev_high': p['high'], 'prev_low': p['low'], 'prev_close': p['close'],
           'prev_range_pct': round(lv['R'] / lv['C'] * 100, 2)}
    row.update({k: round(lv[k], 2) for k in LEVEL_ORDER})
    row.update({'open': float(b['open'].iloc[0]), 'high': float(b['high'].max()), 'low': float(b['low'].min()), 'close': float(b['close'].iloc[-1]),
                'outermost_R_reached': _outermost(b, lv, 'R'), 'outermost_S_reached': _outermost(b, lv, 'S')})
    return row


def latest_block(symbol: str, last: dict) -> str:
    b = last['bars']
    lv = camarilla_levels(float(b['high'].max()), float(b['low'].min()), float(b['close'].iloc[-1]))
    lines = [f'{symbol}: levels for the session after {last["date"]} (contract {last["expiry"]}); '
             f'H={b["high"].max():.2f} L={b["low"].min():.2f} C={b["close"].iloc[-1]:.2f} range={lv["R"]:.2f}']
    lines += [f'   {k:>2}  {lv[k]:>12.2f}' for k in reversed(LEVEL_ORDER)]
    return '\n'.join(lines)


def main(argv):
    symbols = argv or configs.SYMBOLS
    os.makedirs(configs.OUTPUT_DIR, exist_ok=True)
    latest = []
    for sym in symbols:
        sessions = data.load_sessions(sym)
        df = pd.DataFrame([session_row(s) for s in sessions])
        df.to_csv(os.path.join(configs.OUTPUT_DIR, f'levels_{sym}.csv'), index=False)
        print(f'{sym}: {len(df)} sessions, {df["date"].min()} to {df["date"].max()} -> outputs/levels_{sym}.csv', flush=True)
        latest.append(latest_block(sym, sessions[-1]))
    text = '\n\n'.join(latest)
    print('\n' + text)
    with open(os.path.join(configs.OUTPUT_DIR, 'levels_latest.txt'), 'w') as f:
        f.write(text + '\n')


if __name__ == '__main__':
    main(sys.argv[1:])
