"""
Feasibility of "equal thirds, trail on T2, re-entry of the booked lot" (plans/janus-camarilla-research.md items 3 and 8). Descriptive, no costs,
nothing optimised: for the first touch entry of each setup per session (fade = reclaim of S3/R3, breakout = break of R4/S4, both directions),
how often does price reach T1, T2, T3, how often does it come back to the entry price after T1 (the re-entry opportunity), what happens to a
lot re-bought there, and how often would a stop trailed to cost or to T1 after T2 be hit before T3.

    python janus_backtest/thirds_feasibility.py            # all four symbols
    python janus_backtest/thirds_feasibility.py CRUDEOILM

Setups are measured independently of each other (no daily entry cap, no stop-and-reverse chain); the full engine comes later. Writes
outputs/thirds_trades_<SYMBOL>.csv (gitignored) and outputs/thirds_feasibility_summary.txt.
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import janus_configs as configs  # noqa: E402
import janus_data as data        # noqa: E402
import janus_setups as setups    # noqa: E402

pd.set_option('display.width', 220)
pd.set_option('display.max_columns', 40)


def build_trades(symbol: str):
    """(DataFrame of entries with their paths, count of entries skipped because the fill was already at or past T1)."""
    rows, skipped = [], 0
    for s in data.load_sessions(symbol):
        for fam, spec in configs.SETUPS.items():
            for d in (1, -1):
                h, l, o, c, lv = setups.frame(s, d)
                e = setups.find_entry(h, l, o, lv[spec['entry']], configs.AT_OPEN_MINUTES)
                if e is None:
                    continue
                j, fill = e
                stop, tg = lv[spec['stop']], [lv[k] for k in spec['targets']]
                if fill >= tg[0]:
                    skipped += 1
                    continue
                risk = fill - stop
                row = {'symbol': symbol, 'date': s['date'], 'year': s['date'].year, 'family': fam, 'side': 'long' if d == 1 else 'short',
                       'entry_bar': j, 'risk_pct': risk / abs(fill) * 100, 'gap_in_risk': (fill - lv[spec['entry']]) / risk,
                       'deep_arm': bool(j > 0 and l[:j].min() <= stop), 'late_fill': fill > lv[spec['entry']]}
                row.update({f'rr{i + 1}': (t - fill) / risk for i, t in enumerate(tg)})
                row.update(setups.trade_path(h, l, j, fill, stop, tg))
                rows.append(row)
    return pd.DataFrame(rows), skipped


def _share(n, d):
    return float('nan') if d == 0 else n / d * 100


def summarise(tr: pd.DataFrame, by: list) -> pd.DataFrame:
    """One row per group: entries, then each probability as a percentage of its own base (the base is in the column name)."""
    out = []
    for key, g in tr.groupby(by):
        t1 = g[g['a'] == 'T1']
        t2 = t1[t1['t2_vs_stop'] == 'T2']
        ret = t1[t1['b'] == 'retrace']
        dec = ret[ret['reentry'].isin(['win', 'loss'])]
        bm = (1.0 / (1.0 + dec['rr1'])).mean() * 100          # driftless P(T1 again before the stop) from the entry price: risk / (risk + reward)
        win = (dec['reentry'] == 'win').sum()
        exp = ((dec['rr1'].where(dec['reentry'] == 'win', -1.0)).mean()) if len(dec) else float('nan')   # risk units per re-bought lot, gross
        out.append({**dict(zip(by, key if isinstance(key, tuple) else (key,))), 'entries': len(g),
                    'T1%': _share(len(t1), len(g)), 'stop_first%': _share((g['a'] == 'stop').sum(), len(g)),
                    'T2|T1%': _share(len(t2), len(t1)),
                    'retrace|T1%': _share(len(ret), len(t1)), 'retraces': len(ret), 'reentry_open': int((ret['reentry'] == 'open').sum()),
                    'reentry_win%': _share(win, len(dec)), 'benchmark%': bm, 'reentry_exp_risk': exp,
                    'T3|T2%': _share((t2['c_fixed'] == 'T3').sum(), len(t2)),
                    'coststop|T2%': _share((t2['c_cost'] == 'stopped').sum(), len(t2)),
                    'T1stop|T2%': _share((t2['c_t1'] == 'stopped').sum(), len(t2))})
    return pd.DataFrame(out).round(1)


def geometry(tr: pd.DataFrame) -> pd.DataFrame:
    """Median stop distance (percent of the entry price) and the reward in units of the stop distance, per instrument and family."""
    g = tr.groupby(['symbol', 'family'])
    return pd.DataFrame({'risk_%_of_price': g['risk_pct'].median(), 'rr_T1': g['rr1'].median(), 'rr_T2': g['rr2'].median(),
                         'rr_T3': g['rr3'].median(), 'deep_arm_%': g['deep_arm'].mean() * 100,
                         'entry_bar_stop_%': g['entry_bar_stop'].mean() * 100, 'fill_gap_med': g['gap_in_risk'].median(), 'fill_gap_p90': g['gap_in_risk'].quantile(0.9), 'ambiguous_%': g['a_ambiguous'].mean() * 100}).round(2)


def main(symbols):
    os.makedirs(configs.OUTPUT_DIR, exist_ok=True)
    frames, report = [], []
    for sym in symbols:
        tr, skipped = build_trades(sym)
        tr.to_csv(os.path.join(configs.OUTPUT_DIR, f'thirds_trades_{sym}.csv'), index=False)
        frames.append(tr)
        report.append(f'{sym}: {len(tr)} entries, {skipped} skipped (fill already at or past T1)')
    allt = pd.concat(frames, ignore_index=True)
    text = ['Thirds / trail / re-entry feasibility (descriptive, no costs; setups measured independently, first entry per setup per session)',
            f'TRAIL_SAME_BAR={configs.TRAIL_SAME_BAR}', '', *report, '',
            'Geometry (median; rr = reward in units of the stop distance)', geometry(allt).to_string(), '',
            'By instrument and family', summarise(allt, ['symbol', 'family']).to_string(), '',
            'Pooled over instruments, by family', summarise(allt, ['family']).to_string(), '',
            'Pooled over instruments, by family and year', summarise(allt, ['family', 'year']).to_string()]
    out = '\n'.join(text)
    with open(os.path.join(configs.OUTPUT_DIR, 'thirds_feasibility_summary.txt'), 'w') as f:
        f.write(out + '\n')
    print(out)


if __name__ == '__main__':
    main(sys.argv[1:] or configs.SYMBOLS)
