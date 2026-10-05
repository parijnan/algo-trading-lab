"""
Phase 0 (descriptive, no strategy, nothing optimised): for the four instruments, how often each Camarilla level is touched, and what happens
after the first touch. Reads the study the way plans/janus-camarilla-research.md section 4 describes.

    python janus_backtest/phase0_descriptive.py            # all four symbols
    python janus_backtest/phase0_descriptive.py CRUDEOILM  # one

Writes outputs/phase0_events_<SYMBOL>.csv (one row per session and level) and outputs/phase0_summary.txt.
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import janus_configs as configs  # noqa: E402
import janus_data as data        # noqa: E402
import janus_events as events    # noqa: E402

pd.set_option('display.width', 220)
pd.set_option('display.max_columns', 40)


def build_events(symbol: str) -> pd.DataFrame:
    rows = []
    for s in data.load_sessions(symbol):
        rows.extend(events.analyse_session(s))
    return pd.DataFrame(rows)


def touch_table(ev: pd.DataFrame) -> pd.DataFrame:
    """Share of sessions in which each level was touched, split into true intraday touches and gap-opens beyond the level."""
    g = ev.groupby('level')
    return pd.DataFrame({'sessions': g.size(), 'touched_%': g['touched'].mean() * 100,
                         'gap_open_%': g['at_open'].mean() * 100,
                         'intraday_touch_%': (ev['touched'] & ~ev['at_open']).groupby(ev['level']).mean() * 100}).round(1)


def passage_table(ev: pd.DataFrame, level_num: int, pair: tuple, by: str = None) -> pd.DataFrame:
    """First passage after a touch of R/S<level_num> (intraday touches only): outcome shares, and P(inward first) among decided outcomes against the
    driftless benchmark. Pooled over the R and S sides (they are mirrored) unless `by` splits further."""
    tag = f'{pair[0]}_{pair[1]}'
    d = ev[ev['level'].isin([f'R{level_num}', f'S{level_num}']) & ev['touched'] & ~ev['at_open']].copy()
    d['group'] = 'all'
    out = []
    for name, g in d.groupby(by or 'group'):
        dec = g[g[f'fp_{tag}'].isin(['in', 'out'])]
        n = len(g)
        out.append({by or 'group': name, 'touches': n,
                    'in_%': (g[f'fp_{tag}'] == 'in').mean() * 100, 'out_%': (g[f'fp_{tag}'] == 'out').mean() * 100,
                    'ambig_%': (g[f'fp_{tag}'] == 'ambiguous').mean() * 100, 'none_%': (g[f'fp_{tag}'] == 'none').mean() * 100,
                    'P_in|decided_%': (dec[f'fp_{tag}'] == 'in').mean() * 100 if len(dec) else float('nan'),
                    'benchmark_%': dec[f'bm_{tag}'].mean() * 100 if len(dec) else float('nan')})
    t = pd.DataFrame(out).set_index(by or 'group')
    t['excess_pp'] = t['P_in|decided_%'] - t['benchmark_%']
    return t.round(1)


def excursions(ev: pd.DataFrame) -> pd.DataFrame:
    d = ev[ev['touched'] & ~ev['at_open']]
    g = d.groupby('level')
    return pd.DataFrame({'touches': g.size(), 'median_out_exc_R': g['out_exc_R'].median(), 'median_in_exc_R': g['in_exc_R'].median(),
                         'median_close_vs_level_R': g['close_vs_level_R'].median(),
                         'closed_beyond_level_%': (d['close_vs_level_R'] > 0).groupby(d['level']).mean() * 100}).round(2)


def report(symbol: str, ev: pd.DataFrame) -> str:
    s = [f'==== {symbol}: {ev["date"].nunique()} sessions, {ev["date"].min()} to {ev["date"].max()}, '
         f'median previous-session range {ev["r_pct"].median():.2f}% of price ====', '', 'Touch frequency by level:', touch_table(ev).to_string(), '',
         'Excursions after the first intraday touch (units of previous range R):', excursions(ev).to_string()]
    for lvl, pairs in configs.FIRST_PASSAGE.items():
        for pair in pairs:
            s += ['', f'First passage after a touch of level {lvl}: outward R{pair[0]} vs inward {"R" + str(pair[1]) if pair[1] != "C" else "previous close"} '
                      f'(S side mirrored, pooled):', passage_table(ev, lvl, pair).to_string(),
                  '  ... by year:', passage_table(ev, lvl, pair, 'year').to_string(),
                  '  ... by open zone:', passage_table(ev, lvl, pair, 'open_zone').to_string()]
    return '\n'.join(s)


def main(argv):
    symbols = argv or configs.SYMBOLS
    os.makedirs(configs.OUTPUT_DIR, exist_ok=True)
    text = []
    for sym in symbols:
        ev = build_events(sym)
        ev.to_csv(os.path.join(configs.OUTPUT_DIR, f'phase0_events_{sym}.csv'), index=False)
        text.append(report(sym, ev))
        print(text[-1], '\n', flush=True)
    with open(os.path.join(configs.OUTPUT_DIR, 'phase0_summary.txt'), 'w') as f:
        f.write('\n\n'.join(text))


if __name__ == '__main__':
    main(sys.argv[1:])
