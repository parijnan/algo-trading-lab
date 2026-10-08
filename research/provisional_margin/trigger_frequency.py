"""
How often the provisional path would run (plans/hestia-provisional-all-engines.md section 1, point 3 of the review). Angel One omits one-minute candles in which
nothing traded, so a 15-minute window holds fewer than 15 candles at the boundary whenever any minute was quiet, not only when a REST call fails. Hestia
(`live_data._after_merge`) then sends a provisional bar. Counted here from the Angel One nightly pipeline files (the same source the live cache is built from),
per instrument, per contract on the dates it is effective: windows, windows with < BAR_MINUTES candles, how many of those are real flips, and how many have a
traded final minute (the only ones the feed-staleness gate lets through).

    python research/provisional_margin/trigger_frequency.py [SYMBOL ...]
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import margin_configs as cfg  # noqa: E402
import measure_margins as mm  # noqa: E402
import data_loader_p3 as p3  # noqa: E402

SINCE = '2026-02-01'            # the Angel One files are complete from early 2026; earlier months are partial in this repo
LAST_WINDOW_START = '23:30'     # windows starting at or after this are the closing windows (23:30 or 23:55 close): left out of the count


def angel_table(symbol: str) -> pd.DataFrame:
    _, period, mult = cfg.INSTRUMENTS[symbol]
    calendar, closed = p3._discover_expiries(symbol), mm._closed_dates()
    frames = {e: p3._read_contract_file(p3.ANGELONE_DATA_DIR, symbol, e) for e in calendar}
    days = sorted({d for f in frames.values() if len(f) for d in f['time_stamp'].dt.date.unique()})
    effective = {}
    for d in days:
        if d < pd.Timestamp(SINCE).date() or d.weekday() >= 5:
            continue
        eff = p3._effective_contract_for_date(d, calendar, closed)
        if eff is not None and len(frames.get(eff, ())) and d in set(frames[eff]['time_stamp'].dt.date):
            effective.setdefault(eff, set()).add(d)
    out = []
    for e, dset in effective.items():
        f = frames[e]
        b = mm.add_signal(mm.build_bars(f), period, mult)
        b = b[b['start'].dt.date.isin(dset)].copy()
        b.insert(0, 'expiry', e)
        out.append(b)
    return pd.concat(out).sort_values('start').reset_index(drop=True)


def summarize(t: pd.DataFrame) -> dict:
    t = t[t['start'].dt.strftime('%H:%M') < LAST_WINDOW_START]
    inc = t['n_min'] < cfg.BAR_MINUTES
    sessions = t['start'].dt.date.nunique()
    flips = t['flip'].fillna(False).astype(bool)
    return {'sessions': sessions, 'windows': len(t), 'incomplete': int(inc.sum()), 'incomplete_pct': float(inc.mean() * 100), 'incomplete_per_session': float(inc.sum() / sessions),
            'incomplete_final_traded_per_session': float((inc & t['final_traded']).sum() / sessions), 'real_flips': int(flips.sum()),
            'flips_in_incomplete': int((flips & inc).sum()), 'flips_in_incomplete_final_traded': int((flips & inc & t['final_traded']).sum()),
            'first': str(t['start'].min()), 'last': str(t['start'].max())}


def main(argv):
    rows = []
    for s in (argv or list(cfg.INSTRUMENTS)):
        r = summarize(angel_table(s))
        r['symbol'] = s
        rows.append(r)
    res = pd.DataFrame(rows).set_index('symbol')
    os.makedirs(cfg.OUTPUT_DIR, exist_ok=True)
    res.to_csv(os.path.join(cfg.OUTPUT_DIR, 'trigger_frequency.csv'))
    pd.set_option('display.width', 220)
    print(res.T.to_string())


if __name__ == '__main__':
    main(sys.argv[1:])
