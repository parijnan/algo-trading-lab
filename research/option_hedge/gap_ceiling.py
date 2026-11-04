"""Phase 0 of plans/mcx-option-hedging.md: overnight / weekend gap exposure of Selene, Helios and Typhon, from the integrated backtest ledger (one unit each).
Read-only research script: no broker, no Delos. Rupees are per unit; x5 for the live 5 units. Run from anywhere:
    python research/option_hedge/gap_ceiling.py        # writes the gap events to EVENTS_CSV, prints the per-engine table
    python research/option_hedge/gap_followup.py       # tail-only option payouts and stop-outs that gapped through the stop
"""
import glob
import os

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EVENTS_CSV = '/tmp/option_hedge_gap_events.csv'    # scratch output, not tracked
ENG = {  # engine: (instrument, Rs per point per unit, stop %)
    'Selene': ('SILVERMIC', 1.0, 3.0),
    'Helios': ('GOLDPETAL', 20.0, 1.6),
    'Typhon': ('NATGASMINI', 500.0, 0.8),
}
BREAK_H = 3.0          # a hole this long between bars is a session break
WEEKEND_H = 40.0       # a break this long is a weekend / holiday
UNITS_LIVE = 5

led = pd.read_csv(f'{REPO}/hestia_backtest/outputs/integrated_trades.csv', parse_dates=['entry_ts', 'exit_ts'])
print('reasons', led.groupby(['engine', 'reason']).size().to_dict())


def load_contracts(instr):
    out = {}
    for root in ('mcx', 'mcx_fyers'):           # fyers last, so it wins on a clash (the backtests read Fyers first)
        for f in sorted(glob.glob(f'{REPO}/data_pipeline/data/{root}/{instr}/*_futures.csv')):
            exp = os.path.basename(f).split('_')[0]
            d = pd.read_csv(f)
            d.columns = [c.strip().lower() for c in d.columns]
            d['ts'] = pd.to_datetime(d['time_stamp'].astype(str).str[:19])
            d = d[['ts', 'open', 'high', 'low', 'close']]
            if exp in out:
                d = pd.concat([out[exp].reset_index(), d]).drop_duplicates('ts', keep='last')
            out[exp] = d.sort_values('ts').set_index('ts')
    return out


def breaks_of(d):
    ts = d.index.values
    dt = np.diff(ts).astype('timedelta64[s]').astype(float) / 3600
    idx = np.where(dt > BREAK_H)[0]
    return idx, dt[idx]


rows_ev, rows_tr = [], []
for eng, (instr, rpp, stop) in ENG.items():
    con = load_contracts(instr)
    info = {}
    for exp, d in con.items():
        idx, hrs = breaks_of(d)
        info[exp] = (d, idx, hrs)
    t = led[led.engine == eng].reset_index(drop=True)
    matched = 0
    for _, tr in t.iterrows():
        sign = 1 if tr.direction == 'bullish' else -1
        best, bdiff = None, 1e9
        for exp, (d, idx, hrs) in info.items():
            if tr.entry_ts in d.index:
                diff = abs(d.at[tr.entry_ts, 'open'] - tr.entry) / tr.entry
                if diff < bdiff - 1e-12:
                    best, bdiff = exp, diff
        gap_total, n_ev = 0.0, 0
        if best is not None and bdiff < 0.003:
            matched += 1
            d, idx, hrs = info[best]
            ts = d.index
            for i, h in zip(idx, hrs):
                before, after = ts[i], ts[i + 1]
                if tr.entry_ts <= before and tr.exit_ts >= after:
                    c0, o1 = d['close'].iat[i], d['open'].iat[i + 1]
                    g = sign * (o1 - c0)
                    # worst adverse excursion in the 15 minutes after the open
                    w = d.iloc[i + 1:i + 16]
                    exc = sign * ((w['low'].min() if sign > 0 else w['high'].max()) - o1) if len(w) else 0.0
                    gap_stop = (tr.reason == 'stop_loss') and (tr.exit_ts == after)
                    rows_ev.append(dict(engine=eng, trade=tr.trade_id, exit_ts=tr.exit_ts, before=before, after=after, hours=h,
                                        kind='weekend' if h >= WEEKEND_H else 'night',
                                        gap_pct=100 * g / c0, gap_rs=g * rpp, exc15_pct=100 * exc / o1, gap_stop=gap_stop))
                    gap_total += g * rpp
                    n_ev += 1
        # gap-through-stop excess on a stop that filled at the first open after a break
        loss_pct = -100 * tr.pts / tr.entry
        rows_tr.append(dict(engine=eng, trade=tr.trade_id, entry=tr.entry, exit_ts=tr.exit_ts, rs=tr.rs, gap_rs=gap_total, n_ev=n_ev, reason=tr.reason,
                            loss_pct=loss_pct, matched=best is not None and bdiff < 0.003))
    print(f'{eng}: trades {len(t)}, contract matched {matched} ({100*matched/len(t):.1f}%)')

ev = pd.DataFrame(rows_ev)
tr = pd.DataFrame(rows_tr)
ev.to_csv(EVENTS_CSV, index=False)

# excess beyond the stop on stop-outs filled at an open after a break
ex = ev[ev.gap_stop].merge(tr[['engine', 'trade', 'loss_pct', 'entry']], on=['engine', 'trade'])
ex['excess_pct'] = ex['loss_pct'] - ex['engine'].map({k: v[2] for k, v in ENG.items()})
ex['excess_rs'] = ex['excess_pct'] / 100 * ex['entry'] * ex['engine'].map({k: v[1] for k, v in ENG.items()})

summary = []
for eng, (instr, rpp, stop) in ENG.items():
    e = ev[ev.engine == eng]
    t = tr[tr.engine == eng].sort_values('exit_ts')
    total = t.rs.sum()
    gp = t.rs[t.rs > 0].sum()
    eq = t.rs.cumsum()
    peak = eq.cummax()
    dd = eq - peak
    tro = dd.idxmin()
    pk = eq.loc[:tro].idxmax()
    ddv = dd.min()
    win = t[(t.exit_ts > t.loc[pk, 'exit_ts']) & (t.exit_ts <= t.loc[tro, 'exit_ts'])]
    gap_in_dd = win.gap_rs.sum()
    adv = e[e.gap_rs < 0].gap_rs.sum()
    fav = e[e.gap_rs > 0].gap_rs.sum()
    exs = ex[ex.engine == eng]
    row = dict(engine=eng, trades=len(t), trades_crossing=int((t.n_ev > 0).sum()), gap_events=len(e),
               nights=int((e.kind == 'night').sum()), weekends=int((e.kind == 'weekend').sum()),
               total_rs=total, gross_profit_rs=gp, net_gap_rs=e.gap_rs.sum(), adverse_gap_rs=adv, favourable_gap_rs=fav,
               adverse_pct_of_total=100 * adv / total, adverse_pct_of_gross_profit=100 * adv / gp,
               maxdd_rs=ddv, adverse_gaps_inside_maxdd_rs=win.gap_rs[win.gap_rs < 0].sum(), net_gap_inside_maxdd_rs=gap_in_dd,
               mean_gap_pct=e.gap_pct.mean(), p1_gap_pct=e.gap_pct.quantile(.01), p5_gap_pct=e.gap_pct.quantile(.05),
               worst_gap_pct=e.gap_pct.min(), worst_gap_rs=e.gap_rs.min(), gaps_worse_than_half_pct=int((e.gap_pct < -0.5).sum()),
               gaps_worse_than_1pct=int((e.gap_pct < -1.0).sum()),
               stopouts_at_open=int(e.gap_stop.sum()), exc15_p5_pct=e.exc15_pct.quantile(.05), exc15_worst_pct=e.exc15_pct.min())
    for k in ('night', 'weekend'):
        s = e[e.kind == k]
        row[f'{k}_mean_gap_pct'] = s.gap_pct.mean()
        row[f'{k}_adverse_rs'] = s[s.gap_rs < 0].gap_rs.sum()
        row[f'{k}_net_rs'] = s.gap_rs.sum()
        row[f'{k}_worst_pct'] = s.gap_pct.min()
    # stop-outs that gapped through the stop
    s = ex[ex.engine == eng]
    s = s[s.excess_pct > 0.05]
    row['gapped_through_stop_n'] = len(s)
    row['gapped_through_excess_pct_sum'] = s.excess_pct.sum()
    row['gapped_through_excess_rs'] = float(s.excess_rs.sum())
    summary.append(row)
S = pd.DataFrame(summary).set_index('engine').T
pd.set_option('display.width', 200)
print(S.round(2).to_string())

print('\nTop 8 worst adverse gaps per engine (pct, Rs per unit):')
for eng in ENG:
    print(eng)
    print(ev[ev.engine == eng].nsmallest(8, 'gap_pct')[['trade', 'before', 'after', 'kind', 'gap_pct', 'gap_rs', 'gap_stop']].round(2).to_string(index=False))
ex2 = ex[ex.excess_pct > 0.05]
print('\nStop-outs gapped through the stop:')
print(ex2[['engine', 'trade', 'after', 'loss_pct', 'excess_pct']].round(2).to_string(index=False))
by_year = ev.assign(y=ev.after.dt.year).groupby(['engine', 'y']).gap_rs.agg(['count', 'sum', lambda s: s[s < 0].sum()]).round(0)
print('\nby year (events, net Rs/unit, adverse Rs/unit)'); print(by_year.to_string())
