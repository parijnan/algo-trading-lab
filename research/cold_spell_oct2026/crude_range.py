"""Is crude near its highs in a tight range (the whipsaw setup), and how did Prometheus do in past episodes of it?
Level = cumulated within-contract daily moves (roll-free), so only differences and ratios are meaningful."""
import numpy as np, pandas as pd
from common import daily_returns, load_trades, last_close, OUT

r = daily_returns('CRUDEOILM')
c = r.cumsum()
lvl = c - c.iloc[-1] + last_close('CRUDEOILM')   # anchored to the latest real close, so % figures are meaningful
feat = pd.DataFrame({'lvl': lvl})
feat['range20_pct'] = (lvl.rolling(20).max() - lvl.rolling(20).min()) / lvl * 100
feat['below_high120_pct'] = (lvl.rolling(120).max() - lvl) / lvl * 100
feat['lvl_pctile_250'] = lvl.rolling(250).apply(lambda x: (x[-1] >= x).mean() * 100, raw=True)
feat['move_next10'] = lvl.shift(-10) - lvl
feat = feat.dropna(subset=['range20_pct', 'below_high120_pct'])
feat.round(2).to_csv(OUT / 'crude_range_features.csv')
print(feat.tail(3).round(2).to_string())

tight = feat.range20_pct <= feat.range20_pct.quantile(0.25)
nearhigh = feat.below_high120_pct <= 5.0
feat['setup'] = tight & nearhigh
print('thresholds: range20 <= %.2f%% (25th pct); within 5%% of the 120-day high' % feat.range20_pct.quantile(0.25))
print('setup days', int(feat.setup.sum()), 'of', len(feat), '; today flagged:', bool(feat.setup.iloc[-1]))
# episodes
ep, c = [], 0
for x in feat.setup:
    if x: c += 1
    elif c: ep.append(c); c = 0
if c: ep.append(c)
print('episode lengths (sessions):', pd.Series(ep).describe().round(1).to_dict())
for lab, m in (('setup', feat.setup), ('other', ~feat.setup)):
    x = feat.loc[m, 'move_next10'].dropna()
    print(f'{lab}: n={len(x)}  next-10-session move: mean {x.mean():+.0f}, mean |move| {x.abs().mean():.0f}, share up {(x>0).mean():.0%}')

t = load_trades(); p = t[t.engine == 'Prometheus'].copy()
p['setup'] = p.entry_day.map(feat.setup).astype('boolean')
p = p.dropna(subset=['setup'])
rows = []
for lab, m in (('entered in setup', p.setup), ('entered otherwise', ~p.setup)):
    x = p[m]
    rows.append((lab, len(x), x.rs.mean(), (x.rs > 0).mean() * 100, x.rs.sum()))
R = pd.DataFrame(rows, columns=['group', 'trades', 'mean_rs_per_trade_1unit', 'win_pct', 'total_rs'])
R.round(1).to_csv(OUT / 'prometheus_by_crude_setup.csv', index=False)
print(R.round(1).to_string(index=False))
