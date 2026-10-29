"""P&L view of cold spells: underwater (drawdown) spells and cold five-session windows of the four-engine backtest (1 unit each)."""
import numpy as np, pandas as pd
from common import load_trades, OUT

t = load_trades()
d = t.groupby('exit_day').rs.sum()
d = d.reindex(pd.bdate_range(d.index.min(), d.index.max()), fill_value=0.0)
eq = d.cumsum(); dd = eq - eq.cummax()

spells, start, trough, tday = [], None, 0, None
for day, v in dd.items():
    if v < 0:
        if start is None: start, trough, tday = day, v, day
        if v < trough: trough, tday = v, day
    elif start is not None:
        spells.append((start, tday, day, trough)); start = None
S = pd.DataFrame(spells, columns=['start', 'trough_day', 'recovered', 'depth_rs'])
S['underwater_wd'] = [len(pd.bdate_range(a, b)) - 1 for a, b in zip(S.start, S.recovered)]
S['decline_wd'] = [len(pd.bdate_range(a, b)) - 1 for a, b in zip(S.start, S.trough_day)]
S['recovery_wd'] = S.underwater_wd - S.decline_wd
S.to_csv(OUT / 'underwater_spells.csv', index=False)
print('current drawdown', round(dd.iloc[-1]), 'max', round(dd.min()), 'spell open since', start.date() if start is not None else None)
def q(x): return [round(x.median()), round(x.quantile(.9)), x.max()]
for lo, name in ((0, 'all'), (15000, '>=15k'), (30000, '>=30k')):
    s = S[S.depth_rs <= -lo]
    print(f'{name}: n={len(s)} underwater med/p90/max {q(s.underwater_wd)} decline {q(s.decline_wd)} recovery {q(s.recovery_wd)}')

r5 = d.rolling(5).sum(); thr = -11700
cold = r5[r5 <= thr]; ep = []; last = None
for day in cold.index:
    if last is None or (day - last).days > 14: ep.append([day, day])
    else: ep[-1][1] = day
    last = day
rows = []
for a, b in ep:
    i = d.index.get_loc(a); k = i
    while k < len(r5) and not (r5.iloc[k] > 0): k += 1
    rows.append((a.date(), (b - a).days, k - i if k < len(r5) else np.nan, eq.iloc[min(i + 20, len(eq) - 1)] - eq.iloc[i] if i + 20 < len(eq) else np.nan))
E = pd.DataFrame(rows, columns=['first_cold_day', 'cold_span_calendar_days', 'sessions_until_r5_positive', 'pnl_next_20_sessions_rs'])
E.to_csv(OUT / 'cold_windows.csv', index=False)
print(E.to_string())
