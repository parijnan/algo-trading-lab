"""Price view: 5-session efficiency ratio per instrument and how long 'ranging' lasts (daily closes)."""
import pandas as pd
from common import INSTRUMENTS, OUT, load_trades

from common import daily_returns
ER = {}
for k, ins in INSTRUMENTS.items():
    r = daily_returns(ins)
    ER[k] = r.rolling(5).sum().abs() / r.abs().rolling(5).sum()
E = pd.DataFrame(ER).dropna()
E.round(3).to_csv(OUT / 'efficiency_ratio_5d.csv')
thr = E.quantile(0.30); n = (E < thr).sum(axis=1)
print('window', E.index[0].date(), E.index[-1].date(), len(E)); print(E.tail(6).round(2))
for K in (3, 4):
    f = n >= K; runs, c = [], 0
    for x in f:
        if x: c += 1
        elif c: runs.append(c); c = 0
    runs = pd.Series(runs)
    print(f'>= {K}/4 ranging: {f.mean():.1%} of days, {len(runs)} runs, median {runs.median()}, max {runs.max()}')
f = (n >= 3).astype(int)
for k in (3, 5, 10): print(f'P(flag {k} days later | flag today) = {f.shift(-k)[f == 1].mean():.2f}  (base {f.mean():.2f})')
t = load_trades(); pl = t.groupby('exit_day').rs.sum().reindex(E.index, fill_value=0)
for K in (3, 4):
    g = n >= K; fwd = pl.rolling(5).sum().shift(-5)
    print(f'K={K}: mean daily pnl flagged {pl[g].mean():.0f} vs other {pl[~g].mean():.0f}; next-5 {fwd[g].mean():.0f} vs {fwd.mean():.0f}')
