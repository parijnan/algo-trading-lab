"""
Does early favourable excursion (MFE) predict a trade's outcome beyond what its current unrealised P&L already says, and is acting on it worth it?

Per trade and per checkpoint k (trading minutes since entry), in the engine's long frame (a short is reflected about its entry):
  MFE_k = peak gain over the first k bars, MAE_k = deepest dip, U_k = open of bar k minus entry (the price a rule would act on),
  alive iff the trade's decided exit comes at bar k or later (a trade already out is excluded: causal, no lookahead).
Outcome = the trade's decided result per lot in percent of entry (Prometheus: the two lots averaged), so engines and price eras are comparable.
First-look rules, acting at the open of bar k: R1 exit if U_k <= 0; R2 exit if MFE_k < q x R (R = the engine's stop %).

    python research/early_mfe/early_mfe_study.py
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import early_configs as cfg  # noqa: E402
import trailing_overlay as TO  # noqa: E402


def make_record(e, entry_ts, o, h, l, exit_idx, pct, lot_exits=None, lot_pts=None, engine=''):
    """A trade in the long frame: o has one more bar than h/l (the open a checkpoint acts on)."""
    return {'e': e, 'entry_ts': entry_ts, 'o': o, 'h': h, 'l': l, 'i_x': exit_idx, 'pct': pct, 'lot_exits': lot_exits, 'lot_pts': lot_pts, 'engine': engine}


def build_single(trades, p, sl_pct, target_pct, engine):
    recs = []
    for t in trades:
        r = TO.simulate_trail(t, p, sl_pct, target_pct, ('none',))
        if r is None:
            continue
        e, bull, lo, end = t['entry'], t['bull'], t['lo'], t['end']
        o, h, l = p.open[lo:end + 1].copy(), p.high[lo:end].copy(), p.low[lo:end].copy()
        if not bull:
            o, h, l = 2 * e - o, 2 * e - l, 2 * e - h
        n = end - lo
        active = ~p.guarded[lo:end]
        sl_level = e * (1 - sl_pct / 100)
        hit_s = (l <= sl_level) & active
        i_s = int(hit_s.argmax()) if hit_s.any() else n
        i_t = n
        if target_pct is not None:
            hit_t = (h >= e * (1 + target_pct / 100)) & active
            i_t = int(hit_t.argmax()) if hit_t.any() else n
        i_x = i_s if (i_s < n and i_s <= i_t) else i_t
        recs.append(make_record(e, t['entry_ts'], o, h, l, i_x, r[0] / e * 100, engine=engine))
    return recs


def build_prometheus(track_dir, mult_dir, engine):
    base = os.path.join(cfg.REPO_ROOT, 'prometheus_backtest', track_dir, 'data_sweep', mult_dir)
    b = pd.read_csv(os.path.join(base, 'bespoke_trade_summary.csv'), parse_dates=['entry_ts', 'lot1_exit_ts', 'lot2_exit_ts'])
    recs = []
    for r in b.itertuples():
        f = os.path.join(base, 'trade_logs', f"trade_{int(r.trade_id):04d}_{r.entry_ts:%Y-%m-%d_%H%M}_{'S' if r.direction == 'bearish' else 'B'}.csv")
        if not os.path.exists(f) or pd.isna(r.lot2_exit_ts):
            continue
        t = pd.read_csv(f, parse_dates=['ts'])
        e, bull = float(r.entry_price), r.direction == 'bullish'
        o, h, l = t['open'].to_numpy(float), t['high'].to_numpy(float), t['low'].to_numpy(float)
        if not bull:
            o, h, l = 2 * e - o, 2 * e - l, 2 * e - h
        ts = t['ts'].to_numpy()
        idx = [int(np.searchsorted(ts, np.datetime64(x))) for x in (r.lot1_exit_ts, r.lot2_exit_ts)]
        if max(idx) >= len(ts):
            continue
        pts = [float(r.lot1_pnl_points), float(r.lot2_pnl_points)]
        recs.append(make_record(e, r.entry_ts, o, h[:-1] if len(h) == len(o) else h, l[:-1] if len(l) == len(o) else l, max(idx), sum(pts) / 2 / e * 100,
                                lot_exits=idx, lot_pts=pts, engine=engine))
    return recs


def alive_at(r, k):
    return r['i_x'] >= k and len(r['h']) >= k and len(r['o']) > k


def checkpoint_features(r, k):
    e = r['e']
    return (r['h'][:k].max() - e) / e * 100, (e - r['l'][:k].min()) / e * 100, (r['o'][k] - e) / e * 100


def partial_corr(x, z, y):
    """Semi-partial correlation: corr(x with the linear effect of z removed, y). y is NOT residualised, so this is not the full partial correlation; the
    2026-09-08 study used the same definition, which keeps the two comparable."""
    if len(x) < 5 or np.std(z) == 0:
        return float('nan')
    slope = np.polyfit(z, x, 1)
    res = x - np.polyval(slope, z)
    return float(np.corrcoef(res, y)[0, 1]) if np.std(res) > 0 else float('nan')


def descriptive(recs, k):
    rows = [(*checkpoint_features(r, k), r['pct']) for r in recs if alive_at(r, k)]
    if len(rows) < 10:
        return None
    a = np.array(rows)
    mfe, mae, u, y = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
    order = np.argsort(mfe, kind='stable')
    third = len(a) // 3
    lo_i, hi_i = order[:third], order[-third:]
    out = {'k': k, 'n': len(a), 'corr_mfe': round(float(np.corrcoef(mfe, y)[0, 1]), 3), 'corr_u': round(float(np.corrcoef(u, y)[0, 1]), 3),
           'partial_mfe': round(partial_corr(mfe, u, y), 3),
           'low_win': round(float((y[lo_i] > 0).mean() * 100), 1), 'low_mean': round(float(y[lo_i].mean()), 3),
           'high_win': round(float((y[hi_i] > 0).mean() * 100), 1), 'high_mean': round(float(y[hi_i].mean()), 3)}
    mae_low = mae[lo_i]
    if len(lo_i) >= 2:
        med = np.median(mae_low)
        chop, wrong = y[lo_i][mae_low <= med], y[lo_i][mae_low > med]
        out.update({'chop_n': len(chop), 'chop_win': round(float((chop > 0).mean() * 100), 1), 'chop_mean': round(float(chop.mean()), 3),
                    'wrong_n': len(wrong), 'wrong_win': round(float((wrong > 0).mean() * 100), 1), 'wrong_mean': round(float(wrong.mean()), 3)})
    return out


def rule_outcome(r, k, kind, thresh):
    """The trade's result (percent per lot) with the rule applied at checkpoint k; the baseline result if it does not trigger. Second value: triggered."""
    if not alive_at(r, k):
        return r['pct'], False
    mfe, _, u = checkpoint_features(r, k)
    trig = (u <= 0) if kind == 'u' else (mfe < thresh)
    if not trig:
        return r['pct'], False
    e, px = r['e'], r['o'][k]
    if r['lot_exits'] is None:
        return (px - e) / e * 100, True
    pts = [r['lot_pts'][j] if r['lot_exits'][j] < k else px - e for j in range(2)]      # a lot already booked keeps its result
    return sum(pts) / 2 / e * 100, True


def evaluate(recs, k, kind, thresh):
    split = pd.Timestamp(cfg.SPLIT_DATE)
    rows, trig_base, trig_rule = [], [], []
    for r in recs:
        pct, trig = rule_outcome(r, k, kind, thresh)
        rows.append((r['entry_ts'], pct))
        if trig:
            trig_base.append(r['pct']); trig_rule.append(pct)
    df = pd.DataFrame(rows, columns=['entry_ts', 'pct']).sort_values('entry_ts', kind='stable')
    full, pre, post = TO.metrics(df), TO.metrics(df[df['entry_ts'] < split]), TO.metrics(df[df['entry_ts'] >= split])
    return {'rule': f"U<=0 @ {k}" if kind == 'u' else f"MFE<{thresh:.3g}% @ {k}", 'triggered': len(trig_base),
            'held_mean': round(float(np.mean(trig_base)), 3) if trig_base else float('nan'), 'exit_mean': round(float(np.mean(trig_rule)), 3) if trig_rule else float('nan'),
            'recovered_win_pct': round(float((np.array(trig_base) > 0).mean() * 100), 1) if trig_base else float('nan'),
            **full, 'pre_total': pre['total_pct'], 'pre_calmar': pre['calmar_pct'], 'post_total': post['total_pct'], 'post_calmar': post['calmar_pct']}


def load_engines():
    out = {}
    for name, (trades, p, sl, tgt, _ref) in TO.load_all().items():
        out[name] = (build_single(trades, p, sl, tgt, name), sl)
    for name, (track, mult, R) in cfg.PROMETHEUS_TRACKS.items():
        out[name] = (build_prometheus(track, mult, name), R)
    return out


def main():
    os.makedirs(cfg.OUTPUT_DIR, exist_ok=True)
    desc_rows, rule_rows = [], []
    for name, (recs, R) in load_engines().items():
        print(f'\n===== {name}: {len(recs)} trades, R (stop) = {R}%', flush=True)
        for k in cfg.CHECKPOINTS:
            d = descriptive(recs, k)
            if d:
                desc_rows.append({'engine': name, **d})
        base = evaluate(recs, 10 ** 9, 'u', 0)
        print(f"baseline: total {base['total_pct']}%, DD {base['dd_pct']}%, Calmar {base['calmar_pct']}, win {base['win_pct']}% | pre {base['pre_total']}/{base['pre_calmar']} post {base['post_total']}/{base['post_calmar']}")
        rule_rows.append({'engine': name, 'R': R, 'baseline': True, **base})
        for k in cfg.RULE_CHECKPOINTS:
            rule_rows.append({'engine': name, 'R': R, 'baseline': False, **evaluate(recs, k, 'u', 0)})
            for q in cfg.MFE_R_FRACTIONS:
                rule_rows.append({'engine': name, 'R': R, 'baseline': False, **evaluate(recs, k, 'mfe', q * R)})
    d = pd.DataFrame(desc_rows)
    r = pd.DataFrame(rule_rows)
    d.to_csv(os.path.join(cfg.OUTPUT_DIR, 'descriptive.csv'), index=False)
    r.to_csv(os.path.join(cfg.OUTPUT_DIR, 'rules.csv'), index=False)
    pd.set_option('display.width', 250); pd.set_option('display.max_columns', None)
    for name, g in d.groupby('engine', sort=False):
        print(f'\n### {name}'); print(g.drop(columns='engine').to_string(index=False))
    for name, g in r.groupby('engine', sort=False):
        b = g[g['baseline']].iloc[0]
        print(f"\n### {name} rules (baseline total {b['total_pct']}%, Calmar {b['calmar_pct']}, pre {b['pre_calmar']}, post {b['post_calmar']})")
        print(g[~g['baseline']][['rule', 'triggered', 'held_mean', 'exit_mean', 'recovered_win_pct', 'total_pct', 'dd_pct', 'calmar_pct', 'pre_total', 'pre_calmar', 'post_total', 'post_calmar']].to_string(index=False))


if __name__ == '__main__':
    main()
